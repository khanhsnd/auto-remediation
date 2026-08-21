"""
Model Incident - bản ghi "đã nhận lỗi từ bên thứ 3".

Đây là entity trung tâm của hệ thống: mọi thứ đều quay quanh incident.

KIẾN THỨC
  1. Unique index trên fingerprint -> chống trùng ở TẦNG DB
  2. Index tổ hợp phục vụ đúng câu query hay dùng
  3. Máy trạng thái: transition_to() kiểm tra hợp lệ trước khi đổi status
  4. TYPE_CHECKING -> import chỉ để type hint, tránh import vòng
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.exceptions import ConflictError
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_column, utcnow
from app.domain.enums import ALLOWED_TRANSITIONS, AlertSource, IncidentStatus, Severity

# TYPE_CHECKING chỉ True khi mypy/IDE phân tích, False lúc chạy thật.
# Nhờ đó import được class để type hint mà không tạo import vòng
# (incident -> execution -> incident).
if TYPE_CHECKING:
    from app.db.models.execution import Execution
    from app.db.models.runbook import Runbook


class Incident(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "incidents"

    # ---------------------------------------------------------- định danh
    # fingerprint: vân tay do Alert.fingerprint() tính ra.
    # unique=True là "van an toàn" chống trùng: kể cả 10 pod API nhận cùng
    # một alert đồng thời, DB chỉ cho 1 bản ghi tồn tại. Kiểm tra ở app
    # (SELECT rồi INSERT) KHÔNG bao giờ đủ - đó là race condition kinh điển.
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)

    source: Mapped[AlertSource] = mapped_column(enum_column(AlertSource), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), default="")

    # ------------------------------------------------------------- nội dung
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[Severity] = mapped_column(
        enum_column(Severity, length=16), nullable=False, index=True
    )
    service: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    environment: Mapped[str] = mapped_column(String(32), default="production", index=True)

    labels: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # repr=False không áp dụng cho model ORM, nhưng deferred thì có:
    # raw_payload có thể rất to (vài chục KB), không nên load nếu không cần.
    raw_payload: Mapped[dict[str, Any]] = mapped_column(default=dict)

    # ------------------------------------------------------------ trạng thái
    status: Mapped[IncidentStatus] = mapped_column(
        enum_column(IncidentStatus),
        default=IncidentStatus.RECEIVED,
        nullable=False,
        index=True,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Runbook được chọn. Nullable vì lúc mới nhận chưa biết chạy gì.
    # ondelete="SET NULL": xoá runbook thì incident cũ vẫn còn (giữ lịch sử).
    runbook_id: Mapped[str | None] = mapped_column(
        ForeignKey("runbooks.id", ondelete="SET NULL"), default=None, index=True
    )

    # ------------------------------------------------------------- thời gian
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_error: Mapped[str] = mapped_column(Text, default="")

    # ----------------------------------------------------------- quan hệ
    # Dùng CHUỖI "Runbook" thay vì class: lúc file này chạy, class Runbook
    # có thể chưa được import. SQLAlchemy giải chuỗi này sau, khi configure.
    runbook: Mapped[Runbook | None] = relationship(lazy="selectin")

    executions: Mapped[list[Execution]] = relationship(
        back_populates="incident",
        cascade="all, delete-orphan",
        order_by="Execution.created_at",
        lazy="selectin",
    )

    __table_args__ = (
        # Index tổ hợp cho câu query của dashboard:
        #   WHERE status = 'received' ORDER BY created_at DESC
        # Thứ tự cột trong index rất quan trọng: cột lọc bằng "=" đứng trước,
        # cột sắp xếp đứng sau.
        Index("ix_incident_status_created", "status", "created_at"),
        Index("ix_incident_service_env", "service", "environment"),
        Index("ix_incident_severity_status", "severity", "status"),
    )

    # ------------------------------------------------- máy trạng thái
    def can_transition_to(self, target: IncidentStatus) -> bool:
        """Tra bảng ALLOWED_TRANSITIONS xem bước chuyển này có hợp lệ không."""
        return target in ALLOWED_TRANSITIONS.get(self.status, frozenset())

    def transition_to(self, target: IncidentStatus, *, error: str = "") -> None:
        """
        Đổi trạng thái AN TOÀN.

        Vì sao gói vào method thay vì `incident.status = X` khắp nơi?
          - Chặn bước chuyển vô lý (RESOLVED -> REMEDIATING)
          - Tự set các timestamp liên quan, không bị quên
          - Logic tập trung 1 chỗ, dễ đọc dễ sửa (KISS + DRY)

        Đây chính là tinh thần "encapsulation" của OOP: object tự bảo vệ
        tính đúng đắn của chính nó.
        """
        if target is self.status:
            return  # idempotent: gọi lại cùng trạng thái thì không sao

        if not self.can_transition_to(target):
            raise ConflictError(
                f"Không thể chuyển {self.status} -> {target}",
                details={
                    "current": str(self.status),
                    "target": str(target),
                    "allowed": sorted(str(s) for s in ALLOWED_TRANSITIONS[self.status]),
                },
            )

        self.status = target
        match target:
            case IncidentStatus.RESOLVED:
                self.resolved_at = utcnow()
                self.last_error = ""
            case IncidentStatus.ESCALATED:
                self.escalated_at = utcnow()
                self.last_error = error or self.last_error
            case IncidentStatus.FAILED:
                self.last_error = error
            case IncidentStatus.REMEDIATING:
                self.attempt_count += 1
            case _:
                pass

    def __repr__(self) -> str:
        return f"<Incident {self.service}/{self.title[:30]} {self.status}>"
