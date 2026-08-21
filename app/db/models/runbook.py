"""
Model Runbook + RunbookStep - "công thức chữa bệnh" cho hạ tầng.

KHÁI NIỆM: runbook là quy trình xử lý sự cố mà SRE vốn viết trong wiki
("nếu payment-api hết RAM thì restart pod, chờ 30s, kiểm tra health").
Ở đây ta biến quy trình đó thành DỮ LIỆU để máy tự chạy.

KIẾN THỨC SQLALCHEMY QUAN TRỌNG
  1. relationship + back_populates : liên kết 2 chiều giữa 2 model
  2. cascade="all, delete-orphan"  : xoá runbook thì xoá luôn các step
  3. order_by                      : step phải chạy đúng thứ tự
  4. lazy="selectin"               : chống bài toán N+1 query
  5. CheckConstraint               : ràng buộc dữ liệu ngay tại DB
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_column
from app.domain.enums import ActionType


class Runbook(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "runbooks"

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, default="")

    # Bật/tắt runbook mà không cần xoá -> an toàn khi runbook gây hại.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    # Số nhỏ = ưu tiên cao (giống nice của Linux). Khi nhiều runbook cùng
    # khớp, engine chọn theo priority rồi tới độ cụ thể của luật.
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    # LUẬT KHỚP - lưu JSONB, cấu trúc do app/domain/rules.py định nghĩa.
    # Đây là điểm mấu chốt: sửa luật = update 1 row, KHÔNG cần deploy code.
    match_rules: Mapped[dict[str, Any]] = mapped_column(default=dict)

    # Chạy tối đa bao nhiêu lần cho cùng một incident trước khi bỏ cuộc.
    max_attempts: Mapped[int] = mapped_column(Integer, default=2, nullable=False)

    # Thời gian "nghỉ" giữa 2 lần chạy cho cùng một fingerprint.
    # Chống vòng lặp tai hoạ: restart -> lỗi lại -> restart -> ...
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False)

    # Runbook nguy hiểm (vd xoá dữ liệu, scale down) thì bắt buộc người duyệt.
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # relationship: KHÔNG phải cột trong DB, mà là "cây cầu" Python-level.
    #   back_populates : hai chiều đồng bộ với nhau (sửa bên này bên kia biết)
    #   cascade        : xoá runbook -> xoá luôn step (delete-orphan: step bị
    #                    tách khỏi runbook cũng bị xoá)
    #   order_by       : luôn trả về theo đúng thứ tự chạy
    #   lazy="selectin": khi load runbook, SQLAlchemy chạy THÊM 1 query
    #                    "WHERE runbook_id IN (...)" để lấy hết step một lần.
    #                    Nếu để mặc định (lazy="select"), load 50 runbook sẽ
    #                    sinh 51 query -> đó chính là bài toán N+1.
    #                    Với async thì lazy load còn gây lỗi MissingGreenlet,
    #                    nên eager load như thế này là bắt buộc.
    steps: Mapped[list[RunbookStep]] = relationship(
        back_populates="runbook",
        cascade="all, delete-orphan",
        order_by="RunbookStep.order_index",
        lazy="selectin",
    )

    __table_args__ = (
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        CheckConstraint("cooldown_seconds >= 0", name="cooldown_non_negative"),
    )

    def __repr__(self) -> str:
        return f"<Runbook {self.name} steps={len(self.steps)} enabled={self.enabled}>"


class RunbookStep(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Một bước trong runbook. Nhiều step tạo thành quy trình tuần tự."""

    __tablename__ = "runbook_steps"

    # ondelete="CASCADE" là ràng buộc ở TẦNG DB (khác cascade của ORM ở trên).
    # Cần cả hai: ORM lo khi xoá qua session, DB lo khi ai đó xoá bằng SQL tay.
    runbook_id: Mapped[str] = mapped_column(
        ForeignKey("runbooks.id", ondelete="CASCADE"), nullable=False, index=True
    )

    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    # Loại hành động -> quyết định Executor nào sẽ chạy step này.
    action_type: Mapped[ActionType] = mapped_column(enum_column(ActionType), nullable=False)

    # Tham số riêng của từng action, vd:
    #   HTTP_CALL    : {"method":"POST","url":"...","json":{...}}
    #   GRPC_COMMAND : {"action":"RESTART_SERVICE","target":"payment-api"}
    #   WAIT         : {"seconds": 30}
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)

    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    retries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # True -> step này lỗi vẫn chạy tiếp step sau (vd bước NOTIFY không quan trọng).
    continue_on_failure: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Cách hoàn tác nếu các bước sau thất bại (vd đã scale up thì scale down lại).
    rollback_params: Mapped[dict[str, Any]] = mapped_column(default=dict)

    runbook: Mapped[Runbook] = relationship(back_populates="steps")

    __table_args__ = (
        # Trong cùng 1 runbook, order_index phải duy nhất -> không có 2 step
        # cùng thứ tự gây kết quả chạy không xác định.
        UniqueConstraint("runbook_id", "order_index", name="uq_step_order"),
        CheckConstraint("timeout_seconds > 0", name="timeout_positive"),
        CheckConstraint("retries >= 0", name="retries_non_negative"),
    )

    def __repr__(self) -> str:
        return f"<RunbookStep #{self.order_index} {self.name} ({self.action_type})>"
