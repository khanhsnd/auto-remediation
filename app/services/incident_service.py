"""
IncidentService - bước "GHI NHẬN" trong luồng nghiệp vụ.

LUỒNG ĐẦY ĐỦ CỦA MỘT ALERT
    1. API nhận webhook (đã xác thực chữ ký HMAC)
    2. normalize            -> Alert chuẩn hoá
    3. fingerprint          -> vân tay chống trùng
    4. Redis SET NX         -> chặn trùng ở tầng nhanh nhất
    5. upsert vào Postgres  -> chặn trùng ở tầng chắc nhất
    6. ghi audit log
    7. publish Kafka        -> worker sẽ lo phần xử lý
    8. trả 202 Accepted     (KHÔNG chờ xử lý xong)

VÌ SAO CHỐNG TRÙNG HAI TẦNG (Redis + DB)?
    Redis nhanh (dưới 1ms) nhưng có thể mất dữ liệu khi restart.
    DB chắc chắn nhưng chậm hơn.
    Redis chặn 99% trường hợp; DB là lưới an toàn cuối cùng.
    Đây là mẫu "defense in depth".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.config import Settings, get_settings
from app.core.context import get_actor, get_request_id
from app.core.logging import get_logger
from app.db.models.audit import AuditLog
from app.db.models.incident import Incident
from app.db.repositories.incident import IncidentRepository
from app.domain.alert import Alert
from app.domain.enums import IncidentStatus
from app.infra.kafka import EventPublisher
from app.infra.redis_client import claim_once, get_redis

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IngestResult:
    """
    Kết quả ghi nhận một alert.

    `deduplicated=True` nghĩa là alert này đã thấy rồi. API vẫn trả 202
    (không phải lỗi) nhưng nói rõ để bên gửi biết.
    """

    incident: Incident
    created: bool
    deduplicated: bool
    message: str


class IncidentService:
    """
    Điều phối việc ghi nhận incident.

    Tất cả phụ thuộc đều nhận qua constructor -> dễ test, dễ thay thế
    (Dependency Inversion trong SOLID).
    """

    def __init__(
        self,
        repo: IncidentRepository,
        publisher: EventPublisher | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._repo = repo
        self._publisher = publisher
        self._cfg = settings or get_settings()

    async def ingest(self, alert: Alert) -> IngestResult:
        """
        Ghi nhận một alert.

        Hàm này phải NHANH (dưới 50ms) vì bên thứ 3 đang chờ. Mọi việc nặng
        đều đẩy sang worker qua Kafka.
        """
        fingerprint = alert.fingerprint()
        redis = get_redis()

        # --- Tầng 1: Redis idempotency -----------------------------------
        # TTL 5 phút: Alertmanager gửi lại mỗi 30s, nên trong 5 phút ta chỉ
        # xử lý 1 lần. Sau 5 phút, nếu vẫn còn alert thì đáng để xem lại.
        is_first = await claim_once(redis, f"alert:{fingerprint}", ttl=300)

        if not is_first:
            # Đã thấy rồi. Vẫn tra DB để trả về incident hiện có cho client.
            existing = await self._repo.get_by_fingerprint(fingerprint)
            if existing is not None:
                logger.info("Alert trùng (Redis), bỏ qua", extra={"fingerprint": fingerprint})
                return IngestResult(
                    existing,
                    created=False,
                    deduplicated=True,
                    message="Alert đã được ghi nhận trước đó",
                )
            # Redis nói "đã thấy" nhưng DB không có -> lần trước xử lý dở
            # (app chết giữa đường). Đi tiếp để tạo cho đủ.
            logger.warning("Redis có key nhưng DB không có incident, tạo lại")

        # --- Tầng 2: upsert vào Postgres ---------------------------------
        # Dùng ON CONFLICT DO NOTHING nên an toàn với race condition.
        incident, created = await self._repo.upsert_by_fingerprint(
            {
                "fingerprint": fingerprint,
                "source": alert.source,
                "external_id": alert.external_id,
                "title": alert.title,
                "description": alert.description,
                "severity": alert.severity,
                "service": alert.service,
                "environment": alert.environment,
                "labels": alert.labels,
                "raw_payload": alert.raw_payload,
                "occurred_at": alert.occurred_at,
                "status": IncidentStatus.RECEIVED,
            }
        )

        # --- Audit log ----------------------------------------------------
        self._repo.session.add(
            AuditLog(
                actor=get_actor(),
                action="incident.created" if created else "incident.duplicate",
                entity_type="incident",
                entity_id=incident.id,
                payload={
                    "fingerprint": fingerprint,
                    "source": str(alert.source),
                    "severity": str(alert.severity),
                    "service": alert.service,
                },
                request_id=get_request_id(),
            )
        )

        # Commit ở SERVICE layer, không phải trong repository. Lý do: một use
        # case có thể ghi nhiều bảng, và tất cả phải cùng thành công hoặc
        # cùng thất bại (Unit of Work).
        await self._repo.session.commit()

        # --- Đẩy sang worker ---------------------------------------------
        # Chỉ publish khi TẠO MỚI, tránh worker xử lý lại incident cũ.
        if created and self._publisher is not None:
            await self._publisher.publish(
                self._cfg.kafka_topic_incidents,
                {
                    "incident_id": incident.id,
                    "fingerprint": fingerprint,
                    "service": incident.service,
                    "severity": str(incident.severity),
                },
                key=incident.id,  # cùng incident -> cùng partition -> đúng thứ tự
                event_type="incident.received",
            )

        logger.info(
            "Đã ghi nhận incident %s (%s/%s)",
            incident.id,
            incident.service,
            incident.severity,
            # CHÚ Ý: không đặt key trùng field nội bộ của LogRecord.
            # "created" là một trong số đó -> logging raise KeyError.
            # Xem danh sách đầy đủ ở _RESERVED trong app/core/logging.py.
            extra={"incident_id": incident.id, "is_new": created},
        )

        return IngestResult(
            incident,
            created=created,
            deduplicated=not created,
            message="Đã ghi nhận" if created else "Incident đã tồn tại",
        )

    async def suppress(self, incident_id: str, *, reason: str) -> Incident:
        """
        Bỏ qua có chủ đích - vd đang trong cửa sổ bảo trì.

        transition_to() sẽ tự chặn nếu trạng thái hiện tại không cho phép.
        """
        incident = await self._repo.get_or_404(incident_id)
        incident.transition_to(IncidentStatus.SUPPRESSED, error=reason)

        self._repo.session.add(
            AuditLog(
                actor=get_actor(),
                action="incident.suppressed",
                entity_type="incident",
                entity_id=incident.id,
                payload={"reason": reason},
                request_id=get_request_id(),
            )
        )
        await self._repo.session.commit()
        return incident

    async def escalate(self, incident_id: str, *, reason: str) -> Incident:
        """
        Bàn giao cho người thật.

        Escalate KHÔNG phải thất bại của hệ thống - đó là hành vi đúng khi
        gặp tình huống ngoài dự kiến. Một hệ thống tự động tốt phải biết
        khi nào nên dừng lại và gọi người.
        """
        incident = await self._repo.get_or_404(incident_id)
        incident.transition_to(IncidentStatus.ESCALATED, error=reason)

        self._repo.session.add(
            AuditLog(
                actor=get_actor(),
                action="incident.escalated",
                entity_type="incident",
                entity_id=incident.id,
                payload={"reason": reason},
                request_id=get_request_id(),
            )
        )
        await self._repo.session.commit()

        if self._publisher is not None:
            await self._publisher.publish(
                self._cfg.kafka_topic_remediation,
                {
                    "incident_id": incident.id,
                    "service": incident.service,
                    "severity": str(incident.severity),
                    "reason": reason,
                    "level": "critical",
                    "message": f"Cần người xử lý: {incident.title}",
                },
                key=incident.id,
                event_type="incident.escalated",
            )

        logger.warning("Escalate incident %s: %s", incident.id, reason)
        return incident

    async def dashboard_stats(self) -> dict[str, Any]:
        """
        Số liệu tổng quan cho dashboard.

        Gom nhiều query vào một hàm service để endpoint chỉ cần gọi 1 lần.
        """
        return {
            "by_status": await self._repo.stats_by_status(within_hours=24),
            "mttr_seconds": await self._repo.mttr_seconds(within_hours=24),
            "flapping_services": await self._repo.find_flapping(),
            "total_24h": await self._repo.count(),
        }
