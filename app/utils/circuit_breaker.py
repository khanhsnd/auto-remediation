"""
Circuit Breaker - "cầu dao" bảo vệ hệ thống.

BÀI TOÁN: Kubernetes API đang sập. Engine vẫn cố gọi nó 1000 lần/phút, mỗi
lần chờ timeout 15s -> hàng nghìn task bị treo, cạn connection, và app của
ta cũng sập theo (cascading failure).

GIẢI PHÁP: đếm số lần lỗi. Vượt ngưỡng thì "ngắt cầu dao" - các lời gọi sau
bị từ chối NGAY LẬP TỨC, không mất 15s chờ. Sau một khoảng thời gian, cho
thử lại một ít request để dò xem đã hồi phục chưa.

BA TRẠNG THÁI:
    CLOSED    : bình thường, cho qua hết (cầu dao đóng = điện chạy)
    OPEN      : đang chặn, từ chối ngay lập tức
    HALF_OPEN : cho qua vài request để thăm dò
                 -> thành công đủ số lần thì về CLOSED
                 -> lỗi thì OPEN lại ngay

    CLOSED --(lỗi >= ngưỡng)--> OPEN --(hết recovery_time)--> HALF_OPEN
       ^                                                        |
       +---------------(thành công đủ)--------------------------+

KIẾN THỨC PYTHON
  1. asyncio.Lock  - chống race condition khi nhiều task cùng sửa state
  2. __aenter__ / __aexit__ - tự viết async context manager (không dùng decorator)
  3. dataclass giữ state, StrEnum cho trạng thái
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import StrEnum
from types import TracebackType
from typing import Literal, Self

from app.core.exceptions import CircuitOpenError
from app.core.logging import get_logger

logger = get_logger(__name__)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """
    Một cầu dao cho MỘT dependency (mỗi service ngoài nên có riêng một cái).

    dataclass thường (không frozen) vì state phải thay đổi được.
    field(default_factory=...) cho các giá trị mutable/khởi tạo động.
    """

    name: str
    failure_threshold: int = 5      # bao nhiêu lỗi liên tiếp thì ngắt
    recovery_time: float = 30.0     # ngắt bao lâu trước khi thăm dò lại
    success_threshold: int = 2      # thăm dò thành công mấy lần thì đóng lại

    # --- state nội bộ (không phải tham số cấu hình) ---
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    opened_at: float = 0.0

    # Lock đảm bảo chỉ một task sửa state tại một thời điểm.
    # asyncio.Lock (không phải threading.Lock) vì ta ở trong event loop.
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def before_call(self) -> None:
        """
        Gọi TRƯỚC khi thực hiện request. Ném CircuitOpenError nếu đang chặn.

        async with self._lock: đoạn trong này chỉ một task chạy tại một lúc.
        Vì sao cần? Đọc state -> tính toán -> ghi state là 3 bước; nếu bị
        chen ngang giữa các await thì hai task có thể cùng chuyển trạng thái.
        """
        async with self._lock:
            if self.state is CircuitState.OPEN:
                elapsed = time.monotonic() - self.opened_at

                # time.monotonic() luôn tăng, không bị nhảy khi máy đổi giờ.
                # Đo khoảng thời gian thì phải dùng nó, không dùng time.time().
                if elapsed < self.recovery_time:
                    raise CircuitOpenError(
                        f"Circuit '{self.name}' đang mở, thử lại sau "
                        f"{self.recovery_time - elapsed:.0f}s",
                        details={"circuit": self.name, "state": str(self.state)},
                    )

                # Hết thời gian chờ -> chuyển sang thăm dò.
                self.state = CircuitState.HALF_OPEN
                self.success_count = 0
                logger.info("Circuit '%s': OPEN -> HALF_OPEN (thăm dò)", self.name)

    async def on_success(self) -> None:
        """Gọi khi request thành công."""
        async with self._lock:
            match self.state:
                case CircuitState.HALF_OPEN:
                    self.success_count += 1
                    if self.success_count >= self.success_threshold:
                        self.state = CircuitState.CLOSED
                        self.failure_count = 0
                        logger.info("Circuit '%s': HALF_OPEN -> CLOSED (đã hồi phục)", self.name)
                case CircuitState.CLOSED:
                    # Thành công thì reset bộ đếm lỗi. Chú ý: ta đếm lỗi
                    # LIÊN TIẾP, nên một lần thành công là xoá sạch.
                    self.failure_count = 0
                case _:
                    pass

    async def on_failure(self) -> None:
        """Gọi khi request thất bại."""
        async with self._lock:
            self.failure_count += 1

            # Đang thăm dò mà lỗi -> mở lại ngay, không cần chờ đủ ngưỡng.
            if self.state is CircuitState.HALF_OPEN:
                self._open()
                return

            if self.failure_count >= self.failure_threshold:
                self._open()

    def _open(self) -> None:
        """Hàm nội bộ (tiền tố _) - đã nắm lock rồi mới được gọi."""
        self.state = CircuitState.OPEN
        self.opened_at = time.monotonic()
        self.success_count = 0
        logger.warning(
            "Circuit '%s' MỞ sau %d lỗi, chặn %.0fs",
            self.name,
            self.failure_count,
            self.recovery_time,
        )

    # ------------------------------------------------- async context manager
    async def __aenter__(self) -> Self:
        """
        Cho phép dùng:  async with breaker: ...

        Tự viết __aenter__/__aexit__ thay vì @asynccontextmanager khi object
        cần giữ state và có nhiều method khác - đây là cách "đầy đủ" nhất.
        """
        await self.before_call()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        """
        exc_type là None nếu khối with chạy êm, ngược lại là class của lỗi.

        Trả về False = "tôi KHÔNG xử lý lỗi này, cứ để nó bay lên".
        Trả về True sẽ NUỐT lỗi - hầu như luôn là sai, trừ khi cố ý.

        VÌ SAO KIỂU TRẢ VỀ LÀ Literal[False] MÀ KHÔNG PHẢI bool?
        Nếu khai bool, type checker phải giả định context manager CÓ THỂ nuốt
        lỗi -> nó coi dòng code sau "async with" là có thể chạy tới, và báo
        "missing return statement" ở hàm mà lệnh return duy nhất nằm trong
        khối with. Literal[False] nói rõ "không bao giờ nuốt lỗi".
        """
        if exc_type is None:
            await self.on_success()
        else:
            await self.on_failure()
        return False


# Sổ đăng ký: mỗi dependency một breaker, tra theo tên.
# Vì sao không dùng chung 1 breaker? Vì K8s API sập thì không có lý gì chặn
# luôn cả Slack. Mỗi dependency phải được cách ly (bulkhead pattern).
_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(name: str, **kwargs: float | int) -> CircuitBreaker:
    """Lấy breaker theo tên, chưa có thì tạo mới (lazy)."""
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(name=name, **kwargs)  # type: ignore[arg-type]
    return _breakers[name]


def all_breakers() -> dict[str, CircuitState]:
    """Trạng thái mọi breaker - để lộ ra endpoint /health cho dễ quan sát."""
    return {name: cb.state for name, cb in _breakers.items()}
