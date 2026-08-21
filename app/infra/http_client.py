"""
HTTP client với httpx - gọi API bên ngoài (K8s, Jenkins, Slack, cloud...).

VÌ SAO httpx MÀ KHÔNG PHẢI requests?
requests là ĐỒNG BỘ: một lời gọi requests.get() chặn cả event loop, mọi
request khác của app phải chờ. httpx hỗ trợ async đúng nghĩa.

BA THỨ BẮT BUỘC khi gọi mạng trong hệ thống thật:
  1. TIMEOUT         : không có thì request treo vô hạn
  2. CONNECTION POOL : tái dùng TCP + TLS handshake, nhanh hơn nhiều lần
  3. CIRCUIT BREAKER : dependency sập thì ngắt sớm, không sập theo

KIẾN THỨC PYTHON
  1. Kết hợp nhiều decorator (thứ tự áp dụng từ DƯỚI lên TRÊN)
  2. async with cho client -> tự đóng connection
  3. Kết hợp async context manager (circuit breaker) với request
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger
from app.utils.circuit_breaker import get_breaker
from app.utils.decorators import retry

logger = get_logger(__name__)

_client: httpx.AsyncClient | None = None


def init_http_client(settings: Settings | None = None) -> httpx.AsyncClient:
    """
    Tạo client dùng chung.

    Timeout tách làm nhiều loại - đây là chi tiết hay bị bỏ qua:
      connect : bắt tay TCP (nên ngắn, mạng tốt thì rất nhanh)
      read    : chờ server trả dữ liệu (dài hơn, vì server cần thời gian)
      write   : gửi body đi
      pool    : chờ lấy connection rảnh từ pool

    keepalive_expiry: giữ connection mở 30s để request sau dùng lại, tiết
    kiệm được cả TCP handshake lẫn TLS handshake.
    """
    global _client  # noqa: PLW0603

    if _client is not None:
        return _client

    _ = settings or get_settings()
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0),
        limits=httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=30.0,
        ),
        follow_redirects=True,
        headers={"User-Agent": "auto-remediation/0.1"},
    )
    logger.info("Đã tạo HTTP client")
    return _client


def get_http_client() -> httpx.AsyncClient:
    return _client if _client is not None else init_http_client()


async def close_http_client() -> None:
    global _client  # noqa: PLW0603
    if _client is not None:
        await _client.aclose()
        logger.info("Đã đóng HTTP client")
    _client = None


# Nhiều decorator xếp chồng: áp dụng từ DƯỚI lên.
#   @retry ở dưới -> bọc hàm gốc trước
# Nếu đảo ngược thì retry sẽ chạy ngoài cùng, ý nghĩa khác đi.
@retry(attempts=3, base_delay=0.4, exceptions=(httpx.TransportError,))
async def request_json(
    method: str,
    url: str,
    *,
    service_name: str = "external",
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """
    Gọi HTTP và trả về JSON đã parse.

    Chỉ retry với TransportError (lỗi mạng, DNS, connection reset) - những
    lỗi có khả năng tự khỏi. KHÔNG retry với 4xx: request sai thì gửi lại
    1000 lần vẫn sai, chỉ tốn tài nguyên.

    Trả về dict luôn có "status_code" để chỗ gọi kiểm tra được.
    """
    breaker = get_breaker(service_name, failure_threshold=5, recovery_time=30.0)
    client = get_http_client()

    # async with breaker: tự gọi before_call() lúc vào, on_success/on_failure
    # lúc ra. Toàn bộ logic cầu dao gói trong 1 dòng.
    async with breaker:
        try:
            response = await client.request(
                method.upper(),
                url,
                json=json_body,
                headers=headers,
                params=params,
                timeout=timeout,
            )
        except httpx.TimeoutException as err:
            raise ExternalServiceError(service_name, f"Timeout khi gọi {url}") from err

        # raise_for_status() ném lỗi nếu status >= 400.
        if response.is_error:
            raise ExternalServiceError(
                service_name,
                f"HTTP {response.status_code}: {response.text[:300]}",
                status=response.status_code,
            )

        # Response có thể không phải JSON (vd 204 No Content).
        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text[:2000]}

        return {
            "status_code": response.status_code,
            "body": body,
            "headers": dict(response.headers),
        }


async def healthcheck(url: str, *, expect_status: int = 200, timeout: float = 5.0) -> bool:
    """
    Kiểm tra một endpoint có sống không.

    Dùng bởi executor HEALTH_CHECK: sau khi restart service, ta phải XÁC MINH
    nó đã sống lại. Không xác minh mà báo "resolved" là tự lừa mình.
    """
    try:
        response = await get_http_client().get(url, timeout=timeout)
        return response.status_code == expect_status
    except httpx.HTTPError as err:
        logger.debug("Health check thất bại %s: %s", url, err)
        return False
