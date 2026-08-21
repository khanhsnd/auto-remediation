"""Schema cho Execution / StepResult - lịch sử xử lý."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, computed_field

from app.domain.enums import ActionType, ExecutionStatus
from app.schemas.common import ORMModel


class StepResultOut(ORMModel):
    id: str
    order_index: int
    name: str
    action_type: ActionType
    status: ExecutionStatus
    attempt: int
    output: dict[str, Any]
    error: str
    duration_ms: int
    started_at: datetime | None
    finished_at: datetime | None


class ExecutionOut(ORMModel):
    id: str
    incident_id: str
    runbook_id: str | None
    runbook_name: str
    status: ExecutionStatus
    attempt: int
    triggered_by: str
    dry_run: bool
    error: str
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    step_results: list[StepResultOut] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def duration_ms(self) -> int | None:
        """
        Tính thời gian chạy ngay trong schema.

        @computed_field làm field này xuất hiện trong JSON trả về và cả trong
        tài liệu OpenAPI - client không phải tự tính.
        """
        if self.started_at is None or self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def steps_summary(self) -> str:
        """Tóm tắt nhanh dạng "3/4 bước thành công"."""
        ok = sum(1 for s in self.step_results if s.status is ExecutionStatus.SUCCEEDED)
        return f"{ok}/{len(self.step_results)} bước thành công"


class TriggerRemediationRequest(BaseModel):
    """
    Kích hoạt remediation thủ công.

    Ai dùng? Người vận hành muốn chạy runbook ngay không chờ engine, hoặc
    chạy lại sau khi đã sửa nguyên nhân gốc.
    """

    dry_run: bool = Field(
        default=False,
        description="True = chỉ mô phỏng, không tác động thật vào hạ tầng",
    )
    force: bool = Field(
        default=False,
        description="True = bỏ qua giới hạn max_attempts (dùng cẩn thận)",
    )


class RemediationReportOut(BaseModel):
    """Kết quả một lượt phục hồi."""

    incident_id: str
    execution_id: str | None
    status: ExecutionStatus
    runbook_name: str
    steps_total: int
    steps_succeeded: int
    duration_ms: int
    message: str
