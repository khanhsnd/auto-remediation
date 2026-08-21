"""
Cây exception của hệ thống.

VÌ SAO CẦN EXCEPTION RIÊNG?
  - `raise Exception("loi")` thì chỗ gọi không biết bắt cái gì cho đúng.
  - Có cây riêng thì `except AppError` bắt được hết lỗi "của mình",
    còn lỗi lạ (bug thật) vẫn bay lên để ta thấy mà sửa.

KIẾN THỨC PYTHON
  1. Kế thừa nhiều tầng -> phân loại lỗi
  2. ClassVar          -> biến thuộc CLASS, không thuộc instance
  3. __init__ tuỳ biến + super().__init__()
  4. Exception chaining: `raise X from err` để giữ nguyên nguyên nhân gốc
"""

from __future__ import annotations

from typing import Any, ClassVar


class AppError(Exception):
    """
    Lớp gốc của mọi lỗi nghiệp vụ.

    ClassVar nói với type checker: "đây là thuộc tính của class, subclass
    ghi đè được, nhưng đừng coi là field của instance".
    """

    status_code: ClassVar[int] = 500
    error_code: ClassVar[str] = "internal_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        # dấu * bắt buộc `details` phải truyền theo tên (keyword-only).
        # Lợi ích: đọc code chỗ gọi rõ nghĩa, và sau này thêm tham số
        # không sợ vỡ thứ tự đối số.
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        """Chuyển thành JSON để trả về cho client."""
        return {"error": self.error_code, "message": self.message, "details": self.details}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(message={self.message!r}, details={self.details!r})"


# ----------------------------------------------------------------- 4xx client
class NotFoundError(AppError):
    status_code = 404
    error_code = "not_found"


class ValidationError(AppError):
    status_code = 422
    error_code = "validation_error"


class ConflictError(AppError):
    """Trùng dữ liệu, hoặc trạng thái hiện tại không cho phép hành động."""

    status_code = 409
    error_code = "conflict"


class UnauthorizedError(AppError):
    status_code = 401
    error_code = "unauthorized"


class ForbiddenError(AppError):
    """Đã đăng nhập nhưng không đủ quyền."""

    status_code = 403
    error_code = "forbidden"


class RateLimitError(AppError):
    status_code = 429
    error_code = "rate_limited"

    def __init__(self, message: str, *, retry_after: int = 60) -> None:
        super().__init__(message, details={"retry_after": retry_after})
        self.retry_after = retry_after


# ------------------------------------------------------- lỗi domain / hạ tầng
class RemediationError(AppError):
    """Gốc cho mọi lỗi trong quá trình tự phục hồi."""

    status_code = 500
    error_code = "remediation_error"


class RunbookNotMatchedError(RemediationError):
    """Không tìm được runbook nào khớp -> phải escalate cho người thật."""

    error_code = "runbook_not_matched"


class StepExecutionError(RemediationError):
    """Một bước trong runbook chạy thất bại."""

    error_code = "step_failed"

    def __init__(self, step_name: str, reason: str, *, exit_code: int | None = None) -> None:
        super().__init__(
            f"Bước '{step_name}' thất bại: {reason}",
            details={"step": step_name, "reason": reason, "exit_code": exit_code},
        )
        self.step_name = step_name


class StepTimeoutError(StepExecutionError):
    error_code = "step_timeout"

    def __init__(self, step_name: str, timeout: int) -> None:
        # Gọi lên cha để tái dùng logic format message.
        super().__init__(step_name, f"vượt quá {timeout}s")
        self.timeout = timeout


class ApprovalRequiredError(RemediationError):
    """Runbook nguy hiểm, cần người duyệt trước khi chạy."""

    status_code = 202
    error_code = "approval_required"


class CircuitOpenError(RemediationError):
    """Circuit breaker đang mở -> tạm không gọi dependency đó nữa."""

    status_code = 503
    error_code = "circuit_open"


class ExternalServiceError(AppError):
    """Gọi hệ thống ngoài (K8s API, Slack, agent...) bị lỗi."""

    status_code = 502
    error_code = "external_service_error"

    def __init__(self, service: str, message: str, *, status: int | None = None) -> None:
        super().__init__(f"[{service}] {message}", details={"service": service, "status": status})
        self.service = service
