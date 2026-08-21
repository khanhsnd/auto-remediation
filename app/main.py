"""
Điểm vào của web API.

Chạy:  uvicorn app.main:app --reload
Swagger: http://localhost:8000/docs

KIẾN THỨC QUAN TRỌNG NHẤT Ở FILE NÀY: LIFESPAN
Đây là nơi tạo/dọn các tài nguyên sống suốt đời ứng dụng (DB pool, Redis
pool, Kafka producer, HTTP client). Nguyên tắc: KHÔNG tạo chúng ở cấp module
(lúc import), vì:
  - Import không nên gây tác dụng phụ ra bên ngoài (mở socket)
  - Test import module sẽ chậm và có thể fail nếu chưa bật hạ tầng
  - Không có chỗ nào để dọn dẹp đúng cách

KIẾN THỨC PYTHON
  1. @asynccontextmanager cho lifespan: code trước yield = startup,
     code sau yield = shutdown
  2. Application factory (create_app) -> test tạo được app riêng biệt
  3. app.state - túi chứa singleton
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.errors import register_error_handlers
from app.api.middleware import AccessLogMiddleware, RequestContextMiddleware
from app.api.v1.router import api_router, health_router
from app.core.config import Settings, get_settings
from app.core.logging import get_logger, setup_logging
from app.db.session import dispose_engine, init_engine
from app.infra.http_client import close_http_client, init_http_client
from app.infra.kafka import EventPublisher
from app.infra.redis_client import RedisCache, close_redis, init_redis

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Vòng đời ứng dụng.

        code ở đây          -> chạy khi STARTUP
        yield               -> app phục vụ request
        code sau yield      -> chạy khi SHUTDOWN

    THỨ TỰ DỌN DẸP phải NGƯỢC với thứ tự khởi tạo (giống stack): thứ tạo
    sau cùng thì đóng trước, vì nó có thể đang phụ thuộc vào thứ tạo trước.
    """
    cfg: Settings = get_settings()
    setup_logging(cfg)

    logger.info(
        "Khởi động %s v%s (env=%s, dry_run=%s)",
        cfg.app_name,
        __version__,
        cfg.app_env,
        cfg.dry_run,
    )

    # --- STARTUP ---------------------------------------------------------
    init_engine(cfg)
    redis = init_redis(cfg)
    init_http_client(cfg)

    publisher = EventPublisher(cfg)
    await publisher.start()

    # Nhét vào app.state để dependency lấy được (xem app/api/deps.py).
    app.state.publisher = publisher
    app.state.cache = RedisCache(redis, ttl=cfg.cache_ttl_seconds)

    # Import package executors một lần để chúng TỰ ĐĂNG KÝ vào registry.
    # Không import ở đâu cả thì registry rỗng và engine không tìm được
    # executor nào - lỗi rất khó đoán.
    #
    # Lưu ý: KHÔNG viết "import app.services.executors" ở đây, vì nó gán lại
    # tên `app` thành module và che mất biến `app` (FastAPI) của file này.
    # Import trực tiếp symbol vừa tránh bẫy đó, vừa vẫn chạy __init__.py của
    # package - nơi mọi executor được import.
    from app.services.executors import Executor

    logger.info(
        "Đã nạp %d executor: %s",
        len(Executor.registry),
        ", ".join(sorted(str(a) for a in Executor.registry)),
    )

    try:
        yield  # <-- app chạy ở đây
    finally:
        # --- SHUTDOWN ----------------------------------------------------
        # finally để dù startup lỗi giữa đường vẫn dọn được phần đã tạo.
        logger.info("Đang tắt ứng dụng...")
        await publisher.stop()
        await close_http_client()
        await close_redis()
        await dispose_engine()
        logger.info("Đã tắt xong")


def create_app(settings: Settings | None = None) -> FastAPI:
    """
    APPLICATION FACTORY - hàm tạo app thay vì tạo trực tiếp ở cấp module.

    VÌ SAO? Trong test, mỗi test case có thể cần một app với config khác
    (DB khác, Kafka tắt). Nếu app là biến global tạo sẵn thì không làm được.
    """
    cfg = settings or get_settings()

    app = FastAPI(
        title="Auto-Remediation System",
        description=(
            "Hệ thống tự phục hồi hạ tầng.\n\n"
            "**Luồng hoạt động**: nhận alert từ bên thứ 3 -> ghi nhận (dedupe) "
            "-> so khớp runbook -> chạy các bước khắc phục -> xác minh -> "
            "báo cáo. Không xử lý được thì escalate cho người."
        ),
        version=__version__,
        lifespan=lifespan,
        # Tắt tài liệu ở production: /docs phơi bày toàn bộ cấu trúc API,
        # là thông tin quý cho attacker.
        docs_url=None if cfg.is_production else "/docs",
        redoc_url=None if cfg.is_production else "/redoc",
        openapi_url=None if cfg.is_production else "/openapi.json",
    )

    # ------------------------------------------------------------ middleware
    # THỨ TỰ QUAN TRỌNG: middleware thêm SAU sẽ chạy TRƯỚC (như xếp chồng).
    # Ta muốn RequestContextMiddleware chạy đầu tiên (để có request_id cho
    # mọi log phía sau) -> thêm nó CUỐI CÙNG.
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestContextMiddleware)

    app.add_middleware(
        CORSMiddleware,
        # Production KHÔNG được dùng "*": nó cho phép mọi website gọi API
        # bằng cookie/token của người dùng. Hãy liệt kê domain cụ thể.
        allow_origins=["*"] if not cfg.is_production else [],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["*"],
    )

    # -------------------------------------------------------- error handlers
    register_error_handlers(app)

    # --------------------------------------------------------------- routes
    app.include_router(health_router)              # /health, /health/live...
    app.include_router(api_router, prefix="/api/v1")

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        """Trang gốc - chỉ để người vào thấy đường tới tài liệu."""
        return {
            "name": cfg.app_name,
            "version": __version__,
            "docs": "/docs" if not cfg.is_production else "disabled",
        }

    return app


# Biến `app` này là thứ uvicorn tìm: `uvicorn app.main:app`
app = create_app()
