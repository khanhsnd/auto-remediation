"""
RemediationEngine - bước "XỬ LÝ": chạy runbook để tự phục hồi.

TRÁCH NHIỆM
  1. Lấy distributed lock  -> đảm bảo 1 incident chỉ 1 worker xử lý
  2. Match runbook          -> không có thì escalate
  3. Tạo Execution          -> ghi lịch sử
  4. Chạy từng step tuần tự -> có retry + timeout riêng cho mỗi step
  5. Lỗi -> rollback các step đã chạy (theo thứ tự NGƯỢC)
  6. Cập nhật trạng thái incident + publish kết quả

KIẾN THỨC PYTHON NÂNG CAO
  1. asyncio.timeout       - huỷ step chạy quá lâu
  2. asyncio.Semaphore     - giới hạn số việc chạy song song
  3. asyncio.TaskGroup     - chạy nhiều việc song song, lỗi thì huỷ hết
  4. reversed()            - rollback theo thứ tự ngược
  5. Exception hierarchy   - phân biệt lỗi nghiệp vụ và lỗi hệ thống
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from app.core.config import Settings, get_settings
from app.core.context import get_actor, get_request_id
from app.core.logging import get_logger
from app.db.models.audit import AuditLog
from app.db.models.execution import Execution, StepResult
from app.db.models.incident import Incident
from app.db.models.runbook import Runbook, RunbookStep
from app.db.repositories.execution import ExecutionRepository
from app.db.repositories.incident import IncidentRepository
from app.db.repositories.runbook import RunbookRepository
from app.domain.enums import ExecutionStatus, IncidentStatus
from app.infra.kafka import EventPublisher
from app.infra.redis_client import RedisCache, distributed_lock, get_redis
from app.services.executors import ExecutionContext, StepOutcome, get_executor
from app.services.runbook_matcher import RunbookMatcher

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RemediationReport:
    """Tóm tắt một lượt phục hồi - trả về cho API hoặc log ra."""

    incident_id: str
    execution_id: str | None
    status: ExecutionStatus
    runbook_name: str
    steps_total: int
    steps_succeeded: int
    duration_ms: int
    message: str

    @property
    def succeeded(self) -> bool:
        return self.status is ExecutionStatus.SUCCEEDED


class RemediationEngine:
    """
    Bộ máy phục hồi.

    Nhận đủ repository qua constructor. Nhìn danh sách tham số hơi dài, nhưng
    đó là điều TỐT: nó phơi bày rõ mọi phụ thuộc, thay vì ẩn chúng đi bằng
    import global (khi đó test sẽ rất khổ).
    """

    def __init__(
        self,
        incident_repo: IncidentRepository,
        runbook_repo: RunbookRepository,
        execution_repo: ExecutionRepository,
        publisher: EventPublisher | None = None,
        cache: RedisCache | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._incidents = incident_repo
        self._runbooks = runbook_repo
        self._executions = execution_repo
        self._publisher = publisher
        self._cfg = settings or get_settings()
        # Truyền execution_repo để matcher kiểm tra được cooldown.
        self._matcher = RunbookMatcher(runbook_repo, cache, execution_repo)

        # Semaphore giới hạn số remediation chạy song song.
        # VÌ SAO CẦN? Nếu 100 service sập cùng lúc, chạy 100 runbook song
        # song sẽ làm cạn connection pool DB và đập chết luôn K8s API.
        # Semaphore là "giấy phép": hết giấy thì task phải chờ.
        self._semaphore = asyncio.Semaphore(self._cfg.max_concurrent_remediations)

    # =========================================================================
    # Điểm vào chính
    # =========================================================================
    async def process(
        self, incident_id: str, *, triggered_by: str | None = None
    ) -> RemediationReport:
        """
        Xử lý một incident từ đầu tới cuối.

        DISTRIBUTED LOCK: nhiều worker có thể nhận cùng message (Kafka bảo
        đảm at-least-once). Lock đảm bảo chỉ một worker thực sự làm việc.
        Worker khác thấy lock là bỏ qua ngay, không chờ (blocking=False).
        """
        redis = get_redis()

        # Gộp hai context manager vào một câu "async with":
        #   - semaphore giới hạn số việc chạy song song
        #   - lock đảm bảo mỗi incident chỉ một worker xử lý
        async with (
            self._semaphore,
            distributed_lock(redis, f"incident:{incident_id}", ttl=600) as acquired,
        ):
            if not acquired:
                logger.info("Incident %s đang được worker khác xử lý", incident_id)
                return RemediationReport(
                    incident_id=incident_id,
                    execution_id=None,
                    status=ExecutionStatus.SKIPPED,
                    runbook_name="",
                    steps_total=0,
                    steps_succeeded=0,
                    duration_ms=0,
                    message="Worker khác đang xử lý",
                )
            return await self._process_locked(incident_id, triggered_by or "system")

    async def _process_locked(self, incident_id: str, triggered_by: str) -> RemediationReport:
        """
        Phần xử lý thật, đã nắm lock.

        Tách hàm để logic lock và logic nghiệp vụ không lẫn vào nhau -
        mỗi hàm một trách nhiệm (Single Responsibility).
        """
        incident = await self._incidents.get_or_404(incident_id)

        # Trạng thái cuối rồi thì không làm gì nữa (idempotent).
        if incident.status.is_terminal:
            return self._skip_report(incident, f"Incident đã ở trạng thái {incident.status}")

        # --- Bước 1: match runbook ---------------------------------------
        match = await self._matcher.match(incident)

        if not match.matched or match.runbook is None:
            # Không có runbook -> escalate cho người. Đây là hành vi ĐÚNG.
            incident.transition_to(IncidentStatus.ESCALATED, error=match.reason)
            await self._audit(incident.id, "remediation.escalated", {"reason": match.reason})
            await self._incidents.session.commit()
            await self._publish_result(incident, None, match.reason)
            return self._skip_report(incident, match.reason)

        runbook = match.runbook

        # --- Bước 2: kiểm tra cần duyệt tay không -------------------------
        if runbook.requires_approval and triggered_by == "system":
            incident.transition_to(IncidentStatus.TRIAGED)
            incident.runbook_id = runbook.id
            await self._audit(
                incident.id,
                "remediation.awaiting_approval",
                {"runbook": runbook.name},
            )
            await self._incidents.session.commit()
            return self._skip_report(
                incident, f"Runbook '{runbook.name}' cần người duyệt trước khi chạy"
            )

        # --- Bước 3: chuyển trạng thái và tạo Execution -------------------
        if incident.status is IncidentStatus.RECEIVED:
            incident.transition_to(IncidentStatus.TRIAGED)
        incident.runbook_id = runbook.id
        incident.transition_to(IncidentStatus.REMEDIATING)  # tự tăng attempt_count

        execution = Execution(
            incident_id=incident.id,
            runbook_id=runbook.id,
            runbook_name=runbook.name,
            attempt=incident.attempt_count,
            triggered_by=triggered_by,
            dry_run=self._cfg.dry_run,
            request_id=get_request_id(),
        )
        execution.mark_started()
        self._executions.add(execution)

        # flush để lấy execution.id (cần cho ExecutionContext) mà chưa commit.
        await self._executions.flush()

        await self._incidents.session.commit()

        # --- Bước 4: chạy các step ---------------------------------------
        return await self._run_steps(incident, runbook, execution)

    # =========================================================================
    # Chạy các step
    # =========================================================================
    async def _run_steps(
        self,
        incident: Incident,
        runbook: Runbook,
        execution: Execution,
    ) -> RemediationReport:
        """
        Chạy tuần tự từng step. Lỗi ở step quan trọng -> dừng và rollback.

        VÌ SAO TUẦN TỰ MÀ KHÔNG SONG SONG? Vì các bước phục hồi có thứ tự
        nhân quả: phải scale up TRƯỚC khi drain node, phải chờ TRƯỚC khi
        health check. Chạy song song sẽ sai về mặt nghiệp vụ.
        """
        ctx = ExecutionContext(
            incident_id=incident.id,
            execution_id=execution.id,
            service=incident.service,
            environment=incident.environment,
            severity=str(incident.severity),
            labels={str(k): str(v) for k, v in (incident.labels or {}).items()},
            dry_run=self._cfg.dry_run,
            attempt=execution.attempt,
            # Truyền publisher dùng chung xuống cho step NOTIFY,
            # thay vì để executor tự tạo connection mới mỗi lần.
            publisher=self._publisher,
        )

        started = time.perf_counter()
        succeeded = 0
        # Lưu các step đã chạy XONG để rollback nếu cần.
        completed: list[RunbookStep] = []
        failure_error = ""

        for step in runbook.steps:
            outcome = await self._run_one_step(step, ctx, execution)

            if outcome.success:
                succeeded += 1
                completed.append(step)
                # Lưu output để step sau dùng: ctx.outputs["Restart pod"]["stdout"]
                ctx.outputs[step.name] = outcome.output
                continue

            # --- step thất bại ---
            if step.continue_on_failure:
                # Step không quan trọng (vd NOTIFY) -> ghi nhận rồi đi tiếp.
                logger.warning("Step '%s' lỗi nhưng được phép bỏ qua", step.name)
                continue

            failure_error = outcome.error or f"Step '{step.name}' thất bại"
            logger.error("Dừng runbook tại step '%s': %s", step.name, failure_error)
            break

        duration_ms = int((time.perf_counter() - started) * 1000)

        # --- Kết luận ------------------------------------------------------
        if failure_error:
            # Rollback theo thứ tự NGƯỢC: bước làm sau phải hoàn tác trước.
            await self._rollback(completed, ctx)

            execution.mark_finished(ExecutionStatus.FAILED, error=failure_error)
            incident.transition_to(IncidentStatus.FAILED, error=failure_error)

            # Hết lượt thử -> escalate luôn cho người.
            if incident.attempt_count >= runbook.max_attempts:
                incident.transition_to(
                    IncidentStatus.ESCALATED,
                    error=f"Thất bại sau {incident.attempt_count} lần: {failure_error}",
                )

            status = ExecutionStatus.FAILED
            message = failure_error
        else:
            execution.mark_finished(ExecutionStatus.SUCCEEDED)
            incident.transition_to(IncidentStatus.RESOLVED)
            status = ExecutionStatus.SUCCEEDED
            message = f"Đã phục hồi bằng runbook '{runbook.name}'"

        await self._audit(
            incident.id,
            "remediation.finished",
            {
                "execution_id": execution.id,
                "runbook": runbook.name,
                "status": str(status),
                "steps_succeeded": succeeded,
                "steps_total": len(runbook.steps),
                "duration_ms": duration_ms,
            },
        )
        await self._incidents.session.commit()
        await self._publish_result(incident, execution, message)

        logger.info(
            "Kết thúc remediation incident=%s status=%s (%d/%d step, %dms)",
            incident.id,
            status,
            succeeded,
            len(runbook.steps),
            duration_ms,
        )

        return RemediationReport(
            incident_id=incident.id,
            execution_id=execution.id,
            status=status,
            runbook_name=runbook.name,
            steps_total=len(runbook.steps),
            steps_succeeded=succeeded,
            duration_ms=duration_ms,
            message=message,
        )

    async def _run_one_step(
        self,
        step: RunbookStep,
        ctx: ExecutionContext,
        execution: Execution,
    ) -> StepOutcome:
        """
        Chạy MỘT step, có retry và timeout riêng.

        Mỗi lần thử đều được ghi vào bảng step_results -> nhìn lịch sử biết
        chính xác thử mấy lần, mỗi lần lỗi gì.
        """
        from app.db.base import utcnow

        executor = get_executor(step.action_type)
        # retries=2 nghĩa là thử tổng 3 lần (1 lần đầu + 2 lần lại).
        total_attempts = step.retries + 1
        outcome = StepOutcome.fail("chưa chạy")

        for attempt in range(1, total_attempts + 1):
            record = StepResult(
                execution_id=execution.id,
                step_id=step.id,
                order_index=step.order_index,
                name=step.name,
                action_type=step.action_type,
                status=ExecutionStatus.RUNNING,
                attempt=attempt,
                started_at=utcnow(),
            )
            self._executions.session.add(record)

            step_started = time.perf_counter()
            try:
                # asyncio.timeout: nếu vượt hạn, một CancelledError được gửi
                # vào coroutine bên trong -> nó bị huỷ và ta bắt TimeoutError.
                async with asyncio.timeout(step.timeout_seconds):
                    outcome = await executor.run(ctx, step.params or {})

            except TimeoutError:
                outcome = StepOutcome.fail(f"Timeout sau {step.timeout_seconds}s")
                record.status = ExecutionStatus.TIMEOUT

            except Exception as err:  # noqa: BLE001
                # Bắt rộng ở ĐÂY là hợp lý: một executor lỗi bất ngờ không
                # được phép làm sập cả worker. Nhưng phải log đầy đủ
                # (logger.exception in cả traceback) để không mất dấu bug.
                logger.exception("Executor %s lỗi bất ngờ", step.action_type)
                outcome = StepOutcome.fail(f"{type(err).__name__}: {err}")

            duration = int((time.perf_counter() - step_started) * 1000)
            record.duration_ms = duration
            record.finished_at = utcnow()
            record.output = outcome.output
            record.error = outcome.error

            if record.status is not ExecutionStatus.TIMEOUT:
                record.status = (
                    ExecutionStatus.SUCCEEDED if outcome.success else ExecutionStatus.FAILED
                )

            await self._executions.session.commit()

            if outcome.success:
                logger.info("Step '%s' OK (%dms)", step.name, duration)
                return outcome

            if attempt < total_attempts:
                # Backoff nhẹ giữa các lần thử trong cùng một step.
                delay = min(2.0 * attempt, 10.0)
                logger.warning(
                    "Step '%s' lỗi (%d/%d): %s -> thử lại sau %.1fs",
                    step.name,
                    attempt,
                    total_attempts,
                    outcome.error,
                    delay,
                )
                await asyncio.sleep(delay)

        return outcome

    async def _rollback(self, completed: list[RunbookStep], ctx: ExecutionContext) -> None:
        """
        Hoàn tác các step đã chạy, theo thứ tự NGƯỢC.

        VÍ DỤ: runbook làm [scale up 10 replica] -> [drain node] -> [health check].
        Health check fail. Rollback phải: [undrain node] rồi mới [scale về cũ].
        Làm ngược thứ tự thì hệ thống rơi vào trạng thái còn tệ hơn ban đầu.

        reversed() trả về iterator đi từ cuối lên đầu, không copy list.

        Rollback lỗi thì chỉ LOG, không raise: ta đang ở trong đường xử lý
        lỗi rồi, ném thêm lỗi nữa sẽ che mất nguyên nhân gốc.
        """
        if not completed:
            return

        logger.info("Bắt đầu rollback %d step", len(completed))

        for step in reversed(completed):
            if not step.rollback_params:
                continue  # step này không khai báo cách hoàn tác

            try:
                executor = get_executor(step.action_type)
                outcome = await executor.rollback(
                    ctx, {**(step.params or {}), "rollback_params": step.rollback_params}
                )
                logger.info(
                    "Rollback step '%s': %s",
                    step.name,
                    "OK" if outcome.success else outcome.error,
                )
            except Exception:  # noqa: BLE001
                logger.exception("Rollback step '%s' thất bại", step.name)

    # =========================================================================
    # Xử lý theo lô (batch) - chạy song song có kiểm soát
    # =========================================================================
    async def process_batch(self, incident_ids: list[str]) -> list[RemediationReport]:
        """
        Xử lý nhiều incident SONG SONG.

        asyncio.TaskGroup (Python 3.11+) hơn asyncio.gather ở chỗ:
          - Một task lỗi -> tự HUỶ các task còn lại (structured concurrency)
          - Lỗi được gom thành ExceptionGroup, không mất lỗi nào
          - Không thể "quên" await task

        Semaphore trong process() vẫn giới hạn số việc chạy thật cùng lúc,
        nên truyền 100 id vào đây cũng an toàn.

        Ở đây bắt lỗi từng task riêng để một incident lỗi không huỷ cả lô -
        đó là lựa chọn có ý thức, vì các incident độc lập với nhau.
        """
        results: list[RemediationReport] = []

        async def _one(incident_id: str) -> None:
            try:
                results.append(await self.process(incident_id))
            except Exception as err:  # noqa: BLE001
                logger.exception("Xử lý incident %s lỗi", incident_id)
                results.append(
                    RemediationReport(
                        incident_id=incident_id,
                        execution_id=None,
                        status=ExecutionStatus.FAILED,
                        runbook_name="",
                        steps_total=0,
                        steps_succeeded=0,
                        duration_ms=0,
                        message=f"{type(err).__name__}: {err}",
                    )
                )

        async with asyncio.TaskGroup() as tg:
            for incident_id in incident_ids:
                tg.create_task(_one(incident_id))
        # Ra khỏi "async with" là chắc chắn mọi task đã xong.

        return results

    # =========================================================================
    # Helper nội bộ
    # =========================================================================
    async def _audit(self, incident_id: str, action: str, payload: dict[str, object]) -> None:
        """Ghi audit log. Không commit - để chỗ gọi quyết định."""
        self._incidents.session.add(
            AuditLog(
                actor=get_actor(),
                action=action,
                entity_type="incident",
                entity_id=incident_id,
                payload=payload,
                request_id=get_request_id(),
            )
        )

    async def _publish_result(
        self,
        incident: Incident,
        execution: Execution | None,
        message: str,
    ) -> None:
        """Bắn kết quả lên Kafka để service thông báo / dashboard tiêu thụ."""
        if self._publisher is None:
            return

        await self._publisher.publish(
            self._cfg.kafka_topic_remediation,
            {
                "incident_id": incident.id,
                "execution_id": execution.id if execution else None,
                "service": incident.service,
                "status": str(incident.status),
                "severity": str(incident.severity),
                "message": message,
                "attempt": incident.attempt_count,
            },
            key=incident.id,
            event_type="remediation.result",
        )

    def _skip_report(self, incident: Incident, message: str) -> RemediationReport:
        """Gom việc tạo report cho các nhánh không chạy runbook (DRY)."""
        return RemediationReport(
            incident_id=incident.id,
            execution_id=None,
            status=ExecutionStatus.SKIPPED,
            runbook_name="",
            steps_total=0,
            steps_succeeded=0,
            duration_ms=0,
            message=message,
        )
