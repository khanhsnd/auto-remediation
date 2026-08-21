"""
Kafka với aiokafka - hàng đợi sự kiện của hệ thống.

VÌ SAO CẦN KAFKA Ở ĐÂY?
Webhook của bên thứ 3 chỉ chờ vài giây rồi timeout. Nhưng việc phục hồi hạ
tầng có thể mất 2-3 phút. Nếu xử lý ngay trong request:
    - Bên gửi timeout -> nó gửi lại -> ta xử lý trùng
    - API bị chiếm dụng, không nhận được alert mới

Cách đúng: API chỉ GHI NHẬN rồi trả 202 Accepted ngay (dưới 50ms), đẩy việc
vào Kafka. Worker chạy riêng mới thực sự xử lý. Đây là "async processing" -
mẫu kiến trúc cơ bản của hệ thống chịu tải cao.

VÌ SAO KAFKA MÀ KHÔNG PHẢI REDIS QUEUE?
  - Kafka LƯU LẠI message (retention) -> worker chết, khởi động lại vẫn đọc
    tiếp từ offset cũ, không mất việc.
  - Partition -> scale worker theo chiều ngang.
  - Cùng một key luôn vào cùng partition -> đảm bảo THỨ TỰ cho mỗi incident.

KIẾN THỨC PYTHON
  1. async generator để consume message
  2. try/finally đảm bảo đóng connection
  3. Serialize/deserialize JSON qua bytes
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from app.core.config import Settings, get_settings
from app.core.context import get_request_id
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Event:
    """
    Một message trên Kafka, đã được bóc tách.

    Đóng gói thành dataclass thay vì truyền dict lung tung: có type hint,
    IDE gợi ý được, và sai tên field là báo lỗi ngay.
    """

    topic: str
    key: str | None
    value: dict[str, Any]
    offset: int
    partition: int

    @property
    def event_type(self) -> str:
        """Loại sự kiện nằm trong payload, dùng để định tuyến ở worker."""
        return str(self.value.get("type", "unknown"))


def _serialize(value: dict[str, Any]) -> bytes:
    """
    dict -> bytes. Kafka chỉ hiểu bytes.

    default=str để datetime/UUID/Enum không làm json.dumps nổ.
    """
    return json.dumps(value, ensure_ascii=False, default=str).encode()


def _deserialize(raw: bytes | None) -> dict[str, Any]:
    """
    bytes -> dict, an toàn với message rác.

    Message hỏng KHÔNG được làm chết worker: chỉ cần log rồi bỏ qua, nếu
    không một message xấu sẽ chặn cả partition mãi mãi (poison pill).
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Message không phải JSON hợp lệ, bỏ qua")
        return {}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


# =============================================================================
# PRODUCER
# =============================================================================
class EventPublisher:
    """
    Bên gửi message.

    Producer nên tạo MỘT lần rồi dùng chung suốt đời app: nó có buffer nội bộ
    và tự gộp message thành batch để gửi hiệu quả. Tạo mới mỗi lần gửi là
    cực kỳ chậm (phải bắt tay lại với broker).
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._cfg = settings or get_settings()
        self._producer: AIOKafkaProducer | None = None

    async def start(self) -> None:
        """
        Kết nối tới Kafka. Gọi khi app khởi động.

        Ý nghĩa tham số:
          acks="all"          : chờ mọi replica xác nhận -> an toàn nhất,
                                không mất message khi broker chết.
          enable_idempotence  : broker tự loại message trùng khi producer
                                retry -> "exactly once" ở phía gửi.
          linger_ms=10        : chờ 10ms để gộp batch -> throughput cao hơn
                                nhiều mà độ trễ thêm không đáng kể.
          compression="gzip"  : payload alert thường là JSON lặp lại nhiều,
                                nén giảm băng thông rất tốt.
        """
        if not self._cfg.kafka_enabled:
            logger.info("Kafka đang tắt (KAFKA_ENABLED=false), bỏ qua producer")
            return

        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._cfg.kafka_servers_list,
            value_serializer=_serialize,
            key_serializer=lambda k: k.encode() if k else None,
            acks="all",
            enable_idempotence=True,
            linger_ms=10,
            compression_type="gzip",
            request_timeout_ms=10_000,
        )
        await self._producer.start()
        logger.info("Kafka producer đã kết nối")

    async def stop(self) -> None:
        """
        Đóng producer - QUAN TRỌNG: stop() sẽ flush buffer trước khi đóng.

        Không gọi stop() thì các message còn trong buffer bị MẤT khi app tắt.
        """
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None
            logger.info("Kafka producer đã đóng")

    async def publish(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        key: str | None = None,
        event_type: str = "",
    ) -> None:
        """
        Gửi một event.

        `key` quyết định PARTITION: cùng key -> cùng partition -> đảm bảo
        thứ tự. Ở đây dùng incident_id làm key, nhờ đó các sự kiện của cùng
        một incident luôn được xử lý theo đúng trình tự.

        Tự nhét request_id vào payload để trace xuyên suốt API -> worker.
        """
        if self._producer is None:
            logger.debug("Producer chưa bật, bỏ qua publish %s", topic)
            return

        enriched = {
            **payload,
            "type": event_type or payload.get("type", "generic"),
            "request_id": get_request_id(),
        }
        try:
            await self._producer.send_and_wait(topic, value=enriched, key=key)
            logger.debug("Đã gửi event %s -> %s", enriched["type"], topic)
        except Exception as err:  # noqa: BLE001
            # Kafka lỗi KHÔNG được làm sập API. Alert đã ghi vào DB rồi, worker
            # có thể quét DB để bù (reconciliation). Ưu tiên: ghi nhận trước.
            logger.error("Gửi Kafka thất bại: %s", err, extra={"topic": topic})


# =============================================================================
# CONSUMER
# =============================================================================
async def consume(
    topics: list[str],
    *,
    settings: Settings | None = None,
    group_id: str | None = None,
) -> AsyncIterator[Event]:
    """
    Async generator sinh ra từng Event.

    Dùng:
        async for event in consume(["incidents.received"]):
            await handle(event)

    VÌ SAO enable_auto_commit=False?
    Auto-commit tự báo "đã xử lý xong" theo chu kỳ thời gian, kể cả khi ta
    CHƯA xử lý xong thật. Nếu worker chết giữa lúc đó -> message bị coi như
    đã xong nhưng thực tế bị mất.
    Commit thủ công SAU khi xử lý xong -> ngữ nghĩa "at-least-once": tệ nhất
    là xử lý lại một message, và ta đã có idempotency (Redis SET NX) để chặn.

    auto_offset_reset="earliest": consumer group mới sẽ đọc từ đầu, không bỏ
    sót message đã có trong topic.
    """
    cfg = settings or get_settings()
    consumer = AIOKafkaConsumer(
        *topics,
        bootstrap_servers=cfg.kafka_servers_list,
        group_id=group_id or cfg.kafka_consumer_group,
        value_deserializer=_deserialize,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        max_poll_records=10,
        # Nếu worker không "điểm danh" trong 45s, Kafka coi là chết và chia
        # lại partition cho worker khác (rebalance).
        session_timeout_ms=45_000,
        heartbeat_interval_ms=10_000,
    )
    await consumer.start()
    logger.info("Kafka consumer đã bật", extra={"topics": topics})

    try:
        # async for trên consumer: aiokafka tự lo poll và buffer.
        async for msg in consumer:
            yield Event(
                topic=msg.topic,
                key=msg.key.decode() if msg.key else None,
                value=msg.value or {},
                offset=msg.offset,
                partition=msg.partition,
            )
            # Commit SAU khi bên ngoài xử lý xong: generator chỉ chạy tiếp
            # dòng này khi vòng "async for" bên ngoài quay lại lấy item mới.
            await consumer.commit()
    finally:
        # finally đảm bảo consumer rời group một cách "lịch sự", giúp Kafka
        # rebalance ngay thay vì phải chờ session timeout.
        await consumer.stop()
        logger.info("Kafka consumer đã đóng")
