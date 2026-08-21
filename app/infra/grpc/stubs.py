"""
Nạp code protobuf đã sinh, kèm thông báo lỗi rõ ràng nếu chưa sinh.

VÌ SAO CẦN FILE NÀY?
Nếu import trực tiếp `from app.infra.grpc.generated import agent_pb2` ở đầu
các module, thì người vừa clone project về (chưa chạy `make proto`) sẽ gặp
ModuleNotFoundError khó hiểu ngay khi khởi động app - dù họ chỉ muốn chạy API
mà không dùng gRPC.

Cách làm ở đây: import LAZY (chỉ khi thật sự cần) và đổi lỗi thành hướng dẫn.

KIẾN THỨC PYTHON
  - importlib: import bằng tên dạng chuỗi, lúc runtime
  - Custom exception với message hướng dẫn cụ thể
  - Kiểu Any cho module động (type checker không thể biết trước)
"""

from __future__ import annotations

import importlib
from functools import lru_cache
from typing import Any

_HINT = (
    "Chưa sinh code protobuf. Chạy lệnh sau:\n"
    "    make proto\n"
    "hoặc:\n"
    "    python -m grpc_tools.protoc -I protos "
    "--python_out=app/infra/grpc/generated "
    "--grpc_python_out=app/infra/grpc/generated protos/agent.proto"
)


class ProtoNotGeneratedError(RuntimeError):
    """Lỗi rõ nghĩa thay cho ModuleNotFoundError mơ hồ."""

    def __init__(self) -> None:
        super().__init__(_HINT)


@lru_cache(maxsize=1)
def load() -> tuple[Any, Any]:
    """
    Trả về (agent_pb2, agent_pb2_grpc).

    lru_cache để chỉ import một lần - import module là việc tốn kém.
    Trả về tuple rồi chỗ gọi tự bóc: pb, pb_grpc = load()
    """
    try:
        pb = importlib.import_module("app.infra.grpc.generated.agent_pb2")
        pb_grpc = importlib.import_module("app.infra.grpc.generated.agent_pb2_grpc")
    except ModuleNotFoundError as err:
        raise ProtoNotGeneratedError from err
    return pb, pb_grpc


def is_available() -> bool:
    """
    Kiểm tra đã sinh code chưa - để endpoint /health báo trạng thái.

    Đây là mẫu "feature detection": tính năng nào chưa sẵn sàng thì báo,
    chứ không để app chết.
    """
    try:
        load()
        return True
    except ProtoNotGeneratedError:
        return False
