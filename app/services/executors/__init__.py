"""
Gom mọi executor - import ở đây là chúng TỰ ĐĂNG KÝ vào registry.

Đây là điểm hay của __init_subclass__: file này chỉ cần import, không cần
viết register(...) cho từng cái. Thêm executor mới = thêm 1 dòng import.
"""

from app.services.executors.base import (
    ExecutionContext,
    Executor,
    StepOutcome,
    get_executor,
)
from app.services.executors.grpc_exec import GrpcCommandExecutor
from app.services.executors.http_exec import HttpCallExecutor
from app.services.executors.simple import (
    HealthCheckExecutor,
    NoopExecutor,
    NotifyExecutor,
    WaitExecutor,
)

__all__ = [
    "ExecutionContext",
    "Executor",
    "GrpcCommandExecutor",
    "HealthCheckExecutor",
    "HttpCallExecutor",
    "NoopExecutor",
    "NotifyExecutor",
    "StepOutcome",
    "WaitExecutor",
    "get_executor",
]
