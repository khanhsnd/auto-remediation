"""
Redis - dùng cho 4 việc, mỗi việc là một kỹ thuật đáng học riêng.

  1. CACHE          : giảm tải DB (cache-aside pattern)
  2. IDEMPOTENCY    : chặn xử lý trùng webhook (SET NX)
  3. DISTRIBUTED LOCK: đảm bảo 1 incident chỉ 1 worker xử lý
  4. RATE LIMIT     : chống bên thứ 3 spam alert (sliding window bằng Lua)

KIẾN THỨC PYTHON
  1. redis.asyncio - client async, khác hẳn redis đồng bộ
  2. @asynccontextmanager cho lock -> tự động nhả lock kể cả khi lỗi
  3. Lua script - nhiều lệnh Redis chạy NGUYÊN TỬ trên server
  4. Protocol - định nghĩa interface để service không phụ thuộc Redis
"""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

import redis.asyncio as aioredis

from app.core.config import Settings, get_settings
from app.core.exceptions import RateLimitError
from app.core.logging import get_logger

logger = get_logger(__name__)


class CachePort(Protocol):
    """
    PROTOCOL = interface kiểu "duck typing có kiểm tra".

    Khác với ABC: class không cần kế thừa Protocol này. Chỉ cần nó có đủ
    method với đúng chữ ký là mypy chấp nhận (structural subtyping).

    Nhờ Protocol, service khai báo `cache: CachePort` và trong test ta truyền
    vào một dict giả - không cần Redis, test chạy trong vài milliseconds.
    """

    async def get_json(self, key: str) -> Any | None: ...
    async def set_json(self, key: str, value: Any, *, ttl: int | None = None) -> None: ...
    async def delete(self, *keys: str) -> int: ...


# -----------------------------------------------------------------------------
# Quản lý connection pool
# -----------------------------------------------------------------------------
_client: aioredis.Redis | None = None


def init_redis(settings: Settings | None = None) -> aioredis.Redis:
    """
    Tạo client Redis (dùng chung cho cả app).

    decode_responses=True -> tự decode bytes thành str. Không bật thì mọi
    giá trị trả về đều là b"..." và phải .decode() thủ công khắp nơi.

    from_url tự tạo connection pool bên trong, an toàn khi dùng đồng thời.
    """
    global _client  # noqa: PLW0603

    if _client is not None:
        return _client

    cfg = settings or get_settings()
    _client = aioredis.from_url(
        cfg.redis_url,
        decode_responses=True,
        max_connections=50,
        socket_connect_timeout=3,
        socket_timeout=3,
        health_check_interval=30,
    )
    logger.info("Đã tạo Redis client")
    return _client


def get_redis() -> aioredis.Redis:
    """Lấy client, tự khởi tạo nếu chưa có."""
    return _client if _client is not None else init_redis()


async def close_redis() -> None:
    """Đóng pool khi app shutdown."""
    global _client  # noqa: PLW0603
    if _client is not None:
        await _client.aclose()
        logger.info("Đã đóng Redis client")
    _client = None


# =============================================================================
# 1. CACHE - mẫu "cache-aside"
# =============================================================================
class RedisCache:
    """
    Bọc Redis lại cho tiện dùng. Hiện thực hoá CachePort ở trên.

    Quy ước đặt key: "<app>:<loại>:<id>"  ->  "rem:runbook:abc-123"
    Có tiền tố (namespace) để nhiều app chung một Redis không đụng nhau,
    và để xoá theo nhóm bằng SCAN pattern.
    """

    def __init__(self, client: aioredis.Redis, *, prefix: str = "rem", ttl: int = 60) -> None:
        self._r = client
        self._prefix = prefix
        self._default_ttl = ttl

    def _key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    async def get_json(self, key: str) -> Any | None:
        """
        Đọc và parse JSON. Trả None nếu miss hoặc dữ liệu hỏng.

        Vì sao bắt JSONDecodeError? Vì cache là dữ liệu "có thể bỏ": nếu nó
        hỏng, ta chỉ cần coi như cache miss rồi lấy lại từ DB. Đừng để cache
        hỏng làm sập cả request.
        """
        raw = await self._r.get(self._key(key))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Cache hỏng, bỏ qua", extra={"key": key})
            await self.delete(key)
            return None

    async def set_json(self, key: str, value: Any, *, ttl: int | None = None) -> None:
        """
        Ghi cache kèm TTL.

        LUÔN đặt TTL. Key không TTL sẽ nằm mãi trong RAM: dữ liệu cũ phục vụ
        sai, và Redis đầy dần tới lúc OOM. Đây là lỗi rất phổ biến.
        """
        await self._r.set(
            self._key(key),
            json.dumps(value, ensure_ascii=False, default=str),
            ex=ttl if ttl is not None else self._default_ttl,
        )

    async def delete(self, *keys: str) -> int:
        """*keys (var-positional): xoá nhiều key trong 1 lệnh (round-trip ít hơn)."""
        if not keys:
            return 0
        return int(await self._r.delete(*[self._key(k) for k in keys]))

    async def delete_pattern(self, pattern: str) -> int:
        """
        Xoá theo mẫu, vd "runbook:*" khi có runbook được sửa.

        DÙNG SCAN, KHÔNG DÙNG KEYS. KEYS quét toàn bộ keyspace trong MỘT lệnh
        và Redis là đơn luồng -> nó ĐỨNG luôn với DB lớn. SCAN chia thành
        nhiều lượt nhỏ, không chặn server.

        scan_iter là async generator -> dùng "async for".
        """
        deleted = 0
        async for key in self._r.scan_iter(match=f"{self._prefix}:{pattern}", count=200):
            deleted += int(await self._r.delete(key))
        return deleted

    async def get_or_set(
        self,
        key: str,
        loader: Any,
        *,
        ttl: int | None = None,
    ) -> Any:
        """
        CACHE-ASIDE PATTERN - mẫu cache phổ biến nhất:
            1. Tìm trong cache -> có thì trả về (cache hit)
            2. Không có -> gọi loader() lấy từ DB
            3. Ghi vào cache rồi trả về

        `loader` là một callable async (thường là lambda gọi repository).
        Truyền hàm chứ không truyền giá trị -> chỉ query DB khi thật sự miss
        (đây gọi là "lazy evaluation").
        """
        if (cached := await self.get_json(key)) is not None:
            return cached

        value = await loader()
        if value is not None:
            await self.set_json(key, value, ttl=ttl)
        return value


# =============================================================================
# 2. IDEMPOTENCY - chặn xử lý trùng
# =============================================================================
async def claim_once(client: aioredis.Redis, key: str, *, ttl: int = 300) -> bool:
    """
    Trả True nếu ĐÂY LÀ LẦN ĐẦU thấy key này, False nếu đã thấy rồi.

    SET key value NX EX ttl
        NX = "chỉ set nếu key CHƯA tồn tại"
        EX = tự hết hạn sau ttl giây
    Cả hai gộp trong MỘT lệnh -> nguyên tử, không có kẽ hở race condition.

    ỨNG DỤNG THỰC TẾ: Alertmanager gửi lại cùng một alert mỗi 30 giây. Hàm
    này khiến chỉ lần đầu được xử lý; các lần sau bị bỏ qua trong 5 phút.

    Vì sao không kiểm tra trong DB? Vì DB chậm hơn, và "SELECT rồi INSERT"
    vẫn hở race condition. Redis SET NX là công cụ đúng cho việc này.
    """
    result = await client.set(f"rem:idem:{key}", "1", nx=True, ex=ttl)
    return bool(result)


# =============================================================================
# 3. DISTRIBUTED LOCK
# =============================================================================
# Lua script để NHẢ LOCK an toàn.
# Vì sao cần Lua? Nhả lock gồm 2 bước: (1) kiểm tra token có phải của mình,
# (2) xoá key. Nếu làm bằng 2 lệnh riêng, giữa 2 lệnh đó lock có thể hết hạn
# và người khác giành được -> ta xoá lock CỦA NGƯỜI KHÁC.
# Redis chạy cả script Lua như một lệnh duy nhất -> hết kẽ hở.
_UNLOCK_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


@asynccontextmanager
async def distributed_lock(
    client: aioredis.Redis,
    name: str,
    *,
    ttl: int = 60,
    blocking: bool = False,
    retry_delay: float = 0.2,
    max_wait: float = 5.0,
) -> AsyncIterator[bool]:
    """
    Khoá phân tán: chỉ một tiến trình (trên bất kỳ máy nào) vào được.

    TÌNH HUỐNG THẬT: 3 pod worker cùng nhận message về incident X. Nếu không
    khoá, cả 3 cùng restart service -> restart 3 lần liên tiếp, có thể gây
    thêm sự cố. Lock đảm bảo chỉ 1 pod làm.

    Dùng:
        async with distributed_lock(r, f"incident:{id}") as acquired:
            if not acquired:
                return          # người khác đang làm, mình bỏ qua
            ...xử lý...

    QUAN TRỌNG - token ngẫu nhiên: mỗi lần khoá sinh một token riêng và chỉ
    xoá được lock có đúng token của mình. Nếu không, kịch bản sau xảy ra:
        - Pod A khoá, TTL 60s
        - Pod A xử lý lâu quá 60s -> lock tự hết hạn
        - Pod B khoá được
        - Pod A xong việc, gọi unlock -> xoá mất lock của Pod B!

    [Unverified] Cơ chế này an toàn với một Redis instance. Với Redis cluster
    nhiều master, thuật toán Redlock được đề xuất nhưng vẫn còn tranh luận về
    tính đúng đắn của nó khi có phân vùng mạng.
    """
    import asyncio  # import cục bộ cho gọn phần import đầu file

    key = f"rem:lock:{name}"
    token = secrets.token_hex(16)
    acquired = False
    waited = 0.0

    while True:
        acquired = bool(await client.set(key, token, nx=True, ex=ttl))
        if acquired or not blocking or waited >= max_wait:
            break
        await asyncio.sleep(retry_delay)
        waited += retry_delay

    try:
        # yield ra kết quả để chỗ gọi tự quyết định làm gì khi không lấy được.
        yield acquired
    finally:
        # finally: dù thân hàm có ném lỗi, lock vẫn được nhả.
        # Thiếu dòng này là nguyên nhân số 1 của deadlock trong thực tế.
        if acquired:
            await client.eval(_UNLOCK_LUA, 1, key, token)


# =============================================================================
# 4. RATE LIMIT - cửa sổ trượt (sliding window) bằng Lua
# =============================================================================
# Thuật toán: dùng Sorted Set, score = timestamp.
#   1. ZREMRANGEBYSCORE : xoá các bản ghi cũ hơn cửa sổ
#   2. ZCARD            : đếm số request còn trong cửa sổ
#   3. Nếu chưa vượt    : ZADD thêm request hiện tại
# Cả 3 bước phải nguyên tử, nên gói vào Lua.
#
# So với cách đơn giản INCR + EXPIRE (fixed window): fixed window có lỗ hổng
# "biên" - client gửi 100 request ở giây 59 và 100 request ở giây 61 là lọt
# 200 request trong 2 giây dù giới hạn là 100/phút. Sliding window không bị.
_RATE_LIMIT_LUA = """
local key    = KEYS[1]
local now    = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit  = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)

if count >= limit then
    return {0, count}
end

redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, window)
return {1, count + 1}
"""


async def check_rate_limit(
    client: aioredis.Redis,
    identity: str,
    *,
    limit: int,
    window_seconds: int = 60,
) -> tuple[bool, int]:
    """
    Kiểm tra hạn mức. Trả về (được_phép, số_request_hiện_tại).

    `identity` thường là API key hoặc IP của bên gửi alert.

    Vì sao cần? Một hệ thống monitoring bị cấu hình sai có thể bắn 10.000
    alert/phút. Không có rate limit thì DB và Kafka của ta chết trước.
    """
    import time

    now = time.time()
    member = f"{now}:{secrets.token_hex(4)}"  # đảm bảo member không trùng

    result = await client.eval(
        _RATE_LIMIT_LUA,
        1,                       # số lượng KEYS
        f"rem:rl:{identity}",    # KEYS[1]
        str(now),                # ARGV[1]
        str(window_seconds),     # ARGV[2]
        str(limit),              # ARGV[3]
        member,                  # ARGV[4]
    )
    allowed, current = bool(int(result[0])), int(result[1])
    return allowed, current


async def enforce_rate_limit(
    client: aioredis.Redis,
    identity: str,
    *,
    limit: int,
    window_seconds: int = 60,
) -> None:
    """Bản "cứng": vượt hạn mức thì raise luôn, tiện dùng trong dependency."""
    allowed, current = await check_rate_limit(
        client, identity, limit=limit, window_seconds=window_seconds
    )
    if not allowed:
        raise RateLimitError(
            f"Vượt hạn mức {limit} request/{window_seconds}s (hiện tại {current})",
            retry_after=window_seconds,
        )


async def healthcheck() -> bool:
    """PING Redis - dùng cho endpoint /health."""
    try:
        return bool(await get_redis().ping())
    except Exception as err:  # noqa: BLE001
        logger.warning("Redis không phản hồi", extra={"error": str(err)})
        return False
