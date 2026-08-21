"""
Model Execution + StepResult - lịch sử "đã xử lý thế nào".

Một incident có thể chạy runbook nhiều lần (retry) -> nhiều Execution.
Một Execution chạy nhiều step -> nhiều StepResult.

Bảng này trả lời: "3h sáng hệ thống đã làm gì, mỗi bước mất bao lâu,
bước nào lỗi, log ra sao" - vô cùng quan trọng khi điều tra sự cố.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_column, utcnow
from app.domain.enums import ActionType, ExecutionStatus

if TYPE_CHECKING:
    from app.db.models.incident import Incident


class Execution(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Một lượt chạy runbook cho một incident."""

    __tablename__ = "executions"

    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    runbook_id: Mapped[str | None] = mapped_column(
        ForeignKey("runbooks.id", ondelete="SET NULL"), default=None, index=True
    )
    runbook_name: Mapped[str] = mapped_column(String(128), default="")

    status: Mapped[ExecutionStatus] = mapped_column(
        enum_column(ExecutionStatus),
        default=ExecutionStatus.PENDING,
        nullable=False,
        index=True,
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # Ai kích hoạt: "system" (engine tự chạy) hoặc email người bấm nút.
    triggered_by: Mapped[str] = mapped_column(String(255), default="system")

    # dry_run = chạy thử, log đầy đủ nhưng không tác động thật.
    # Tính năng bắt buộc phải có khi viết runbook mới, tránh "test trên prod".
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    error: Mapped[str] = mapped_column(Text, default="")
    request_id: Mapped[str] = mapped_column(String(64), default="-", index=True)

    incident: Mapped[Incident] = relationship(back_populates="executions")
    step_results: Mapped[list[StepResult]] = relationship(
        back_populates="execution",
        cascade="all, delete-orphan",
        order_by="StepResult.order_index",
        lazy="selectin",
    )

    __table_args__ = (Index("ix_execution_incident_attempt", "incident_id", "attempt"),)

    @property
    def duration_ms(self) -> int | None:
        """
        Thời gian chạy, tính bằng ms. None nếu chưa xong.

        Trừ hai datetime ra timedelta; .total_seconds() trả float giây.
        """
        if self.started_at is None or self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    @property
    def is_finished(self) -> bool:
        return self.status not in {ExecutionStatus.PENDING, ExecutionStatus.RUNNING}

    def mark_started(self) -> None:
        self.status = ExecutionStatus.RUNNING
        self.started_at = utcnow()

    def mark_finished(self, status: ExecutionStatus, *, error: str = "") -> None:
        self.status = status
        self.finished_at = utcnow()
        self.error = error

    def __repr__(self) -> str:
        return f"<Execution {self.runbook_name} attempt={self.attempt} {self.status}>"


class StepResult(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Kết quả của MỘT bước trong một lượt chạy."""

    __tablename__ = "step_results"

    execution_id: Mapped[str] = mapped_column(
        ForeignKey("executions.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Không dùng ForeignKey tới runbook_steps: step có thể bị sửa/xoá sau này,
    # nhưng lịch sử phải bất biến. Vì vậy ta COPY tên và loại action vào đây.
    # Đây là kỹ thuật "snapshot" quan trọng khi lưu dữ liệu lịch sử.
    step_id: Mapped[str | None] = mapped_column(String(36), default=None)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    action_type: Mapped[ActionType] = mapped_column(enum_column(ActionType), nullable=False)

    status: Mapped[ExecutionStatus] = mapped_column(
        enum_column(ExecutionStatus), default=ExecutionStatus.PENDING, nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # Output của executor (stdout, response body, metrics...) dạng JSONB.
    output: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str] = mapped_column(Text, default="")

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)

    execution: Mapped[Execution] = relationship(back_populates="step_results")

    def __repr__(self) -> str:
        return f"<StepResult #{self.order_index} {self.name} {self.status}>"
