"""
Correlation ID - "sợi chỉ đỏ" xuyên suốt 1 request.

BÀI TOÁN: 1 alert đi qua API -> Kafka -> worker -> gRPC. Khi lỗi, làm sao
grep log để thấy đúng chuỗi sự kiện của riêng nó giữa hàng vạn dòng log?

GIẢI PHÁP: sinh 1 request_id ở cửa vào, nhét vào contextvars, mọi dòng log
sau đó tự động có nó.

KIẾN THỨC PYTHON: contextvars
  - Giống biến toàn cục, NHƯNG mỗi task asyncio có bản sao riêng.
  - Vì sao không dùng threading.local? Vì asyncio chạy nhiều coroutine trên
    CÙNG 1 thread -> threading.local sẽ bị lẫn dữ liệu giữa các request.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token

# ContextVar[T] với giá trị mặc định. Đặt tên rõ để debug dễ.
_request_id: ContextVar[str] = ContextVar("request_id", default="-")
_actor: ContextVar[str] = ContextVar("actor", default="anonymous")


def new_request_id() -> str:
    """uuid4 = random 128 bit, xác suất trùng gần như 0."""
    return uuid.uuid4().hex


def set_request_id(value: str | None = None) -> Token[str]:
    """
    Gán request_id cho context hiện tại.

    Trả về Token để sau này `reset()` về giá trị cũ - quan trọng khi dùng
    trong middleware, tránh rò rỉ giá trị sang request kế tiếp.
    """
    return _request_id.set(value or new_request_id())


def get_request_id() -> str:
    return _request_id.get()


def set_actor(value: str) -> Token[str]:
    """Ai đang thao tác: email user, hoặc 'system' khi engine tự chạy."""
    return _actor.set(value)


def get_actor() -> str:
    return _actor.get()


def reset(token: Token[str], var: ContextVar[str]) -> None:
    """Khôi phục giá trị trước đó."""
    var.reset(token)


# Export sẵn để middleware reset đúng biến.
REQUEST_ID_VAR = _request_id
ACTOR_VAR = _actor
