"""
gRPC Agent GIẢ LẬP - để chạy thử toàn bộ luồng ở local.

Trong thực tế, agent này là một binary nhỏ chạy trên từng node/cluster, và
nó thực sự gọi systemctl/kubectl. Ở đây ta mô phỏng: ngủ vài giây rồi trả
kết quả, có tỉ lệ thất bại ngẫu nhiên để test nhánh lỗi của engine.

Chạy:  python -m app.infra.grpc.server     (hoặc: make grpc)

KIẾN THỨC PYTHON
  1. Kế thừa class servicer sinh từ proto rồi override method
  2. asyncio.run() - điểm vào của chương trình async
  3. Xử lý tín hiệu (SIGTERM) để tắt "êm" (graceful shutdown)
  4. Async generator làm streaming response
  5. Class factory - tạo class lúc runtime
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import signal
import time
from collections.abc import AsyncIterator
from typing import Any

import grpc

from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.infra.grpc.stubs import load

logger = get_logger(__name__)

# Thời gian mô phỏng cho từng loại hành động (giây).
_SIMULATED_DURATION: dict[str, tuple[float, float]] = {
    "RESTART_SERVICE": (1.0, 3.0),
    "SCALE_WORKLOAD": (2.0, 4.0),
    "CLEAR_CACHE": (0.3, 1.0),
    "ROTATE_LOG": (0.5, 1.5),
    "RUN_SCRIPT": (1.0, 5.0),
    "KILL_PROCESS": (0.2, 0.8),
}

# Tỉ lệ thất bại giả lập - để test nhánh retry/rollback của engine.
_FAILURE_RATE = 0.15


def _build_servicer() -> Any:
    """
    Tạo class servicer BÊN TRONG một hàm.

    Vì sao? Vì class cha (AgentServiceServicer) chỉ tồn tại sau khi đã sinh
    code protobuf. Nếu khai báo class ở cấp module, file này sẽ không import
    được khi chưa chạy `make proto`.

    Đây là kỹ thuật "class factory" - class trong Python cũng chỉ là object,
    tạo được lúc runtime.
    """
    pb, pb_grpc = load()

    # Lớp cha đến từ code sinh tự động nên type checker không biết nó ->
    # bỏ qua có chủ đích ở đúng một dòng này.
    class AgentService(pb_grpc.AgentServiceServicer):  # type: ignore[misc, name-defined]
        """Hiện thực hoá service đã khai báo trong agent.proto."""

        async def ExecuteCommand(  # noqa: N802 - tên do proto quy định
            self,
            request: Any,
            context: grpc.aio.ServicerContext,
        ) -> Any:
            """
            Unary RPC.

            `context` cho phép: đọc metadata, set trailing metadata, abort với
            mã lỗi gRPC, và kiểm tra deadline còn lại.
            """
            action_name = pb.ActionKind.Name(request.action)
            started = time.perf_counter()

            logger.info(
                "Nhận lệnh %s trên %s (dry_run=%s)",
                action_name,
                request.target,
                request.dry_run,
                extra={"incident_id": request.incident_id},
            )

            # Chế độ dry-run: chỉ xác nhận, không "làm" gì.
            if request.dry_run:
                return pb.ExecuteCommandResponse(
                    success=True,
                    exit_code=0,
                    stdout=f"[DRY-RUN] Sẽ thực hiện {action_name} trên {request.target}",
                    duration_ms=1,
                    agent_version="mock-0.1",
                )

            # Mô phỏng thời gian thực thi.
            low, high = _SIMULATED_DURATION.get(action_name, (0.5, 1.5))
            await asyncio.sleep(random.uniform(low, high))  # noqa: S311

            # Mô phỏng thất bại ngẫu nhiên.
            failed = random.random() < _FAILURE_RATE  # noqa: S311
            duration_ms = int((time.perf_counter() - started) * 1000)

            if failed:
                logger.warning("Lệnh %s THẤT BẠI (giả lập)", action_name)
                return pb.ExecuteCommandResponse(
                    success=False,
                    exit_code=1,
                    stderr=f"{action_name} thất bại: service không phản hồi sau khi restart",
                    duration_ms=duration_ms,
                    agent_version="mock-0.1",
                )

            return pb.ExecuteCommandResponse(
                success=True,
                exit_code=0,
                stdout=f"{action_name} trên {request.target} hoàn tất",
                duration_ms=duration_ms,
                agent_version="mock-0.1",
            )

        async def StreamCommand(  # noqa: N802
            self,
            request: Any,
            context: grpc.aio.ServicerContext,
        ) -> AsyncIterator[Any]:
            """
            Server streaming: yield từng dòng log.

            Hàm có `yield` -> async generator. gRPC gửi mỗi item về client
            NGAY LẬP TỨC, không chờ hết vòng lặp. Nhờ đó người vận hành thấy
            log chạy dần dần như đang xem terminal.
            """
            action_name = pb.ActionKind.Name(request.action)
            steps = [
                f"Đang kết nối tới {request.target}...",
                "Kiểm tra quyền thực thi... OK",
                f"Chạy {action_name}...",
                "Chờ service khởi động lại...",
                "Kiểm tra health endpoint... OK",
            ]

            for idx, line in enumerate(steps):
                await asyncio.sleep(0.4)
                yield pb.CommandLogChunk(
                    line=line,
                    timestamp=int(time.time() * 1000),
                    is_stderr=False,
                    is_final=(idx == len(steps) - 1),
                )

        async def HealthCheck(  # noqa: N802
            self,
            request: Any,
            context: grpc.aio.ServicerContext,
        ) -> Any:
            """Trả trạng thái + metric giả lập."""
            cpu = random.uniform(10, 95)  # noqa: S311
            mem = random.uniform(30, 92)  # noqa: S311

            # match/case trên tuple - so khớp nhiều giá trị cùng lúc,
            # kèm guard "if" để diễn tả ngưỡng.
            match (cpu, mem):
                case (c, m) if c > 90 or m > 90:
                    status = pb.HealthCheckResponse.Status.UNHEALTHY
                    message = "Tải quá cao"
                case (c, m) if c > 70 or m > 70:
                    status = pb.HealthCheckResponse.Status.DEGRADED
                    message = "Tải cao nhưng còn chịu được"
                case _:
                    status = pb.HealthCheckResponse.Status.HEALTHY
                    message = "Bình thường"

            return pb.HealthCheckResponse(
                status=status,
                message=message,
                metrics={"cpu": f"{cpu:.1f}", "mem": f"{mem:.1f}"},
            )

    return AgentService()


async def serve() -> None:
    """
    Khởi động server và chờ tới khi có tín hiệu tắt.

    GRACEFUL SHUTDOWN quan trọng thế nào? Khi Kubernetes rolling update, nó
    gửi SIGTERM. Nếu ta tắt ngay lập tức, các lệnh đang chạy giữa đường sẽ
    bị cắt -> hạ tầng ở trạng thái nửa vời (đã stop mà chưa start). Vì vậy
    phải: ngừng nhận request mới, chờ request đang chạy xong, rồi mới tắt.
    """
    cfg = get_settings()
    setup_logging(cfg)

    _, pb_grpc = load()
    server = grpc.aio.server(
        options=[
            ("grpc.max_receive_message_length", 8 * 1024 * 1024),
            ("grpc.keepalive_time_ms", 30_000),
        ]
    )
    pb_grpc.add_AgentServiceServicer_to_server(_build_servicer(), server)

    address = f"[::]:{cfg.grpc_server_port}"
    server.add_insecure_port(address)
    await server.start()
    logger.info("gRPC Agent (giả lập) đang lắng nghe tại %s", address)

    # Event dùng làm "cờ": handler tín hiệu set nó, coroutine chính chờ nó.
    stop_event = asyncio.Event()

    def _on_signal() -> None:
        logger.info("Nhận tín hiệu tắt, đang dừng...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows không hỗ trợ add_signal_handler -> bọc contextlib.suppress
        # để code chạy được cả trên Windows và Linux.
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, _on_signal)

    await stop_event.wait()
    # grace=10: cho các RPC đang chạy tối đa 10s để hoàn thành.
    await server.stop(grace=10)
    logger.info("gRPC Agent đã tắt")


if __name__ == "__main__":
    # asyncio.run() tạo event loop, chạy coroutine, rồi dọn dẹp.
    # suppress(KeyboardInterrupt) để Ctrl+C không in traceback dài dòng.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve())
