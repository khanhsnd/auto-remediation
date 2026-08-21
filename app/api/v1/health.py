"""
Endpoint health check - K8s và load balancer dựa vào đây.

BA LOẠI PROBE (đừng nhầm lẫn, hậu quả rất khác nhau):
  /health/live  (liveness) : process còn sống? -> chết thì K8s RESTART pod.
                             CHỈ kiểm tra bản thân app, KHÔNG kiểm tra DB.
  /health/ready (readiness): sẵn sàng nhận traffic? -> chưa thì K8s NGỪNG
                             gửi request nhưng KHÔNG restart. Ở đây MỚI
                             kiểm tra DB/Redis.
  /health       (chi tiết) : cho người xem và cho dashboard.

SAI LẦM KINH ĐIỂN: cho liveness phụ thuộc DB. DB nghẽn 30 giây -> K8s
restart toàn bộ pod -> mất hết connection đang có -> DB càng nghẽn -> restart
tiếp. Vòng xoáy tử thần.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Response, status

from app import __version__
from app.api.deps import Config
from app.db import session as db_session
from app.infra import redis_client
from app.infra.grpc import stubs
from app.schemas.common import ComponentHealth, HealthResponse
from app.utils.circuit_breaker import all_breakers

router = APIRouter(tags=["health"])


@router.get("/health/live", summary="Liveness probe")
async def liveness() -> dict[str, str]:
    """
    Chỉ trả OK. Nếu endpoint này không phản hồi thì process đã thật sự chết
    hoặc event loop bị chặn hoàn toàn - lúc đó restart là đúng.
    """
    return {"status": "ok"}


@router.get("/health/ready", summary="Readiness probe")
async def readiness(response: Response) -> dict[str, object]:
    """
    Kiểm tra các phụ thuộc BẮT BUỘC.

    asyncio.gather chạy các health check SONG SONG. Nếu chạy tuần tự, mỗi
    cái timeout 3s thì tổng cộng có thể mất 9s -> vượt quá timeout của probe.
    return_exceptions=True để một cái lỗi không làm cả gather nổ.
    """
    db_ok, redis_ok = await asyncio.gather(
        db_session.healthcheck(),
        redis_client.healthcheck(),
        return_exceptions=True,
    )

    # Kết quả có thể là Exception object -> coi như không khoẻ.
    healthy = db_ok is True and redis_ok is True

    if not healthy:
        # 503 Service Unavailable -> K8s hiểu là "chưa sẵn sàng".
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if healthy else "not_ready",
        "database": db_ok is True,
        "redis": redis_ok is True,
    }


@router.get("/health", response_model=HealthResponse, summary="Health chi tiết")
async def health(cfg: Config) -> HealthResponse:
    """
    Thông tin đầy đủ - dùng cho dashboard vận hành.

    Có thêm trạng thái circuit breaker: nhìn vào là biết dependency nào đang
    bị ngắt, cực kỳ hữu ích khi điều tra sự cố.
    """
    db_ok, redis_ok = await asyncio.gather(
        db_session.healthcheck(),
        redis_client.healthcheck(),
        return_exceptions=True,
    )

    components = [
        ComponentHealth(name="postgres", healthy=db_ok is True),
        ComponentHealth(name="redis", healthy=redis_ok is True),
        ComponentHealth(
            name="grpc_stubs",
            healthy=stubs.is_available(),
            detail="" if stubs.is_available() else "Chưa chạy 'make proto'",
        ),
        ComponentHealth(
            name="kafka",
            healthy=cfg.kafka_enabled,
            detail="" if cfg.kafka_enabled else "Đang tắt qua config",
        ),
    ]

    # Mỗi circuit breaker cũng là một "component" để theo dõi.
    components.extend(
        ComponentHealth(name=f"circuit:{name}", healthy=state == "closed", detail=str(state))
        for name, state in all_breakers().items()
    )

    return HealthResponse(
        # all() với generator: True nếu MỌI component đều khoẻ.
        status="ok" if all(c.healthy for c in components) else "degraded",
        version=__version__,
        environment=str(cfg.app_env),
        timestamp=datetime.now(UTC),
        components=components,
    )
