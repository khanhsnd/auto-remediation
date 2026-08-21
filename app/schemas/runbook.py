"""
Schema cho Runbook.

Điểm đáng học ở file này: cách VALIDATE một DSL (match_rules) do người dùng
nhập vào. Runbook sai luật thì hệ thống sẽ hành động sai trên production,
nên phải chặn ngay lúc tạo.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.domain import rules
from app.domain.enums import ActionType
from app.schemas.common import ORMModel


class RunbookStepIn(BaseModel):
    """Một bước khi tạo/sửa runbook."""

    name: Annotated[str, Field(min_length=1, max_length=128)]
    action_type: ActionType
    params: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: Annotated[int, Field(ge=1, le=600)] = 30
    retries: Annotated[int, Field(ge=0, le=5)] = 0
    continue_on_failure: bool = False
    rollback_params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_required_params(self) -> RunbookStepIn:
        """
        model_validator(mode="after") chạy khi TẤT CẢ field đã hợp lệ.

        Dùng khi phép kiểm tra cần nhiều field cùng lúc - ở đây là "params
        cần khoá gì" phụ thuộc vào "action_type".

        match/case làm bảng tra rất gọn.
        """
        required: tuple[str, ...]
        match self.action_type:
            case ActionType.HTTP_CALL:
                required = ("url",)
            case ActionType.GRPC_COMMAND:
                required = ("action",)
            case ActionType.HEALTH_CHECK:
                required = ("url",)
            case ActionType.NOTIFY:
                required = ("message",)
            case _:
                required = ()

        # Set difference: tìm khoá còn thiếu trong 1 phép toán.
        if missing := set(required) - set(self.params):
            raise ValueError(f"Action {self.action_type} cần các tham số: {sorted(missing)}")
        return self


class RunbookStepOut(ORMModel):
    id: str
    order_index: int
    name: str
    action_type: ActionType
    params: dict[str, Any]
    timeout_seconds: int
    retries: int
    continue_on_failure: bool
    rollback_params: dict[str, Any]


class RunbookIn(BaseModel):
    """
    Tạo runbook mới.

    Đây là API "nguy hiểm" nhất của hệ thống: ai tạo được runbook là điều
    khiển được hạ tầng. Vì vậy endpoint tương ứng yêu cầu quyền ADMIN.
    """

    name: Annotated[str, Field(min_length=3, max_length=128)]
    description: str = ""
    enabled: bool = True
    priority: Annotated[int, Field(ge=1, le=1000)] = 100
    match_rules: dict[str, Any] = Field(default_factory=dict)
    max_attempts: Annotated[int, Field(ge=1, le=10)] = 2
    cooldown_seconds: Annotated[int, Field(ge=0, le=86400)] = 300
    requires_approval: bool = False
    steps: Annotated[list[RunbookStepIn], Field(min_length=1, max_length=30)]

    @field_validator("match_rules")
    @classmethod
    def _validate_rules(cls, v: dict[str, Any]) -> dict[str, Any]:
        """
        Chạy thử luật với dữ liệu giả để chắc chắn nó hợp lệ.

        Kỹ thuật hay: thay vì tự viết lại logic kiểm tra (dễ lệch với engine
        thật), ta gọi ĐÚNG hàm mà engine dùng. Sai op hay sai regex là
        rules.matches() raise ngay tại đây.
        """
        if not v:
            return v  # rules rỗng = catch-all, hợp lệ

        allowed_keys = {"all", "any", "none"}
        if unknown := set(v) - allowed_keys:
            raise ValueError(f"Khoá không hợp lệ: {sorted(unknown)}. Chỉ dùng {allowed_keys}")

        sample = {
            "service": "sample",
            "environment": "production",
            "severity": "high",
            "severity_weight": 3,
            "title": "sample",
            "description": "",
            "source": "custom",
            "labels": {},
        }
        rules.matches(sample, v)  # raise nếu luật sai
        return v

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, v: str) -> str:
        """Chuẩn hoá tên: bỏ khoảng trắng 2 đầu, hạ chữ thường."""
        return v.strip().lower()


class RunbookUpdate(BaseModel):
    """
    Sửa runbook - MỌI field đều optional.

    Mẫu PATCH: chỉ gửi field muốn đổi. Ở tầng API ta dùng
    model_dump(exclude_unset=True) để chỉ lấy field client THỰC SỰ gửi,
    nhờ đó phân biệt được "không đổi" với "đổi thành giá trị rỗng".
    """

    description: str | None = None
    enabled: bool | None = None
    priority: Annotated[int | None, Field(ge=1, le=1000)] = None
    match_rules: dict[str, Any] | None = None
    max_attempts: Annotated[int | None, Field(ge=1, le=10)] = None
    cooldown_seconds: Annotated[int | None, Field(ge=0, le=86400)] = None
    requires_approval: bool | None = None


class RunbookOut(ORMModel):
    id: str
    name: str
    description: str
    enabled: bool
    priority: int
    match_rules: dict[str, Any]
    max_attempts: int
    cooldown_seconds: int
    requires_approval: bool
    steps: list[RunbookStepOut]
    created_at: datetime
    updated_at: datetime


class MatchTestRequest(BaseModel):
    """
    Thử xem một alert giả sẽ khớp runbook nào - KHÔNG chạy gì cả.

    Tính năng nhỏ nhưng cực giá trị: cho phép kiểm tra luật trước khi bật
    runbook trên production. Luôn nên có "dry-run" cho mọi thứ nguy hiểm.
    """

    service: str
    severity: str = "high"
    environment: str = "production"
    title: str = ""
    labels: dict[str, str] = Field(default_factory=dict)


class MatchTestResponse(BaseModel):
    matched: bool
    runbook_name: str | None
    reason: str
    candidates: list[str] = Field(default_factory=list)
