"""
Exception handler - đổi exception thành HTTP response.

TRIẾT LÝ: tầng service cứ `raise NotFoundError(...)` một cách tự nhiên,
KHÔNG cần biết HTTP là gì. Việc dịch lỗi sang status code nằm hết ở đây.

Lợi ích: service dùng lại được cho worker Kafka và CLI (những nơi không có
HTTP), mà không phải sửa gì.

KIẾN THỨC PYTHON
  1. Bắt exception theo class cha -> một handler xử lý cả cây con
  2. Dùng __class__.status_code lấy từ chính exception
  3. Che thông tin nội bộ khi ở production (bảo mật)
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.context import get_request_id
from app.core.exceptions import AppError, RateLimitError
from app.core.logging import get_logger

logger = get_logger(__name__)


def _request_id(request: Request) -> str:
    """
    Lấy request id, ưu tiên request.state.

    Handler cho `Exception` chạy ở ServerErrorMiddleware - ngoài phạm vi
    RequestContextMiddleware - nên contextvar đã bị reset. request.state đi
    kèm object Request nên luôn còn giá trị.
    """
    return getattr(request.state, "request_id", None) or get_request_id()


def register_error_handlers(app: FastAPI) -> None:
    """
    Đăng ký các handler vào app.

    Gói vào hàm (không viết rải rác) để main.py gọn và để test tạo được app
    riêng với cùng cấu hình lỗi.
    """

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        """
        Xử lý MỌI lỗi trong cây AppError.

        Python bắt exception theo quan hệ kế thừa, nên handler này tự động
        bắt luôn NotFoundError, ConflictError, RemediationError... Thêm loại
        lỗi mới cũng không phải sửa file này.
        """
        # status_code là ClassVar trên từng lớp con -> mỗi loại lỗi tự biết
        # mã HTTP của mình.
        payload = exc.to_dict()
        payload["request_id"] = _request_id(request)

        headers: dict[str, str] = {}
        if isinstance(exc, RateLimitError):
            # Retry-After là header chuẩn (RFC 6585) nói client chờ bao lâu.
            headers["Retry-After"] = str(exc.retry_after)

        # 5xx là bug/sự cố của mình -> log kèm traceback.
        # 4xx là lỗi của client -> chỉ log warning, không cần traceback.
        if exc.status_code >= 500:
            logger.exception("Lỗi hệ thống: %s", exc.message)
        else:
            logger.warning("%s: %s", exc.error_code, exc.message)

        return JSONResponse(status_code=exc.status_code, content=payload, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        """
        Lỗi validate của Pydantic/FastAPI.

        Mặc định FastAPI trả 422 với cấu trúc riêng. Ta chuẩn hoá lại theo
        ErrorResponse để client chỉ phải xử lý MỘT định dạng lỗi.
        """
        # exc.errors() là list dict, mỗi cái có loc/msg/type.
        details = {
            # loc là tuple ("body", "steps", 0, "url") -> ghép thành đường dẫn.
            ".".join(str(part) for part in err["loc"]): err["msg"]
            for err in exc.errors()
        }
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "message": "Dữ liệu gửi lên không hợp lệ",
                "details": details,
                "request_id": _request_id(request),
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """
        Lưới an toàn cuối cùng: lỗi không lường trước (bug thật).

        BẢO MẬT QUAN TRỌNG: ở production KHÔNG trả nội dung lỗi ra ngoài.
        Traceback có thể chứa đường dẫn file, câu SQL, thậm chí giá trị biến
        (có khi là mật khẩu). Chỉ trả request_id để người dùng báo lại, còn
        chi tiết thì nằm trong log của mình.
        """
        logger.exception("Lỗi không mong đợi: %s", exc)

        cfg = get_settings()
        message = (
            "Đã có lỗi xảy ra, vui lòng liên hệ quản trị viên"
            if cfg.is_production
            else f"{type(exc).__name__}: {exc}"
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "message": message,
                "details": {},
                "request_id": _request_id(request),
            },
        )
