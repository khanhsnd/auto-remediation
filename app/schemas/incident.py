"""Schema cho Incident và webhook đầu vào."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field, field_validator

from app.domain.enums import AlertSource, IncidentStatus, Severity
from app.schemas.common import ORMModel


class GenericAlertIn(BaseModel):
    """
    Định dạng alert "tự do" của mình - dùng cho script nội bộ hoặc test.

    Chú ý: webhook của Prometheus/Datadog KHÔNG dùng schema này (payload của
    họ do họ định nghĩa, ta không sửa được). Chúng đi qua endpoint riêng và
    được normalizer xử lý. Đây là điểm quan trọng khi thiết kế API tích hợp:
    không bắt bên thứ 3 đổi format theo mình.
    """

    title: Annotated[str, Field(min_length=1, max_length=512)]
    service: Annotated[str, Field(min_length=1, max_length=128)]
    severity: Severity = Severity.MEDIUM
    environment: str = "production"
    description: str = ""
    external_id: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    occurred_at: datetime | None = None

    @field_validator("labels")
    @classmethod
    def _limit_labels(cls, v: dict[str, str]) -> dict[str, str]:
        """
        Chặn payload quá lớn.

        Vì sao? Có hệ thống monitoring gửi hàng trăm label. Nhét hết vào JSONB
        làm phình DB và chậm query. Giới hạn 50 là con số thực tế đủ dùng.
        """
        if len(v) > 50:
            raise ValueError("Tối đa 50 label")
        return v


class IncidentOut(ORMModel):
    """Incident trả về cho client."""

    id: str
    fingerprint: str
    source: AlertSource
    external_id: str
    title: str
    description: str
    severity: Severity
    service: str
    environment: str
    status: IncidentStatus
    attempt_count: int
    runbook_id: str | None
    labels: dict[str, Any]
    occurred_at: datetime
    resolved_at: datetime | None
    escalated_at: datetime | None
    last_error: str
    created_at: datetime
    updated_at: datetime

    # raw_payload KHÔNG xuất hiện ở đây: nó có thể chứa thông tin nội bộ của
    # hệ thống monitoring và rất nặng. Muốn xem thì có endpoint riêng.


class IncidentDetailOut(IncidentOut):
    """
    Chi tiết incident - kế thừa IncidentOut rồi thêm field.

    Kế thừa schema là cách rất gọn để làm "nhiều mức chi tiết" mà không
    lặp lại khai báo (DRY).
    """

    raw_payload: dict[str, Any] = Field(default_factory=dict)
    runbook_name: str | None = None


class IngestResponse(BaseModel):
    """
    Trả về khi nhận webhook.

    Trả 202 Accepted (không phải 200 OK) vì việc xử lý CHƯA xong - ta chỉ
    mới ghi nhận. Đây là ngữ nghĩa HTTP đúng cho xử lý bất đồng bộ.
    """

    incident_id: str
    fingerprint: str
    created: bool
    deduplicated: bool
    message: str


class SuppressRequest(BaseModel):
    """Yêu cầu bỏ qua incident."""

    reason: Annotated[str, Field(min_length=3, max_length=500)]


class DashboardStats(BaseModel):
    """Số liệu tổng quan."""

    by_status: dict[str, int]
    mttr_seconds: float | None = Field(
        default=None,
        description="Mean Time To Recovery - trung bình thời gian phục hồi (giây)",
    )
    flapping_services: list[str] = Field(
        default_factory=list,
        description="Service lỗi lặp lại nhiều lần - nên có người xem",
    )
    total_24h: int
