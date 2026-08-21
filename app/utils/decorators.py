"""
Decorator tự viết - phần "nâng cao" quan trọng nhất của Python.

DECORATOR LÀ GÌ?
Là một hàm nhận hàm khác và trả về hàm mới. Cú pháp @ chỉ là đường tắt:

    @retry(attempts=3)
    async def f(): ...

    # hoàn toàn tương đương:
    async def f(): ...
    f = retry(attempts=3)(f)

Vì decorator có tham số nên phải có BA tầng hàm:
    retry(attempts)        -> tầng 1: nhận tham số cấu hình
      decorator(func)      -> tầng 2: nhận hàm gốc
        wrapper(*a, **kw)  -> tầng 3: thay thế hàm gốc khi được gọi

KIẾN THỨC ĐI KÈM
  1. functools.wraps  - giữ __name__, __doc__ của hàm gốc (không có nó thì
                        mọi hàm đều tên "wrapper", debug rất khổ)
  2. ParamSpec + TypeVar - giữ nguyên chữ ký hàm cho type checker
  3. Exponential backoff + jitter - chiến lược retry đúng cách
  4. asyncio.timeout  - context manager huỷ việc quá hạn (3.11+)
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from app.core.logging import get_logger

logger = get_logger(__name__)

# P giữ nguyên danh sách tham số của hàm gốc, R giữ kiểu trả về.
# Nhờ 2 cái này, IDE vẫn gợi ý đúng tham số sau khi bọc decorator.
P = ParamSpec("P")
R = TypeVar("R")

# Type alias cho "hàm async" - viết một lần rồi dùng lại.
#
# Vì sao Coroutine chứ không phải Awaitable? Awaitable rộng hơn (mọi thứ
# await được), nhưng asyncio.create_task() CHỈ nhận Coroutine. Khai đúng
# Coroutine giúp type checker bắt lỗi ngay thay vì để nổ lúc chạy.
AsyncFunc = Callable[P, Coroutine[Any, Any, R]]


def retry(
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 10.0,
    backoff: float = 2.0,
    jitter: bool = True,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[AsyncFunc[P, R]], AsyncFunc[P, R]]:
    """
    Thử lại khi lỗi, với thời gian chờ TĂNG DẦN (exponential backoff).

    VÌ SAO PHẢI TĂNG DẦN? Nếu service đích đang quá tải mà ta retry ngay lập
    tức và liên tục, ta đang góp phần dìm nó sâu hơn (retry storm). Chờ tăng
    dần cho nó thời gian hồi phục.

    VÌ SAO CẦN JITTER (nhiễu ngẫu nhiên)? Nếu 100 pod cùng lỗi cùng lúc, tất
    cả sẽ retry ở đúng giây thứ 1, 2, 4... -> tạo ra các đợt sóng đồng bộ đập
    vào service. Cộng thêm số random giúp trải đều các lần retry ra.

    Delay lần thứ n:  min(base_delay * backoff^n, max_delay) * (0.5..1.0)

    Ví dụ dùng:
        @retry(attempts=4, exceptions=(httpx.HTTPError,))
        async def call_api(): ...
    """

    def decorator(func: AsyncFunc[P, R]) -> AsyncFunc[P, R]:
        @wraps(func)  # <- BẮT BUỘC, đừng bao giờ quên dòng này
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            last_error: BaseException | None = None

            # range(attempts) -> 0, 1, 2 : lần thử đầu tính là attempt 0.
            for attempt in range(attempts):
                try:
                    return await func(*args, **kwargs)
                except exceptions as err:
                    last_error = err

                    # Lần cuối rồi thì đừng chờ nữa, ném lỗi ra luôn.
                    if attempt == attempts - 1:
                        break

                    delay = min(base_delay * (backoff**attempt), max_delay)
                    if jitter:
                        # random.uniform(0.5, 1.0): giữ ít nhất 50% thời gian
                        # chờ, tránh trường hợp jitter làm delay gần bằng 0.
                        delay *= random.uniform(0.5, 1.0)  # noqa: S311

                    logger.warning(
                        "Thử lại %s sau %.2fs (lần %d/%d): %s",
                        func.__name__,
                        delay,
                        attempt + 1,
                        attempts,
                        err,
                    )
                    # asyncio.sleep NHƯỜNG quyền cho task khác chạy.
                    # Đừng dùng time.sleep trong async - nó chặn cả event loop,
                    # làm đứng toàn bộ app.
                    await asyncio.sleep(delay)

            # Tới đây tức là hết lượt. `raise` lại lỗi cuối cùng.
            assert last_error is not None
            raise last_error

        return wrapper

    return decorator


def timed(
    threshold_ms: float = 0.0,
) -> Callable[[AsyncFunc[P, R]], AsyncFunc[P, R]]:
    """
    Đo thời gian chạy và log lại nếu vượt ngưỡng.

    time.perf_counter() dùng đồng hồ độ phân giải cao và KHÔNG bị ảnh hưởng
    khi hệ thống chỉnh giờ (NTP). Đo hiệu năng thì luôn dùng nó, đừng dùng
    time.time().
    """

    def decorator(func: AsyncFunc[P, R]) -> AsyncFunc[P, R]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            start = time.perf_counter()
            try:
                return await func(*args, **kwargs)
            finally:
                # finally -> đo được cả khi hàm ném lỗi.
                elapsed_ms = (time.perf_counter() - start) * 1000
                if elapsed_ms >= threshold_ms:
                    logger.info(
                        "%s chạy %.1fms",
                        func.__name__,
                        elapsed_ms,
                        extra={"duration_ms": round(elapsed_ms, 1)},
                    )

        return wrapper

    return decorator


def with_timeout(
    seconds: float,
) -> Callable[[AsyncFunc[P, R]], AsyncFunc[P, R]]:
    """
    Giới hạn thời gian chạy. Quá hạn -> TimeoutError và task bị huỷ.

    asyncio.timeout (3.11+) là context manager, sạch hơn asyncio.wait_for cũ.
    Cơ chế: khi hết hạn, nó gửi CancelledError vào task đang chạy.

    QUY TẮC VÀNG khi làm hệ thống phân tán: MỌI cuộc gọi ra ngoài đều phải
    có timeout. Một request treo vô hạn sẽ giữ connection, giữ RAM, và cuối
    cùng làm cạn pool -> sập cả app.
    """

    def decorator(func: AsyncFunc[P, R]) -> AsyncFunc[P, R]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            async with asyncio.timeout(seconds):
                return await func(*args, **kwargs)

        return wrapper

    return decorator


def singleflight() -> Callable[[AsyncFunc[P, R]], AsyncFunc[P, R]]:
    """
    Gộp các lời gọi TRÙNG NHAU đang diễn ra đồng thời thành một.

    TÌNH HUỐNG: 50 request cùng lúc hỏi "runbook nào khớp alert X?" và cache
    vừa hết hạn -> 50 query DB giống nhau (cache stampede).

    CÁCH LÀM: lần gọi đầu tạo một Task; các lần gọi sau thấy Task đó đang
    chạy thì cùng await nó, không tạo thêm việc.

    Cấu trúc dữ liệu: dict lưu key -> Task đang chạy. Không cần Lock vì
    asyncio chạy đơn luồng, đoạn code giữa 2 lần await là nguyên tử.
    """
    in_flight: dict[str, asyncio.Task[Any]] = {}

    def decorator(func: AsyncFunc[P, R]) -> AsyncFunc[P, R]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            key = f"{func.__name__}:{args!r}:{sorted(kwargs.items())!r}"

            if (existing := in_flight.get(key)) is not None:
                logger.debug("singleflight: dùng chung kết quả đang chạy %s", key)
                return await existing  # type: ignore[no-any-return]

            task: asyncio.Task[R] = asyncio.create_task(func(*args, **kwargs))
            in_flight[key] = task
            try:
                return await task
            finally:
                # finally để dù lỗi cũng dọn key, không thì lần sau bị kẹt
                # với một task đã chết.
                in_flight.pop(key, None)

        return wrapper

    return decorator
