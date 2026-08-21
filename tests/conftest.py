"""
conftest.py - FastAPI/pytest tự động nạp file này, không cần import.

Fixture khai báo ở đây dùng được ở MỌI file test trong cùng thư mục và
thư mục con.

KIẾN THỨC PYTEST
  1. @pytest.fixture      - tạo dữ liệu/tài nguyên cho test
  2. yield fixture        - có phần dọn dẹp sau khi test xong
  3. scope               - "function" (mặc định, mỗi test một lần) hoặc
                           "session" (cả phiên test chỉ một lần)
  4. monkeypatch         - thay thế hàm/biến tạm thời, tự hoàn nguyên

CHIẾN LƯỢC TEST Ở PROJECT NÀY
  - Dùng SQLite in-memory thay Postgres -> test chạy trong milliseconds,
    không cần docker. Nhờ JsonDict with_variant trong app/db/base.py mà
    model không phải sửa gì.
  - Redis/Kafka/gRPC được thay bằng đối tượng giả (fake) -> test thuần logic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.security import Role, hash_password
from app.db.base import Base
from app.db.models import Runbook, RunbookStep, User  # noqa: F401 - để create_all thấy bảng
from app.domain.enums import ActionType


@pytest.fixture
def settings() -> Settings:
    """
    Settings dùng cho test - trỏ vào SQLite in-memory, tắt Kafka.

    Truyền tham số trực tiếp vào Settings() sẽ ghi đè giá trị đọc từ .env,
    nhờ đó test không phụ thuộc môi trường máy đang chạy.
    """
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        kafka_enabled=False,
        jwt_secret="test-secret-du-dai-toi-thieu-32-byte-cho-hs256",
        app_env="local",
        dry_run=True,
    )


@pytest.fixture
async def db(settings: Settings) -> AsyncIterator[AsyncSession]:
    """
    Session DB trên SQLite in-memory.

    Vì sao mỗi test một engine riêng? Vì ":memory:" của SQLite gắn với từng
    connection - test này không thấy dữ liệu của test kia. Đó chính là điều
    ta muốn: các test HOÀN TOÀN ĐỘC LẬP, chạy thứ tự nào cũng cho kết quả
    như nhau.

    StaticPool giữ đúng một connection để mọi session cùng thấy một DB
    in-memory.
    """
    from sqlalchemy.pool import StaticPool

    engine = create_async_engine(
        settings.database_url,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        # Dọn dẹp: phần sau yield chạy dù test pass hay fail.
        await session.close()
        await engine.dispose()


@pytest.fixture
async def admin_user(db: AsyncSession) -> User:
    """Một user ADMIN đã lưu trong DB."""
    user = User(
        email="admin@test.dev",
        hashed_password=hash_password("test12345"),
        full_name="Test Admin",
        role=Role.ADMIN,
    )
    db.add(user)
    await db.commit()
    return user


@pytest.fixture
async def sample_runbook(db: AsyncSession) -> Runbook:
    """
    Runbook mẫu dùng cho test matcher và engine.

    Dùng NOOP + WAIT để test không gọi ra ngoài mạng.
    """
    runbook = Runbook(
        name="test-restart",
        description="Runbook cho test",
        priority=10,
        match_rules={
            "all": [
                {"field": "service", "op": "eq", "value": "payment-api"},
                {"field": "severity", "op": "in", "value": ["critical", "high"]},
            ]
        },
        max_attempts=2,
    )
    runbook.steps.append(
        RunbookStep(
            order_index=0,
            name="Giả lập restart",
            action_type=ActionType.NOOP,
            params={"note": "test"},
        )
    )
    runbook.steps.append(
        RunbookStep(
            order_index=1,
            name="Chờ 0 giây",
            action_type=ActionType.WAIT,
            params={"seconds": 0},
        )
    )
    db.add(runbook)
    await db.commit()
    return runbook


class FakeRedis:
    """
    Redis giả - chỉ hiện thực đúng những gì code của ta dùng.

    ĐÂY LÀ TEST DOUBLE (dạng "fake"): không phải mock ghi lại lời gọi, mà là
    bản hiện thực đơn giản có HÀNH VI thật. Fake tốt hơn mock ở chỗ test
    kiểm tra được kết quả thật, không chỉ "có gọi hàm hay không".
    """

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        # Mô phỏng sorted set mà rate limiter dùng (chỉ cần đếm số phần tử).
        self._zsets: dict[str, list[str]] = {}

    async def set(self, key: str, value: Any, *, nx: bool = False, ex: int | None = None) -> bool:
        # nx = chỉ set nếu chưa tồn tại -> đúng hành vi Redis thật.
        if nx and key in self._data:
            return False
        self._data[key] = value
        return True

    async def get(self, key: str) -> Any:
        return self._data.get(key)

    async def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self._data.pop(k, None) is not None)

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        """
        Giả lập hai script Lua mà code thật dùng.

        Phân biệt bằng nội dung script - hơi "thô" nhưng đúng tinh thần của
        fake: chỉ mô phỏng vừa đủ để kiểm chứng hành vi, không xây lại Redis.

        Bài học: khi fake một API, hãy fake ĐÚNG KIỂU TRẢ VỀ. Ở đây script
        rate-limit trả về một mảng {allowed, count}; ban đầu fake này trả về
        số 1 và test API lập tức phát hiện ra (TypeError: not subscriptable).
        """
        key = args[0] if args else ""

        # --- script rate limit: trả về [allowed, count] ---
        if "ZADD" in script:
            limit = int(args[3])
            bucket = self._zsets.setdefault(key, [])
            if len(bucket) >= limit:
                return [0, len(bucket)]
            bucket.append(str(args[4]))
            return [1, len(bucket)]

        # --- script unlock: xoá key rồi trả về số key đã xoá ---
        self._data.pop(key, None)
        return 1

    async def ping(self) -> bool:
        return True


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """
    Thay Redis thật bằng FakeRedis trong suốt test.

    monkeypatch.setattr thay thế thuộc tính của module, và TỰ HOÀN NGUYÊN
    khi test kết thúc - không bao giờ rò rỉ sang test khác. Đây là lý do
    nên dùng monkeypatch thay vì tự gán biến global.
    """
    fake = FakeRedis()

    monkeypatch.setattr("app.infra.redis_client._client", fake)
    # BẪY PYTHON QUAN TRỌNG: "from module import ten_ham" tạo ra một tên MỚI
    # trong module đích, trỏ tới cùng object. Patch ở module gốc KHÔNG làm
    # module đích đổi theo, vì nó giữ tham chiếu riêng.
    #   => Quy tắc: PATCH Ở NƠI SỬ DỤNG, không phải nơi định nghĩa.
    for target in (
        "app.infra.redis_client.get_redis",
        "app.services.incident_service.get_redis",
        "app.services.remediation_engine.get_redis",
        "app.api.deps.get_redis",
    ):
        monkeypatch.setattr(target, lambda: fake)
    return fake


@pytest.fixture
async def client(
    db: AsyncSession,
    settings: Settings,
    fake_redis: FakeRedis,
) -> AsyncIterator[Any]:
    """
    HTTP client gọi vào app THẬT, không qua mạng.

    httpx.ASGITransport nối trực tiếp vào ASGI app -> nhanh như gọi hàm,
    nhưng vẫn đi qua đủ middleware, dependency, validation, exception handler.
    Đây là cách test API đúng đắn nhất: gần production mà không cần server.

    LƯU Ý: KHÔNG chạy lifespan -> app không kết nối Postgres/Redis/Kafka thật.
    Thay vào đó ta ghi đè dependency và gán trực tiếp vào app.state.

    dependency_overrides là tính năng vàng của FastAPI: thay bất kỳ
    dependency nào bằng bản test mà KHÔNG sửa một dòng code production.
    """
    import httpx

    from app.api.deps import get_publisher
    from app.core.config import get_settings
    from app.db.session import get_session
    from app.main import create_app

    app = create_app(settings)

    # Kafka/cache tắt trong test.
    app.state.publisher = None
    app.state.cache = None

    # Trả về CHÍNH session của fixture db -> test có thể assert dữ liệu mà
    # endpoint vừa ghi, trong cùng một transaction.
    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_publisher] = lambda: None

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture
def auth_header(settings: Settings):  # noqa: ANN201
    """
    Factory fixture: trả về HÀM tạo header, không phải header cố định.

    Nhờ vậy mỗi test tự chọn role mình cần:
        headers = auth_header(user_id, Role.ADMIN)

    Đây là mẫu "factory as fixture" - rất hay dùng khi test cần nhiều biến thể.
    """
    from app.core.security import create_token

    def _make(user_id: str, role: Role = Role.VIEWER, email: str = "t@test.dev") -> dict[str, str]:
        token = create_token(settings, subject=user_id, email=email, role=role)
        return {"Authorization": f"Bearer {token}"}

    return _make


@pytest.fixture(autouse=True)
def _enable_logging(settings: Settings) -> None:
    """
    autouse=True -> fixture này chạy cho MỌI test, không cần khai báo.

    VÌ SAO BẮT BUỘC PHẢI CÓ? Nếu logging không được cấu hình, root logger ở
    mức WARNING, nên mọi lệnh logger.info() thoát sớm và KHÔNG tạo LogRecord.
    Hậu quả: lỗi trong chính lời gọi log (vd extra dùng key trùng field nội
    bộ của LogRecord) sẽ không bao giờ bị test phát hiện - đúng như bug thật
    mà project này đã gặp.

    Bật INFO ở đây khiến test đi qua cùng đường code như production.
    """
    from app.core.logging import setup_logging

    setup_logging(settings)
