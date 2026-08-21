"""
gRPC client - Engine gọi sang Agent trên node.

KIẾN THỨC gRPC
  1. Channel  : kết nối HTTP/2 tới server. Tạo MỘT lần, dùng lâu dài
                (giống connection pool). Tạo channel mỗi lần gọi là rất chậm.
  2. Stub     : object đại diện service, sinh ra từ .proto
  3. Deadline : timeout của gRPC. BẮT BUỘC truyền, nếu không request treo mãi.
  4. Streaming: server trả nhiều response cho một request

KIẾN THỨC PYTHON
  1. grpc.aio - API async của grpcio (khác hẳn grpc đồng bộ)
  2. async for trên streaming response
  3. Chuyển đổi lỗi tầng hạ tầng thành lỗi domain của mình
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import grpc

from app.core.config import Settings, get_settings
from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger
from app.infra.grpc.stubs import load
from app.utils.circuit_breaker import get_breaker

logger = get_logger(__name__)

_channel: grpc.aio.Channel | None = None


def init_channel(settings: Settings | None = None) -> grpc.aio.Channel:
    """
    Tạo channel dùng chung.

    Ý nghĩa các option (đặt theo tên chuỗi, đúng chuẩn gRPC):
      keepalive_time_ms      : ping định kỳ để giữ kết nối qua NAT/firewall
      max_receive_message_length : giới hạn kích thước response (chống OOM)
      grpc.lb_policy_name    : round_robin khi có nhiều agent phía sau DNS
    """
    global _channel  # noqa: PLW0603

    if _channel is not None:
        return _channel

    cfg = settings or get_settings()
    _channel = grpc.aio.insecure_channel(
        cfg.grpc_agent_target,
        options=[
            ("grpc.keepalive_time_ms", 30_000),
            ("grpc.keepalive_timeout_ms", 10_000),
            ("grpc.max_receive_message_length", 8 * 1024 * 1024),
            ("grpc.lb_policy_name", "round_robin"),
        ],
    )
    logger.info("Đã tạo gRPC channel tới %s", cfg.grpc_agent_target)
    return _channel


async def close_channel() -> None:
    global _channel  # noqa: PLW0603
    if _channel is not None:
        await _channel.close()
        logger.info("Đã đóng gRPC channel")
    _channel = None


class AgentClient:
    """
    Bọc stub gRPC lại cho tầng service dùng dễ hơn.

    Tầng service KHÔNG nên biết tới protobuf message. Nó truyền dict, nhận
    dict. Đây chính là vai trò "adapter" - nếu mai đổi từ gRPC sang REST,
    chỉ file này phải sửa.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._cfg = settings or get_settings()
        # Import lazy: chỉ nạp protobuf khi thật sự khởi tạo client.
        self._pb, self._pb_grpc = load()
        self._stub = self._pb_grpc.AgentServiceStub(init_channel(self._cfg))

    async def execute(
        self,
        *,
        incident_id: str,
        execution_id: str,
        action: str,
        target: str,
        args: dict[str, str] | None = None,
        timeout: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """
        Gọi RPC ExecuteCommand.

        `action` truyền vào là chuỗi ("RESTART_SERVICE"). Enum của protobuf
        thực chất là số, nên phải tra qua .Value() để đổi tên -> số.
        """
        breaker = get_breaker("grpc_agent", failure_threshold=4, recovery_time=20.0)
        timeout_s = timeout or self._cfg.grpc_timeout_seconds

        try:
            action_value = self._pb.ActionKind.Value(action.upper())
        except ValueError as err:
            # Tên action sai -> lỗi cấu hình runbook, không phải lỗi mạng.
            raise ExternalServiceError("grpc_agent", f"Action không hợp lệ: {action}") from err

        request = self._pb.ExecuteCommandRequest(
            incident_id=incident_id,
            execution_id=execution_id,
            action=action_value,
            target=target,
            args=args or {},
            timeout_seconds=timeout_s,
            dry_run=dry_run,
        )

        async with breaker:
            try:
                # timeout của gRPC gọi là "deadline": tính từ lúc gửi, và
                # được truyền tiếp sang các service phía sau (deadline
                # propagation) - rất hay, REST không có sẵn.
                response = await self._stub.ExecuteCommand(request, timeout=timeout_s)
            except grpc.aio.AioRpcError as err:
                # Đổi lỗi gRPC sang lỗi domain, kèm mã lỗi để debug.
                raise ExternalServiceError(
                    "grpc_agent",
                    f"{err.code().name}: {err.details()}",
                ) from err

        return {
            "success": response.success,
            "exit_code": response.exit_code,
            "stdout": response.stdout,
            "stderr": response.stderr,
            "duration_ms": response.duration_ms,
            # HasField dùng cho field `optional` trong proto3: phân biệt
            # "không gửi" với "gửi chuỗi rỗng".
            "agent_version": (
                response.agent_version if response.HasField("agent_version") else None
            ),
        }

    async def stream(
        self,
        *,
        incident_id: str,
        execution_id: str,
        action: str,
        target: str,
        args: dict[str, str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Server-streaming RPC: nhận log từng dòng ngay khi agent in ra.

        Dùng:
            async for chunk in client.stream(...):
                print(chunk["line"])

        Kỹ thuật: hàm này vừa là async generator (có yield) vừa gọi gRPC
        streaming. Mỗi message server gửi về thành một lần yield.
        """
        request = self._pb.ExecuteCommandRequest(
            incident_id=incident_id,
            execution_id=execution_id,
            action=self._pb.ActionKind.Value(action.upper()),
            target=target,
            args=args or {},
        )
        try:
            async for chunk in self._stub.StreamCommand(request):
                yield {
                    "line": chunk.line,
                    "timestamp": chunk.timestamp,
                    "is_stderr": chunk.is_stderr,
                    "is_final": chunk.is_final,
                }
        except grpc.aio.AioRpcError as err:
            raise ExternalServiceError("grpc_agent", f"Stream lỗi: {err.details()}") from err

    async def health(self, target: str) -> dict[str, Any]:
        """Hỏi agent xem node còn khoẻ không, kèm vài metric."""
        request = self._pb.HealthCheckRequest(target=target)
        try:
            response = await self._stub.HealthCheck(
                request, timeout=self._cfg.grpc_timeout_seconds
            )
        except grpc.aio.AioRpcError as err:
            raise ExternalServiceError("grpc_agent", f"Health check lỗi: {err.details()}") from err

        return {
            # Name() đổi số enum -> tên chuỗi, ngược với Value().
            "status": self._pb.HealthCheckResponse.Status.Name(response.status),
            "message": response.message,
            "metrics": dict(response.metrics),
        }
