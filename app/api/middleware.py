"""
Middleware - code chạy TRƯỚC và SAU mỗi request.

MIDDLEWARE HOẠT ĐỘNG NHƯ THẾ NÀO?
Nó bọc quanh toàn bộ ứng dụng như những lớp vỏ hành:

    request  ->  MW1  ->  MW2  ->  endpoint
    response <-  MW1  <-  MW2  <-  endpoint

`call_next` chính là "phần còn lại của ứng dụng". Code trước call_next chạy
lúc đi vào, code sau nó chạy lúc đi ra.

KHI NÀO DÙNG MIDDLEWARE, KHI NÀO DÙNG DEPENDENCY?
  - Middleware : việc áp dụng cho MỌI request (log, correlation id, CORS)
  - Dependency : việc chỉ cần cho một số endpoint (auth, rate limit)
Dependency còn ưu điểm là xuất hiện trong tài liệu OpenAPI.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.context import REQUEST_ID_VAR, get_request_id, set_request_id
from app.core.logging import get_logger

logger = get_logger(__name__)

# Kiểu của call_next - viết ra cho rõ nghĩa.
CallNext = Callable[[Request], Awaitable[Response]]


class RequestContextMiddleware(BaseHTTPMiddleware):
    """
    Gắn correlation id cho mỗi request và đo thời gian xử lý.

    Nếu client đã gửi X-Request-ID (vd từ API gateway) thì DÙNG LẠI nó, để
    trace xuyên nhiều service. Không có thì tự sinh.
    """

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        incoming = request.headers.get("x-request-id")
        token = set_request_id(incoming)
        # Đọc lại giá trị đã set: nếu client không gửi thì đây là id vừa sinh.
        request_id = get_request_id()

        # Gắn thêm vào request.state.
        # VÌ SAO CẦN CẢ HAI CHỖ? contextvars chỉ sống trong phạm vi task đang
        # chạy. Handler cho `Exception` của Starlette nằm ở ServerErrorMiddleware
        # - NGOÀI middleware này - nên lúc nó chạy, contextvar đã bị reset và
        # request_id sẽ là "-". request.state đi cùng object Request nên luôn
        # đọc được. Đây là lỗi thật project này đã gặp.
        request.state.request_id = request_id

        try:
            response = await call_next(request)
        finally:
            # QUAN TRỌNG: reset contextvar. Không reset thì giá trị có thể
            # rò rỉ sang request khác dùng lại cùng task/context.
            REQUEST_ID_VAR.reset(token)

        # Trả request id về cho client -> khi họ báo lỗi, ta có id để tra log.
        response.headers["X-Request-ID"] = request_id
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    """
    Ghi log mỗi request: method, path, status, thời gian.

    Vì sao tự viết mà không dùng log của uvicorn? Vì ta muốn log có
    request_id và ở dạng JSON để đẩy vào ELK/Loki.
    """

    # Không log health check, nếu không log bị ngập vì K8s probe gọi mỗi 10s.
    _SKIP_PATHS = frozenset({"/health", "/health/live", "/health/ready", "/metrics"})

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        if request.url.path in self._SKIP_PATHS:
            return await call_next(request)

        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start) * 1000

        # Chọn level theo status: 5xx là lỗi của mình, 4xx là lỗi của client.
        if response.status_code >= 500:
            level = logger.error
        elif response.status_code >= 400:
            level = logger.warning
        else:
            level = logger.info

        level(
            "%s %s -> %d (%.1fms)",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round(elapsed_ms, 1),
            },
        )
        response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.1f}"
        return response
