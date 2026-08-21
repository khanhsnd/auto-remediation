"""
Test các tiện ích nâng cao: decorator retry, timeout, singleflight.

Đây là những kỹ thuật hay bị viết sai. Test kỹ ở đây rẻ hơn nhiều so với
debug lúc production đang cháy.
"""

from __future__ import annotations

import asyncio

import pytest

from app.utils.decorators import retry, singleflight, timed, with_timeout


class TestRetry:
    async def test_thanh_cong_ngay_lan_dau_thi_khong_retry(self) -> None:
        calls = 0

        @retry(attempts=3, base_delay=0.01)
        async def f() -> str:
            # nonlocal cho phép sửa biến của hàm bao ngoài (closure).
            # Không có nó, "calls += 1" sẽ tạo biến local mới và báo lỗi.
            nonlocal calls
            calls += 1
            return "ok"

        assert await f() == "ok"
        assert calls == 1

    async def test_retry_roi_thanh_cong(self) -> None:
        calls = 0

        @retry(attempts=3, base_delay=0.01)
        async def f() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ConnectionError("mạng lỗi")
            return "ok"

        assert await f() == "ok"
        assert calls == 3

    async def test_het_luot_thi_nem_loi_cuoi(self) -> None:
        """
        Quan trọng: phải ném lỗi GỐC, không phải một exception chung chung.

        Chỗ gọi cần biết chính xác lỗi gì để xử lý đúng.
        """
        calls = 0

        @retry(attempts=3, base_delay=0.01)
        async def f() -> str:
            nonlocal calls
            calls += 1
            raise TimeoutError(f"lần {calls}")

        with pytest.raises(TimeoutError, match="lần 3"):
            await f()
        assert calls == 3

    async def test_chi_retry_dung_loai_exception(self) -> None:
        """
        Không retry với ValueError: request sai thì gửi lại 1000 lần vẫn sai.

        Retry bừa là cách nhanh nhất để tự làm sập hệ thống của mình.
        """
        calls = 0

        @retry(attempts=3, base_delay=0.01, exceptions=(ConnectionError,))
        async def f() -> str:
            nonlocal calls
            calls += 1
            raise ValueError("tham số sai")

        with pytest.raises(ValueError):
            await f()
        assert calls == 1  # KHÔNG retry

    async def test_giu_nguyen_metadata_cua_ham_goc(self) -> None:
        """
        functools.wraps: thiếu nó thì mọi hàm đều tên "wrapper", traceback
        và log trở nên vô dụng.
        """

        @retry(attempts=1)
        async def ham_co_ten_rieng() -> None:
            """Docstring gốc."""

        assert ham_co_ten_rieng.__name__ == "ham_co_ten_rieng"
        assert ham_co_ten_rieng.__doc__ == "Docstring gốc."

    async def test_backoff_tang_dan(self) -> None:
        """
        Kiểm tra thời gian chờ THỰC SỰ tăng lên.

        base_delay=0.05, backoff=2, jitter=False
        -> chờ 0.05 rồi 0.10 = tổng tối thiểu 0.15s
        """
        import time

        @retry(attempts=3, base_delay=0.05, backoff=2.0, jitter=False)
        async def f() -> None:
            raise ConnectionError("lỗi")

        start = time.perf_counter()
        with pytest.raises(ConnectionError):
            await f()
        assert time.perf_counter() - start >= 0.14


class TestTimeout:
    async def test_ham_nhanh_thi_khong_bi_huy(self) -> None:
        @with_timeout(1.0)
        async def f() -> str:
            await asyncio.sleep(0.01)
            return "ok"

        assert await f() == "ok"

    async def test_ham_cham_thi_bi_huy(self) -> None:
        """
        Mọi cuộc gọi ra ngoài PHẢI có timeout. Một request treo vô hạn sẽ
        giữ connection và cuối cùng làm cạn pool.
        """

        @with_timeout(0.05)
        async def f() -> str:
            await asyncio.sleep(5)
            return "khong-bao-gio-toi-day"

        with pytest.raises(TimeoutError):
            await f()


class TestTimed:
    async def test_van_tra_ve_ket_qua_dung(self) -> None:
        """Decorator đo thời gian không được làm thay đổi hành vi hàm."""

        @timed(threshold_ms=0)
        async def f(a: int, b: int) -> int:
            return a + b

        assert await f(2, 3) == 5

    async def test_do_thoi_gian_ca_khi_loi(self) -> None:
        """finally đảm bảo vẫn ghi log dù hàm ném lỗi."""

        @timed(threshold_ms=0)
        async def f() -> None:
            raise RuntimeError("lỗi")

        with pytest.raises(RuntimeError):
            await f()


class TestSingleflight:
    async def test_gop_cac_loi_goi_trung_nhau(self) -> None:
        """
        50 request cùng hỏi một câu khi cache vừa hết hạn -> chỉ 1 lần tính.

        Đây là cách chống "cache stampede" mà không cần lock phân tán.
        """
        calls = 0

        @singleflight()
        async def load(key: str) -> str:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return f"value-{key}"

        # gather chạy 10 lời gọi ĐỒNG THỜI với cùng tham số.
        results = await asyncio.gather(*[load("same") for _ in range(10)])

        assert results == ["value-same"] * 10
        assert calls == 1  # chỉ thực thi MỘT lần

    async def test_tham_so_khac_thi_chay_rieng(self) -> None:
        calls = 0

        @singleflight()
        async def load(key: str) -> str:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return key

        await asyncio.gather(load("a"), load("b"))
        assert calls == 2

    async def test_loi_khong_lam_ket_key_mai_mai(self) -> None:
        """
        finally trong singleflight dọn key -> lần gọi sau vẫn thực thi được.

        Thiếu dọn dẹp thì một lỗi duy nhất sẽ làm key đó chết vĩnh viễn.
        """
        calls = 0

        @singleflight()
        async def load() -> str:
            nonlocal calls
            calls += 1
            raise ValueError("lỗi")

        for _ in range(2):
            with pytest.raises(ValueError):
                await load()
        assert calls == 2
