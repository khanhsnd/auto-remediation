"""
Schema dùng chung.

KIẾN THỨC PYTHON
  1. Generic model của Pydantic -> Page[IncidentOut], Page[RunbookOut]
  2. ConfigDict để cấu hình model
  3. Field(...) mô tả + ràng buộc, hiện luôn trên Swagger
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    """
    Base cho mọi schema đọc từ ORM object.

    from_attributes=True (Pydantic v2; v1 gọi là orm_mode) cho phép:
        IncidentOut.model_validate(incident_orm_object)
    Không có nó thì Pydantic chỉ nhận dict và sẽ báo lỗi.
    """

    model_config = ConfigDict(from_attributes=True)


class Page[T](BaseModel):
    """
    Kết quả phân trang, dùng chung cho mọi loại dữ liệu.

    Cú pháp generic PEP 695: `class Page[T]` -> dùng Page[IncidentOut].
    Nhờ generic mà không phải viết IncidentPage, RunbookPage... riêng (DRY),
    và Swagger vẫn hiện đúng kiểu của từng endpoint.
    """

    items: list[T]
    total: int = Field(description="Tổng số bản ghi khớp điều kiện lọc")
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        """Còn trang sau không - client dùng để biết có nên gọi tiếp."""
        return self.offset + len(self.items) < self.total


class ErrorResponse(BaseModel):
    """
    Định dạng lỗi THỐNG NHẤT cho toàn API.

    Client chỉ cần viết một hàm xử lý lỗi duy nhất. Nếu mỗi endpoint trả một
    kiểu lỗi khác nhau thì phía frontend sẽ rất khổ.
    """

    error: str = Field(description="Mã lỗi dạng máy đọc được, vd 'not_found'")
    message: str = Field(description="Thông báo cho người đọc")
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str = Field(default="-", description="Dùng để tra log")


class ComponentHealth(BaseModel):
    """Trạng thái một thành phần phụ thuộc."""

    name: str
    healthy: bool
    detail: str = ""


class HealthResponse(BaseModel):
    """
    Kết quả health check.

    PHÂN BIỆT HAI LOẠI PROBE (rất quan trọng khi deploy K8s):
      - liveness : "process còn sống không?" -> chết thì K8s RESTART pod
      - readiness: "sẵn sàng nhận traffic chưa?" -> chưa thì K8s NGỪNG
                   gửi request, nhưng KHÔNG restart
    Nếu để liveness phụ thuộc vào DB thì DB chậm một nhịp là K8s restart
    toàn bộ pod -> mất luôn khả năng phục hồi. Đây là lỗi thực tế rất hay gặp.
    """

    status: str = Field(description="ok | degraded")
    version: str
    environment: str
    timestamp: datetime
    components: list[ComponentHealth] = Field(default_factory=list)
