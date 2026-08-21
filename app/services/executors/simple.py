"""
Các executor đơn giản: WAIT, NOTIFY, HEALTH_CHECK, NOOP.

Bốn cái này ngắn nhưng KHÔNG hề phụ: một runbook tốt luôn có nhịp
"tác động -> chờ -> xác minh -> thông báo". Thiếu bước xác minh thì hệ thống
sẽ báo "đã sửa xong" trong khi service vẫn chết.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from app.core.config import get_settings
from app.core.logging import get_logger
from app.domain.enums import ActionType
from app.infra.http_client import healthcheck as http_healthcheck
from app.services.executors.base import ExecutionContext, Executor, StepOutcome

logger = get_logger(__name__)


class WaitExecutor(Executor):
    """
    Chờ N giây.

    VÌ SAO CẦN? Sau khi restart, service cần thời gian warm-up (nạp cache,
    kết nối DB, JIT compile). Kiểm tra health ngay lập tức sẽ thấy "chết"
    và engine tưởng thất bại -> restart lại -> vòng lặp vô nghĩa.

    Tham số: {"seconds": 30}
    """

    action_type: ClassVar[ActionType] = ActionType.WAIT

    async def run(self, ctx: ExecutionContext, params: dict[str, Any]) -> StepOutcome:
        seconds = float(params.get("seconds", 5))

        # Chặn trên: runbook lỡ ghi 100000 giây thì không treo cả worker.
        seconds = min(seconds, 300.0)

        if ctx.dry_run:
            return StepOutcome.ok(dry_run=True, would_wait=seconds)

        logger.info("Chờ %.1fs", seconds)
        # asyncio.sleep NHƯỜNG event loop cho task khác -> trong lúc chờ,
        # worker vẫn xử lý được incident khác. time.sleep thì đứng hết.
        await asyncio.sleep(seconds)
        return StepOutcome.ok(waited_seconds=seconds)


class HealthCheckExecutor(Executor):
    """
    Xác minh service đã hồi phục thật chưa.

    ĐÂY LÀ BƯỚC QUAN TRỌNG NHẤT của một runbook. Không có nó, hệ thống chỉ
    "làm gì đó" rồi tự tuyên bố thành công - tệ hơn cả không làm gì, vì nó
    khiến người vận hành mất niềm tin và bỏ qua alert thật.

    Tham số:
        {"url": "http://{service}/healthz", "retries": 5, "interval": 3,
         "expect_status": 200}
    """

    action_type: ClassVar[ActionType] = ActionType.HEALTH_CHECK

    async def run(self, ctx: ExecutionContext, params: dict[str, Any]) -> StepOutcome:
        p = ctx.render_params(params)
        url = p.get("url")
        if not url:
            raise ValueError("HEALTH_CHECK cần tham số 'url'")

        retries = int(p.get("retries", 3))
        interval = float(p.get("interval", 3))
        expect = int(p.get("expect_status", 200))

        if ctx.dry_run:
            return StepOutcome.ok(dry_run=True, url=url)

        # Thử nhiều lần: service vừa khởi động có thể chưa kịp sẵn sàng.
        # enumerate(..., start=1) để đếm từ 1 cho log dễ đọc.
        for attempt in range(1, retries + 1):
            if await http_healthcheck(url, expect_status=expect, timeout=5.0):
                return StepOutcome.ok(healthy=True, attempts=attempt, url=url)

            logger.info("Health check %s chưa OK (lần %d/%d)", url, attempt, retries)
            if attempt < retries:
                await asyncio.sleep(interval)

        # Hết lượt mà vẫn không khoẻ -> thất bại thật, engine phải escalate.
        return StepOutcome.fail(
            f"Service vẫn không khoẻ sau {retries} lần kiểm tra",
            url=url,
            attempts=retries,
        )


class NotifyExecutor(Executor):
    """
    Bắn thông báo qua Kafka (rồi service khác đẩy tiếp lên Slack/Telegram).

    VÌ SAO KHÔNG GỌI SLACK TRỰC TIẾP? Vì Slack có thể chậm/lỗi, và ta không
    muốn việc gửi thông báo làm chậm hoặc hỏng luồng phục hồi. Đẩy vào Kafka
    là "fire and forget" - đúng tinh thần tách rời (decoupling).

    Tham số: {"channel": "sre-oncall", "message": "Đã restart {service}",
              "level": "warning"}
    """

    action_type: ClassVar[ActionType] = ActionType.NOTIFY

    async def run(self, ctx: ExecutionContext, params: dict[str, Any]) -> StepOutcome:
        p = ctx.render_params(params)
        message = str(p.get("message", f"Incident {ctx.incident_id}"))

        if ctx.dry_run:
            return StepOutcome.ok(dry_run=True, message=message)

        # Dùng publisher DÙNG CHUNG mà engine đã truyền vào ctx.
        # Trước đây executor tự tạo EventPublisher rồi start/stop ở mỗi step:
        # mỗi lần thông báo là một lần bắt tay lại với broker (~30ms) và hai
        # dòng log rác. Nhận qua ctx là cách đúng.
        if ctx.publisher is None:
            # Không có publisher (Kafka tắt, hoặc đang test) -> chỉ log.
            # Thông báo lỗi KHÔNG được làm thất bại cả runbook.
            logger.info("Không có publisher, chỉ log thông báo: %s", message)
            return StepOutcome.ok(notified=False, message=message, reason="no_publisher")

        await ctx.publisher.publish(
            get_settings().kafka_topic_remediation,
            {
                "incident_id": ctx.incident_id,
                "execution_id": ctx.execution_id,
                "channel": p.get("channel", "default"),
                "level": p.get("level", "info"),
                "message": message,
            },
            key=ctx.incident_id,
            event_type="notification",
        )
        return StepOutcome.ok(notified=True, message=message)


class NoopExecutor(Executor):
    """
    Không làm gì - nhưng rất hữu ích.

    Dùng để:
      - Test luồng engine mà không tác động hạ tầng
      - Làm chỗ giữ chỗ (placeholder) trong runbook đang viết dở
      - Làm runbook "catch-all" chỉ để ghi nhận rồi escalate
    """

    action_type: ClassVar[ActionType] = ActionType.NOOP

    async def run(self, ctx: ExecutionContext, params: dict[str, Any]) -> StepOutcome:
        logger.info("NOOP cho incident %s", ctx.incident_id)
        return StepOutcome.ok(noop=True, params=params)
