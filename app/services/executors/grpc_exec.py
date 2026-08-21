"""
Executor gọi Agent qua gRPC - dùng khi cần chạy lệnh TRÊN node.

Vì sao không SSH? Vì SSH cần lưu private key trong app (rủi ro bảo mật cao),
khó giới hạn lệnh được phép chạy, và khó audit. Agent gRPC thì:
  - Chỉ nhận đúng các action đã định nghĩa trong .proto (whitelist by design)
  - Có mTLS, có log tập trung
  - Engine không cần quyền root ở đâu cả
"""

from __future__ import annotations

from typing import Any, ClassVar

from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger
from app.domain.enums import ActionType
from app.services.executors.base import ExecutionContext, Executor, StepOutcome

logger = get_logger(__name__)


class GrpcCommandExecutor(Executor):
    """
    Nhờ agent thực thi một hành động trên node.

    Tham số trong runbook:
        {
          "action": "RESTART_SERVICE",
          "target": "{service}",
          "args": {"namespace": "{environment}"},
          "timeout": 30,
          "rollback_action": "SCALE_WORKLOAD"
        }
    """

    action_type: ClassVar[ActionType] = ActionType.GRPC_COMMAND

    async def run(self, ctx: ExecutionContext, params: dict[str, Any]) -> StepOutcome:
        p = ctx.render_params(params)

        action = str(p.get("action", "")).strip()
        target = str(p.get("target", ctx.service))
        if not action:
            raise ValueError("GRPC_COMMAND cần tham số 'action'")

        # Import trong hàm: nếu chưa chạy `make proto`, các executor khác vẫn
        # dùng được bình thường, chỉ executor này báo lỗi.
        from app.infra.grpc.client import AgentClient
        from app.infra.grpc.stubs import ProtoNotGeneratedError

        try:
            client = AgentClient()
        except ProtoNotGeneratedError as err:
            return StepOutcome.fail(f"Chưa sinh code protobuf: {err}")

        # args của protobuf map<string,string> nên mọi giá trị phải là str.
        args = {str(k): str(v) for k, v in (p.get("args") or {}).items()}

        try:
            result = await client.execute(
                incident_id=ctx.incident_id,
                execution_id=ctx.execution_id,
                action=action,
                target=target,
                args=args,
                timeout=p.get("timeout"),
                dry_run=ctx.dry_run,
            )
        except ExternalServiceError as err:
            return StepOutcome.fail(err.message)

        if not result["success"]:
            return StepOutcome.fail(
                result["stderr"] or f"{action} thất bại (exit={result['exit_code']})",
                exit_code=result["exit_code"],
                stdout=result["stdout"],
            )

        return StepOutcome.ok(
            stdout=result["stdout"],
            exit_code=result["exit_code"],
            duration_ms=result["duration_ms"],
            agent_version=result["agent_version"],
        )

    async def rollback(self, ctx: ExecutionContext, params: dict[str, Any]) -> StepOutcome:
        """
        Hoàn tác bằng cách chạy action nghịch đảo.

        Ví dụ: đã SCALE_WORKLOAD lên 10 replica mà bước sau lỗi thì scale
        về số cũ. Runbook khai báo rollback_params cho việc này.

        Override method của cha -> minh hoạ tính đa hình (polymorphism):
        engine gọi executor.rollback() mà không cần biết là executor nào.
        """
        rollback_params = params.get("rollback_params") or {}
        if not rollback_params:
            return StepOutcome.ok(skipped="không khai báo rollback_params")

        logger.info("Rollback: %s", rollback_params.get("action"))
        # Tái dùng chính run() - rollback cũng chỉ là một lệnh gRPC khác.
        return await self.run(ctx, rollback_params)
