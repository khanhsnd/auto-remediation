"""
Test RemediationEngine - luồng xử lý end-to-end.

Đây là test tích hợp GIÁ TRỊ NHẤT của project: nó chạy đúng con đường mà
production sẽ đi (ghi nhận -> match -> chạy step -> đổi trạng thái), chỉ
thay Redis bằng fake và dùng SQLite thay Postgres.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models.runbook import Runbook, RunbookStep
from app.db.repositories.execution import ExecutionRepository
from app.db.repositories.incident import IncidentRepository
from app.db.repositories.runbook import RunbookRepository
from app.domain.alert import Alert
from app.domain.enums import (
    ActionType,
    AlertSource,
    ExecutionStatus,
    IncidentStatus,
    Severity,
)
from app.services.incident_service import IncidentService
from app.services.remediation_engine import RemediationEngine


def build_engine(db: AsyncSession, settings: Settings) -> RemediationEngine:
    """
    Lắp engine với đầy đủ repository.

    publisher=None và cache=None: engine đã thiết kế để chạy được khi thiếu
    Kafka/Redis-cache. Nhờ vậy test không cần dựng hạ tầng gì.
    """
    return RemediationEngine(
        IncidentRepository(db),
        RunbookRepository(db),
        ExecutionRepository(db),
        publisher=None,
        cache=None,
        settings=settings,
    )


def make_alert(**kwargs: object) -> Alert:
    defaults: dict[str, object] = {
        "source": AlertSource.PROMETHEUS,
        "external_id": "ext-1",
        "title": "High memory usage",
        "severity": Severity.CRITICAL,
        "service": "payment-api",
        "environment": "production",
        "labels": {"alertname": "HighMemoryUsage"},
    }
    return Alert(**{**defaults, **kwargs})  # type: ignore[arg-type]


async def seed_runbook(
    db: AsyncSession,
    *,
    name: str = "test-rb",
    steps: list[dict[str, object]] | None = None,
    max_attempts: int = 2,
    requires_approval: bool = False,
) -> Runbook:
    """Runbook mặc định: 2 step vô hại (NOOP + WAIT 0s)."""
    specs = steps or [
        {"name": "noop-1", "action_type": ActionType.NOOP, "params": {}},
        {"name": "wait-0", "action_type": ActionType.WAIT, "params": {"seconds": 0}},
    ]
    rb = Runbook(
        name=name,
        priority=10,
        match_rules={},  # catch-all cho gọn test
        max_attempts=max_attempts,
        requires_approval=requires_approval,
    )
    for idx, spec in enumerate(specs):
        rb.steps.append(RunbookStep(order_index=idx, **spec))  # type: ignore[arg-type]
    db.add(rb)
    await db.commit()
    return rb


class TestIngest:
    """Bước 1 của luồng: GHI NHẬN."""

    async def test_ghi_nhan_tao_incident_moi(
        self, db: AsyncSession, settings: Settings, fake_redis: object
    ) -> None:
        service = IncidentService(IncidentRepository(db), publisher=None, settings=settings)
        result = await service.ingest(make_alert())

        assert result.created is True
        assert result.deduplicated is False
        assert result.incident.status is IncidentStatus.RECEIVED
        assert result.incident.service == "payment-api"
        assert result.incident.fingerprint  # đã tính vân tay

    async def test_alert_trung_khong_tao_incident_thu_hai(
        self, db: AsyncSession, settings: Settings, fake_redis: object
    ) -> None:
        """
        ĐÂY LÀ TEST QUAN TRỌNG NHẤT của cơ chế chống trùng.

        Prometheus gửi lại cùng alert mỗi 30 giây. Nếu test này fail thì
        production sẽ có hàng trăm incident trùng và restart service hàng
        trăm lần.
        """
        service = IncidentService(IncidentRepository(db), publisher=None, settings=settings)

        first = await service.ingest(make_alert())
        second = await service.ingest(make_alert())

        assert first.created is True
        assert second.created is False
        assert second.deduplicated is True
        # Quan trọng: vẫn trả về CÙNG incident, không tạo bản mới.
        assert second.incident.id == first.incident.id

        assert await IncidentRepository(db).count() == 1

    async def test_alert_khac_service_thi_tao_incident_rieng(
        self, db: AsyncSession, settings: Settings, fake_redis: object
    ) -> None:
        service = IncidentService(IncidentRepository(db), publisher=None, settings=settings)

        await service.ingest(make_alert(service="payment-api"))
        await service.ingest(make_alert(service="order-api"))

        assert await IncidentRepository(db).count() == 2

    async def test_ghi_audit_log(
        self, db: AsyncSession, settings: Settings, fake_redis: object
    ) -> None:
        """
        Audit log là yêu cầu bắt buộc: phải trả lời được "ai làm gì lúc nào".
        """
        from sqlalchemy import select

        from app.db.models.audit import AuditLog

        service = IncidentService(IncidentRepository(db), publisher=None, settings=settings)
        await service.ingest(make_alert())

        logs = (await db.execute(select(AuditLog))).scalars().all()
        assert len(logs) == 1
        assert logs[0].action == "incident.created"
        assert logs[0].entity_type == "incident"


class TestRemediationHappyPath:
    """Luồng thành công trọn vẹn."""

    async def test_chay_runbook_thanh_cong(
        self, db: AsyncSession, settings: Settings, fake_redis: object
    ) -> None:
        await seed_runbook(db)

        service = IncidentService(IncidentRepository(db), publisher=None, settings=settings)
        ingested = await service.ingest(make_alert())

        engine = build_engine(db, settings)
        report = await engine.process(ingested.incident.id)

        assert report.succeeded is True
        assert report.status is ExecutionStatus.SUCCEEDED
        assert report.steps_total == 2
        assert report.steps_succeeded == 2
        assert report.runbook_name == "test-rb"

    async def test_trang_thai_incident_thanh_resolved(
        self, db: AsyncSession, settings: Settings, fake_redis: object
    ) -> None:
        await seed_runbook(db)
        service = IncidentService(IncidentRepository(db), publisher=None, settings=settings)
        ingested = await service.ingest(make_alert())

        await build_engine(db, settings).process(ingested.incident.id)

        # Đọc lại từ DB để chắc chắn thay đổi đã được commit.
        incident = await IncidentRepository(db).get_or_404(ingested.incident.id)
        assert incident.status is IncidentStatus.RESOLVED
        assert incident.resolved_at is not None
        assert incident.attempt_count == 1

    async def test_luu_lich_su_tung_step(
        self, db: AsyncSession, settings: Settings, fake_redis: object
    ) -> None:
        """
        Lịch sử từng step là thứ người vận hành cần lúc 3h sáng.
        """
        await seed_runbook(db)
        service = IncidentService(IncidentRepository(db), publisher=None, settings=settings)
        ingested = await service.ingest(make_alert())

        await build_engine(db, settings).process(ingested.incident.id)

        executions = await ExecutionRepository(db).list_for_incident(ingested.incident.id)
        assert len(executions) == 1

        execution = executions[0]
        assert execution.status is ExecutionStatus.SUCCEEDED
        assert len(execution.step_results) == 2
        # order_index đảm bảo đúng thứ tự chạy.
        assert [s.order_index for s in execution.step_results] == [0, 1]
        assert all(s.status is ExecutionStatus.SUCCEEDED for s in execution.step_results)


class TestRemediationFailurePath:
    """Luồng thất bại - phần dễ bị bỏ qua nhưng quan trọng nhất khi vận hành."""

    async def test_khong_co_runbook_thi_escalate(
        self, db: AsyncSession, settings: Settings, fake_redis: object
    ) -> None:
        """
        Không có runbook nào -> KHÔNG được im lặng, phải escalate cho người.

        Đây là hành vi đúng: hệ thống tự động biết giới hạn của mình.
        """
        service = IncidentService(IncidentRepository(db), publisher=None, settings=settings)
        ingested = await service.ingest(make_alert())

        report = await build_engine(db, settings).process(ingested.incident.id)

        assert report.succeeded is False
        assert report.status is ExecutionStatus.SKIPPED

        incident = await IncidentRepository(db).get_or_404(ingested.incident.id)
        assert incident.status is IncidentStatus.ESCALATED
        assert incident.last_error  # có ghi lý do

    async def test_runbook_can_duyet_thi_chi_triage(
        self, db: AsyncSession, settings: Settings, fake_redis: object
    ) -> None:
        """
        requires_approval=True -> engine dừng ở TRIAGED, chờ người bấm nút.

        Chốt an toàn cho các hành động nguy hiểm (chạm vào DB, xoá dữ liệu).
        """
        await seed_runbook(db, requires_approval=True)
        service = IncidentService(IncidentRepository(db), publisher=None, settings=settings)
        ingested = await service.ingest(make_alert())

        report = await build_engine(db, settings).process(ingested.incident.id)

        assert report.status is ExecutionStatus.SKIPPED
        assert "cần người duyệt" in report.message

        incident = await IncidentRepository(db).get_or_404(ingested.incident.id)
        assert incident.status is IncidentStatus.TRIAGED
        # Đã chọn được runbook, chỉ chưa chạy.
        assert incident.runbook_id is not None

    async def test_nguoi_bam_nut_thi_chay_du_runbook_can_duyet(
        self, db: AsyncSession, settings: Settings, fake_redis: object
    ) -> None:
        """triggered_by là email người thật -> bỏ qua yêu cầu duyệt."""
        await seed_runbook(db, requires_approval=True)
        service = IncidentService(IncidentRepository(db), publisher=None, settings=settings)
        ingested = await service.ingest(make_alert())

        report = await build_engine(db, settings).process(
            ingested.incident.id, triggered_by="sre@company.com"
        )

        assert report.succeeded is True

    async def test_incident_da_xong_thi_khong_chay_lai(
        self, db: AsyncSession, settings: Settings, fake_redis: object
    ) -> None:
        """
        Idempotent: Kafka có thể gửi lại message (at-least-once).
        Chạy lại runbook trên incident đã RESOLVED là tai hoạ.
        """
        await seed_runbook(db)
        service = IncidentService(IncidentRepository(db), publisher=None, settings=settings)
        ingested = await service.ingest(make_alert())

        engine = build_engine(db, settings)
        first = await engine.process(ingested.incident.id)
        second = await engine.process(ingested.incident.id)

        assert first.succeeded is True
        assert second.status is ExecutionStatus.SKIPPED
        assert "đã ở trạng thái" in second.message

        # Chỉ có 1 execution, không phát sinh lần thứ hai.
        executions = await ExecutionRepository(db).list_for_incident(ingested.incident.id)
        assert len(executions) == 1


class TestStepFailure:
    async def test_step_loi_thi_incident_failed(
        self, db: AsyncSession, settings: Settings, fake_redis: object
    ) -> None:
        """
        HEALTH_CHECK trỏ vào cổng không có ai lắng nghe -> thất bại thật.

        Test này kiểm chứng: engine phát hiện thất bại, KHÔNG báo resolved bừa.
        Cổng 9 là "discard" theo IANA - chắc chắn không có HTTP server.
        """
        await seed_runbook(
            db,
            steps=[
                {
                    "name": "health-that-bai",
                    "action_type": ActionType.HEALTH_CHECK,
                    "params": {"url": "http://127.0.0.1:9/healthz", "retries": 1},
                    "timeout_seconds": 5,
                }
            ],
            max_attempts=1,
        )
        service = IncidentService(IncidentRepository(db), publisher=None, settings=settings)
        ingested = await service.ingest(make_alert())

        # Fixture settings có dry_run=True -> executor sẽ bỏ qua gọi mạng.
        # Test này cần gọi thật để thấy nó fail, nên tắt dry_run.
        # model_copy(update=...) tạo bản sao đã sửa - Settings là immutable.
        real = settings.model_copy(update={"dry_run": False})
        report = await build_engine(db, real).process(ingested.incident.id)

        assert report.succeeded is False
        assert report.status is ExecutionStatus.FAILED

        incident = await IncidentRepository(db).get_or_404(ingested.incident.id)
        # max_attempts=1 và đã thử 1 lần -> escalate luôn cho người.
        assert incident.status is IncidentStatus.ESCALATED

    async def test_continue_on_failure_khong_chan_luong(
        self, db: AsyncSession, settings: Settings, fake_redis: object
    ) -> None:
        """
        Step phụ (vd gửi thông báo) lỗi thì runbook vẫn phải chạy tiếp.

        Nếu không có cờ này, Slack sập là hệ thống mất luôn khả năng tự phục hồi.
        """
        await seed_runbook(
            db,
            steps=[
                {
                    "name": "buoc-phu-loi",
                    "action_type": ActionType.HEALTH_CHECK,
                    "params": {"url": "http://127.0.0.1:9/x", "retries": 1},
                    "timeout_seconds": 5,
                    "continue_on_failure": True,
                },
                {"name": "buoc-chinh", "action_type": ActionType.NOOP, "params": {}},
            ],
        )
        service = IncidentService(IncidentRepository(db), publisher=None, settings=settings)
        ingested = await service.ingest(make_alert())

        real = settings.model_copy(update={"dry_run": False})
        report = await build_engine(db, real).process(ingested.incident.id)

        assert report.succeeded is True
        # 2 step nhưng chỉ 1 thành công - step lỗi được bỏ qua có chủ đích.
        assert report.steps_succeeded == 1


class TestDryRun:
    async def test_dry_run_khong_tac_dong_that(
        self, db: AsyncSession, settings: Settings, fake_redis: object
    ) -> None:
        """
        dry_run=True -> executor chỉ ghi lại ý định, không gọi ra ngoài.

        Tính năng bắt buộc khi viết runbook mới: kiểm tra logic mà không sợ
        làm sập production.
        """
        await seed_runbook(
            db,
            steps=[
                {
                    "name": "goi-api-nguy-hiem",
                    "action_type": ActionType.HTTP_CALL,
                    # URL không tồn tại: nếu dry_run không hoạt động thì
                    # step này chắc chắn fail.
                    "params": {"url": "http://127.0.0.1:9/scale", "method": "POST"},
                    "timeout_seconds": 5,
                }
            ],
        )
        service = IncidentService(IncidentRepository(db), publisher=None, settings=settings)
        ingested = await service.ingest(make_alert())

        report = await build_engine(db, settings).process(ingested.incident.id)

        assert report.succeeded is True

        executions = await ExecutionRepository(db).list_for_incident(ingested.incident.id)
        assert executions[0].step_results[0].output.get("dry_run") is True


class TestConcurrency:
    async def test_lock_chan_worker_thu_hai(
        self, db: AsyncSession, settings: Settings, fake_redis: object
    ) -> None:
        """
        Distributed lock: worker thứ hai phải bỏ qua, không xử lý song song.

        Ở đây giả lập bằng cách chiếm sẵn key lock trong FakeRedis, giống như
        có worker khác đang giữ.
        """
        await seed_runbook(db)
        service = IncidentService(IncidentRepository(db), publisher=None, settings=settings)
        ingested = await service.ingest(make_alert())

        await fake_redis.set(  # type: ignore[attr-defined]
            f"rem:lock:incident:{ingested.incident.id}", "worker-khac", nx=True
        )

        report = await build_engine(db, settings).process(ingested.incident.id)

        assert report.status is ExecutionStatus.SKIPPED
        assert "Worker khác" in report.message


class TestCooldown:
    """
    Cooldown chặn vòng lặp restart -> lỗi lại -> restart.

    Đây là chốt an toàn quan trọng: nguyên nhân gốc chưa được sửa thì restart
    bao nhiêu lần cũng vô nghĩa, mà mỗi lần lại gián đoạn thêm.
    """

    async def test_vua_chay_xong_thi_phai_cho(
        self, db: AsyncSession, settings: Settings, fake_redis: object
    ) -> None:
        # cooldown 600s, max_attempts=3 để không bị chặn bởi lý do khác.
        rb = await seed_runbook(db, max_attempts=3)
        rb.cooldown_seconds = 600
        await db.commit()

        service = IncidentService(IncidentRepository(db), publisher=None, settings=settings)
        ingested = await service.ingest(make_alert())
        incident_id = ingested.incident.id

        engine = build_engine(db, settings)
        first = await engine.process(incident_id)
        assert first.succeeded is True

        # Đưa incident về FAILED để nó còn được phép chạy lại (RESOLVED là
        # trạng thái cuối, sẽ bị chặn bởi lý do khác nên không kiểm tra được
        # cooldown).
        incident = await IncidentRepository(db).get_or_404(incident_id)
        incident.status = IncidentStatus.FAILED
        await db.commit()

        second = await engine.process(incident_id)

        assert second.status is ExecutionStatus.SKIPPED
        assert "cooldown" in second.message

    async def test_cooldown_bang_0_thi_chay_lai_ngay(
        self, db: AsyncSession, settings: Settings, fake_redis: object
    ) -> None:
        """cooldown_seconds=0 -> tắt hoàn toàn cơ chế chờ."""
        rb = await seed_runbook(db, max_attempts=3)
        rb.cooldown_seconds = 0
        await db.commit()

        service = IncidentService(IncidentRepository(db), publisher=None, settings=settings)
        ingested = await service.ingest(make_alert())
        incident_id = ingested.incident.id

        engine = build_engine(db, settings)
        await engine.process(incident_id)

        incident = await IncidentRepository(db).get_or_404(incident_id)
        incident.status = IncidentStatus.FAILED
        await db.commit()

        second = await engine.process(incident_id)
        assert second.succeeded is True
        assert second.status is ExecutionStatus.SUCCEEDED
