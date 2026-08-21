"""
Worker tiêu thụ Kafka và chạy remediation.

Chạy:  python -m app.workers.remediation_worker   (hoặc: make worker)

KIẾN TRÚC
    API  --publish-->  Kafka  --consume-->  Worker  --gRPC/HTTP-->  Hạ tầng

Worker này là nơi việc nặng thực sự diễn ra. Nó có thể chạy nhiều instance
song song: Kafka chia partition, còn distributed lock (Redis) đảm bảo cùng
một incident không bị hai worker xử lý.

KIẾN THỨC PYTHON
  1. async for trên async generator (consume)
  2. Signal handling + graceful shutdown
  3. asyncio.Event làm cờ dừng
  4. Vòng lặp "reconciliation" chạy song song để bù việc bị sót
  5. asyncio.TaskGroup điều phối nhiều vòng lặp cùng lúc
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from app.core.config import Settings, get_settings
from app.core.context import set_actor, set_request_id
from app.core.logging import get_logger, setup_logging
from app.db.repositories.execution import ExecutionRepository
from app.db.repositories.incident import IncidentRepository
from app.db.repositories.runbook import RunbookRepository
from app.db.session import dispose_engine, init_engine, session_scope
from app.infra.http_client import close_http_client, init_http_client
from app.infra.kafka import Event, EventPublisher, consume
from app.infra.redis_client import RedisCache, close_redis, init_redis
from app.services.remediation_engine import RemediationEngine

logger = get_logger(__name__)

# Cờ dừng dùng chung cho mọi vòng lặp trong worker.
_shutdown = asyncio.Event()


async def handle_event(
    event: Event,
    cfg: Settings,
    publisher: EventPublisher,
    cache: RedisCache,
) -> None:
    """
    Xử lý MỘT event từ Kafka.

    Mỗi event dùng một session DB riêng (session_scope) - không dùng chung
    session giữa các event. Vì sao? Nếu dùng chung, một event lỗi làm session
    vào trạng thái rollback, kéo theo mọi event sau đó cũng lỗi.
    """
    incident_id = event.value.get("incident_id")
    if not incident_id:
        logger.warning("Event không có incident_id, bỏ qua: %s", event.value)
        return

    # Lấy lại request_id từ event -> log của worker nối được với log của API.
    # Đây chính là giá trị của việc truyền correlation id qua message.
    set_request_id(event.value.get("request_id"))
    set_actor("system")

    logger.info(
        "Xử lý event %s cho incident %s (partition=%d offset=%d)",
        event.event_type,
        incident_id,
        event.partition,
        event.offset,
    )

    async with session_scope() as db:
        engine = RemediationEngine(
            IncidentRepository(db),
            RunbookRepository(db),
            ExecutionRepository(db),
            publisher,
            cache,
            cfg,
        )
        report = await engine.process(incident_id)

    logger.info("Xong incident %s: %s (%s)", incident_id, report.status, report.message)


async def consume_loop(cfg: Settings, publisher: EventPublisher, cache: RedisCache) -> None:
    """
    Vòng lặp chính: đọc Kafka và xử lý.

    try/except quanh handle_event: MỘT event lỗi không được làm chết worker.
    Nếu để exception bay lên, vòng "async for" dừng và worker thoát -> mọi
    event còn lại bị treo.
    """
    async for event in consume([cfg.kafka_topic_incidents], settings=cfg):
        if _shutdown.is_set():
            logger.info("Đã có tín hiệu dừng, thoát vòng lặp consume")
            break

        try:
            await handle_event(event, cfg, publisher, cache)
        except Exception:  # noqa: BLE001
            # logger.exception in cả traceback - bắt buộc, nếu không ta chỉ
            # thấy "có lỗi" mà không biết ở đâu.
            logger.exception("Xử lý event thất bại, bỏ qua và tiếp tục")


async def reconcile_loop(
    cfg: Settings,
    publisher: EventPublisher,
    cache: RedisCache,
    *,
    interval: int = 60,
) -> None:
    """
    Vòng lặp "bù": quét DB tìm incident bị sót.

    VÌ SAO CẦN? Kafka có thể lỗi lúc API publish (xem EventPublisher.publish:
    ta cố tình không làm sập API khi Kafka lỗi). Khi đó incident đã nằm trong
    DB nhưng không ai xử lý. Vòng lặp này là lưới an toàn.

    Đây là mẫu RECONCILIATION - nền tảng của Kubernetes controller: đừng chỉ
    tin vào event, hãy định kỳ so sánh trạng thái MONG MUỐN với trạng thái
    THỰC TẾ rồi sửa cho khớp.

    asyncio.wait_for với timeout trên _shutdown.wait() = "ngủ interval giây,
    nhưng tỉnh ngay nếu có tín hiệu dừng". Hay hơn asyncio.sleep vì không
    phải chờ hết chu kỳ mới tắt được.
    """
    while not _shutdown.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(_shutdown.wait(), timeout=interval)
        if _shutdown.is_set():
            break

        try:
            async with session_scope() as db:
                repo = IncidentRepository(db)
                pending = await repo.list_pending(limit=10)
                pending_ids = [i.id for i in pending]

            if not pending_ids:
                continue

            logger.info("Reconcile: tìm thấy %d incident chưa xử lý", len(pending_ids))

            async with session_scope() as db:
                engine = RemediationEngine(
                    IncidentRepository(db),
                    RunbookRepository(db),
                    ExecutionRepository(db),
                    publisher,
                    cache,
                    cfg,
                )
                # process_batch chạy song song có giới hạn (Semaphore).
                await engine.process_batch(pending_ids)

        except Exception:  # noqa: BLE001
            logger.exception("Vòng lặp reconcile lỗi")


async def main() -> None:
    """
    Điểm vào của worker.

    Dùng TaskGroup để chạy hai vòng lặp song song. Ưu điểm so với gather:
    nếu một vòng lặp chết vì lỗi không bắt được, TaskGroup tự huỷ vòng còn
    lại và đưa lỗi ra ngoài -> worker chết hẳn để K8s restart, thay vì chạy
    tiếp trong trạng thái nửa vời (đó mới là điều đáng sợ).
    """
    cfg = get_settings()
    setup_logging(cfg)
    logger.info("Khởi động remediation worker (group=%s)", cfg.kafka_consumer_group)

    init_engine(cfg)
    redis = init_redis(cfg)
    init_http_client(cfg)
    cache = RedisCache(redis, ttl=cfg.cache_ttl_seconds)

    publisher = EventPublisher(cfg)
    await publisher.start()

    # Nạp executor vào registry.
    import app.services.executors  # noqa: F401

    # Đăng ký xử lý tín hiệu tắt.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, _shutdown.set)

    try:
        if cfg.kafka_enabled:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(consume_loop(cfg, publisher, cache))
                tg.create_task(reconcile_loop(cfg, publisher, cache))
        else:
            # Kafka tắt -> chỉ chạy vòng lặp quét DB. Chế độ này tiện khi
            # phát triển ở local mà không muốn dựng Kafka.
            logger.warning("Kafka đang tắt, chỉ chạy vòng lặp reconcile")
            await reconcile_loop(cfg, publisher, cache, interval=15)
    finally:
        logger.info("Dọn dẹp worker...")
        await publisher.stop()
        await close_http_client()
        await close_redis()
        await dispose_engine()
        logger.info("Worker đã tắt")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
