"""
Test Circuit Breaker - "cầu dao" chống sập lan (cascading failure).

Ba trạng thái CLOSED / OPEN / HALF_OPEN và các bước chuyển giữa chúng là
phần dễ viết sai nhất, nên test từng nhánh.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.exceptions import CircuitOpenError
from app.utils.circuit_breaker import CircuitBreaker, CircuitState, get_breaker


class TestStates:
    async def test_bat_dau_o_trang_thai_dong(self) -> None:
        cb = CircuitBreaker(name="test")
        assert cb.state is CircuitState.CLOSED
        await cb.before_call()  # không raise

    async def test_du_so_loi_thi_mo_cau_dao(self) -> None:
        cb = CircuitBreaker(name="test", failure_threshold=3)

        for _ in range(3):
            await cb.on_failure()

        assert cb.state is CircuitState.OPEN

        # Từ giờ mọi lời gọi bị từ chối NGAY, không phải chờ timeout 15s.
        with pytest.raises(CircuitOpenError):
            await cb.before_call()

    async def test_thanh_cong_reset_bo_dem_loi(self) -> None:
        """
        Ta đếm lỗi LIÊN TIẾP. Một lần thành công xoá sạch bộ đếm.

        Nếu đếm tổng lỗi từ đầu thì mọi service chạy lâu đều sẽ bị ngắt oan.
        """
        cb = CircuitBreaker(name="test", failure_threshold=3)

        await cb.on_failure()
        await cb.on_failure()
        await cb.on_success()
        await cb.on_failure()

        assert cb.state is CircuitState.CLOSED  # chưa đủ 3 lỗi liên tiếp

    async def test_sau_recovery_time_thi_tham_do(self) -> None:
        cb = CircuitBreaker(name="test", failure_threshold=1, recovery_time=0.05)
        await cb.on_failure()
        assert cb.state is CircuitState.OPEN

        await asyncio.sleep(0.06)
        await cb.before_call()  # hết thời gian chặn -> chuyển sang thăm dò
        assert cb.state is CircuitState.HALF_OPEN

    async def test_tham_do_thanh_cong_thi_dong_lai(self) -> None:
        cb = CircuitBreaker(
            name="test", failure_threshold=1, recovery_time=0.01, success_threshold=2
        )
        await cb.on_failure()
        await asyncio.sleep(0.02)
        await cb.before_call()

        await cb.on_success()
        assert cb.state is CircuitState.HALF_OPEN  # chưa đủ 2 lần
        await cb.on_success()
        assert cb.state is CircuitState.CLOSED

    async def test_tham_do_that_bai_thi_mo_lai_ngay(self) -> None:
        """
        Đang thăm dò mà lỗi -> mở lại LẬP TỨC, không chờ đủ ngưỡng.

        Vì service rõ ràng vẫn chưa khoẻ, thử thêm chỉ tốn tài nguyên.
        """
        cb = CircuitBreaker(name="test", failure_threshold=5, recovery_time=0.01)
        for _ in range(5):
            await cb.on_failure()
        await asyncio.sleep(0.02)
        await cb.before_call()
        assert cb.state is CircuitState.HALF_OPEN

        await cb.on_failure()
        assert cb.state is CircuitState.OPEN


class TestContextManager:
    async def test_dung_nhu_async_context_manager(self) -> None:
        """__aenter__/__aexit__ cho phép viết "async with breaker:" rất gọn."""
        cb = CircuitBreaker(name="test", failure_threshold=1)

        # Thành công -> tự gọi on_success
        async with cb:
            pass
        assert cb.state is CircuitState.CLOSED

        # Lỗi -> tự gọi on_failure, và lỗi VẪN bay ra ngoài
        # (vì __aexit__ trả về False = "tôi không xử lý lỗi này").
        with pytest.raises(ValueError):
            async with cb:
                raise ValueError("lỗi")
        assert cb.state is CircuitState.OPEN

    async def test_khong_nuot_exception(self) -> None:
        """
        Chi tiết dễ sai: __aexit__ trả True sẽ NUỐT lỗi.

        Nếu nuốt, engine sẽ tưởng step thành công trong khi thực tế đã lỗi.
        """
        cb = CircuitBreaker(name="test")
        with pytest.raises(RuntimeError, match="phai bay ra"):
            async with cb:
                raise RuntimeError("phai bay ra")


class TestRegistry:
    def test_moi_dependency_mot_breaker_rieng(self) -> None:
        """
        BULKHEAD PATTERN: K8s API sập thì không có lý gì chặn luôn cả Slack.

        Mỗi dependency phải được cách ly bằng breaker riêng.
        """
        cb1 = get_breaker("k8s-api")
        cb2 = get_breaker("slack")
        assert cb1 is not cb2

        # Gọi lại cùng tên thì trả về CÙNG object (giữ được state).
        assert get_breaker("k8s-api") is cb1
