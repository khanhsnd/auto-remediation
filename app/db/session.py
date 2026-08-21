"""
Quản lý engine và session của SQLAlchemy async.

BA KHÁI NIỆM CẦN PHÂN BIỆT RÕ
  1. Engine   : quản lý CONNECTION POOL tới DB. Tạo MỘT lần cho cả app.
                Tạo engine cho mỗi request là lỗi nghiêm trọng (cạn connection).
  2. Session  : "vùng làm việc" cho một đơn vị công việc (unit of work).
                Mỗi request/mỗi message Kafka -> một session, xong thì đóng.
  3. Transaction: session.commit() / rollback(). Có commit mới xuống DB thật.

KIẾN THỨC PYTHON
  1. async generator + yield  -> mẫu dependency của FastAPI (setup/teardown)
  2. @asynccontextmanager     -> biến async generator thành "async with"
  3. try/finally              -> đảm bảo đóng tài nguyên dù có lỗi
  4. Global lazy init         -> tạo tài nguyên nặng đúng một lần
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Biến module-level giữ engine/factory. Dùng `| None` + hàm khởi tạo thay vì
# tạo ngay khi import: import module KHÔNG nên mở kết nối mạng (làm test chậm
# và làm app chết ngay lúc import nếu DB chưa sẵn sàng).
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine(settings: Settings | None = None) -> AsyncEngine:
    """
    Tạo engine (idempotent - gọi nhiều lần vẫn chỉ có một engine).

    Ý nghĩa các tham số pool:
      pool_size      : số connection giữ thường trực
      max_overflow   : số connection tạo thêm khi cao điểm
      pool_pre_ping  : ping trước khi dùng -> tránh lỗi "server closed the
                       connection unexpectedly" khi connection bị firewall
                       hoặc Postgres timeout cắt ngang. Rất nên bật.
      pool_recycle   : tái tạo connection sau N giây, tránh connection quá già
    """
    global _engine, _session_factory  # noqa: PLW0603 - singleton có chủ đích

    if _engine is not None:
        return _engine

    cfg = settings or get_settings()
    _engine = create_async_engine(
        cfg.database_url,
        echo=cfg.db_echo,          # True -> in SQL ra log, rất hữu ích khi học
        pool_size=cfg.db_pool_size,
        max_overflow=cfg.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
        future=True,
    )

    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        # expire_on_commit=False là điểm CỰC KỲ QUAN TRỌNG với async.
        # Mặc định True: sau commit mọi object bị "hết hạn", lần đọc thuộc
        # tính tiếp theo sẽ tự query lại DB. Trong async, việc query ẩn đó
        # xảy ra ngoài await -> lỗi MissingGreenlet rất khó hiểu.
        expire_on_commit=False,
        autoflush=False,  # tự quyết định thời điểm flush -> ít bất ngờ hơn
    )
    logger.info("Đã tạo database engine", extra={"pool_size": cfg.db_pool_size})
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Lấy factory, tự khởi tạo engine nếu chưa có."""
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None  # trợ giúp type checker
    return _session_factory


async def dispose_engine() -> None:
    """
    Đóng toàn bộ connection pool - gọi khi app shutdown.

    Không gọi thì Postgres còn giữ connection "treo" một lúc, và pytest sẽ
    cảnh báo "Task was destroyed but it is pending".
    """
    global _engine, _session_factory  # noqa: PLW0603

    if _engine is not None:
        await _engine.dispose()
        logger.info("Đã đóng database engine")
    _engine = None
    _session_factory = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """
    DEPENDENCY của FastAPI: mỗi request một session.

    Cách hoạt động của "yield dependency":
        - code TRƯỚC yield chạy trước khi vào endpoint
        - giá trị yield ra được inject vào endpoint
        - code SAU yield (finally) chạy sau khi response đã xong
    => đúng mô hình setup/teardown.

    Dùng:  async def handler(db: AsyncSession = Depends(get_session)): ...

    Lưu ý: KHÔNG commit ở đây. Commit là quyết định nghiệp vụ, thuộc về
    service layer. Ở đây chỉ đảm bảo rollback nếu có lỗi và luôn đóng session.
    """
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    except Exception:
        # Lỗi giữa đường -> rollback để session không "kẹt" ở trạng thái lỗi.
        await session.rollback()
        raise  # raise lại để tầng trên (exception handler) xử lý
    finally:
        await session.close()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """
    Session cho code KHÔNG chạy trong FastAPI (worker Kafka, script seed, cron).

    @asynccontextmanager biến async generator thành object dùng được với
    "async with". Nhờ đó viết:

        async with session_scope() as db:
            db.add(obj)
        # tới đây đã tự commit (hoặc rollback nếu lỗi) và đóng session

    Khác get_session ở chỗ: hàm này TỰ COMMIT, vì worker không có ai commit hộ.
    """
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def healthcheck() -> bool:
    """
    Ping DB - dùng cho endpoint /health.

    text("SELECT 1") là câu query rẻ nhất để biết connection còn sống.
    """
    from sqlalchemy import text

    try:
        async with get_session_factory()() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception as err:  # noqa: BLE001 - health check phải bắt hết
        logger.warning("Database không phản hồi", extra={"error": str(err)})
        return False
