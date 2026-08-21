"""
Test cơ chế Executor: registry tự động, đa hình, render tham số.

Đây là chỗ kiểm chứng các kỹ thuật Python nâng cao đã dùng:
__init_subclass__, ABC, ClassVar, đa hình.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.enums import ActionType
from app.services.executors import ExecutionContext, Executor, StepOutcome, get_executor
from app.services.executors.simple import NoopExecutor, WaitExecutor


def make_ctx(**kwargs: object) -> ExecutionContext:
    defaults: dict[str, object] = {
        "incident_id": "inc-1",
        "execution_id": "exe-1",
        "service": "payment-api",
        "environment": "production",
        "severity": "critical",
        "labels": {"pod": "payment-7d9f", "alertname": "HighMemory"},
    }
    return ExecutionContext(**{**defaults, **kwargs})  # type: ignore[arg-type]


class TestRegistry:
    def test_moi_action_type_deu_co_executor(self) -> None:
        """
        Registry được điền TỰ ĐỘNG nhờ __init_subclass__ - chỉ cần import
        module là class con tự đăng ký.

        Test này bảo vệ khỏi lỗi "thêm ActionType mới mà quên viết executor",
        vì engine sẽ nổ KeyError lúc chạy production.
        """
        for action in ActionType:
            executor = get_executor(action)
            assert isinstance(executor, Executor)
            assert executor.action_type is action

    def test_action_khong_ton_tai_thi_bao_loi_ro_rang(self) -> None:
        """Thông báo lỗi liệt kê các action có sẵn -> debug nhanh."""
        with pytest.raises(KeyError, match="Không có executor"):
            get_executor("khong_ton_tai")  # type: ignore[arg-type]

    def test_khong_cho_dang_ky_trung_action_type(self) -> None:
        """
        __init_subclass__ chặn hai executor cùng nhận một ActionType.

        Nếu không chặn, class nào import sau sẽ âm thầm ghi đè class trước ->
        hệ thống chạy sai mà không ai biết.
        """
        with pytest.raises(TypeError, match="đã được"):

            class TrungLap(Executor):
                action_type = ActionType.NOOP  # đã có NoopExecutor

                async def run(self, ctx: ExecutionContext, params: dict) -> StepOutcome:
                    return StepOutcome.ok()

    def test_khong_the_khoi_tao_lop_truu_tuong(self) -> None:
        """ABC + @abstractmethod: quên hiện thực run() là báo lỗi ngay."""
        with pytest.raises(TypeError):
            Executor()  # type: ignore[abstract]


class TestTemplateRendering:
    def test_thay_bien_co_ban(self) -> None:
        ctx = make_ctx()
        assert ctx.render("http://api/{service}/restart") == "http://api/payment-api/restart"

    def test_thay_bien_tu_labels(self) -> None:
        """Label của alert cũng dùng được làm biến -> runbook linh hoạt hơn nhiều."""
        ctx = make_ctx()
        assert ctx.render("kubectl delete pod {pod}") == "kubectl delete pod payment-7d9f"

    def test_bien_khong_ton_tai_thi_giu_nguyen(self) -> None:
        """
        Thiếu biến -> giữ chuỗi gốc, KHÔNG làm sập cả runbook.

        Quyết định thiết kế: một step sai template không nên chặn các step
        khác đang cứu hệ thống.
        """
        ctx = make_ctx()
        assert ctx.render("http://api/{khong_co}") == "http://api/{khong_co}"

    def test_render_dict_long_nhau(self) -> None:
        """render_params đệ quy qua dict/list - params thật hay lồng nhiều tầng."""
        ctx = make_ctx()
        result = ctx.render_params(
            {
                "url": "http://api/{service}",
                "json": {"spec": {"name": "{service}-{environment}"}},
                "tags": ["{severity}", "static"],
                "replicas": 5,  # số thì giữ nguyên
            }
        )
        assert result["url"] == "http://api/payment-api"
        assert result["json"]["spec"]["name"] == "payment-api-production"
        assert result["tags"] == ["critical", "static"]
        assert result["replicas"] == 5

    def test_khong_thuc_thi_code_trong_template(self) -> None:
        """
        BẢO MẬT: format_map chỉ THAY biến, không eval.

        Nếu dùng eval() thì runbook trong DB trở thành lỗ hổng thực thi code
        từ xa. Test này chốt lại lựa chọn thiết kế đó.
        """
        ctx = make_ctx()
        payload = "{__import__('os').system('rm -rf /')}"
        # Không raise, và cũng không thực thi gì - chỉ trả về chuỗi gốc.
        assert ctx.render(payload) == payload


class TestOutputChaining:
    def test_step_sau_doc_duoc_ket_qua_step_truoc(self) -> None:
        """
        ctx.outputs là cầu nối giữa các step.

        Ví dụ thật: step 1 gọi API lấy danh sách pod, step 2 restart pod đó.
        """
        ctx = make_ctx()
        ctx.outputs["Lay danh sach pod"] = {"pods": ["a", "b"]}
        assert ctx.outputs["Lay danh sach pod"]["pods"] == ["a", "b"]


class TestSimpleExecutors:
    async def test_noop_luon_thanh_cong(self) -> None:
        outcome = await NoopExecutor().run(make_ctx(), {"note": "test"})
        assert outcome.success is True
        assert outcome.output["noop"] is True

    async def test_wait_gioi_han_thoi_gian_toi_da(self) -> None:
        """
        Runbook lỡ ghi 999999 giây thì không được treo worker.

        Chặn trên 300s là một "sanity guard" - luôn nên có với mọi giá trị
        đến từ dữ liệu bên ngoài.

        Dùng dry_run để đọc giá trị đã bị kẹp mà KHÔNG phải ngủ thật -
        nếu không, chính test này sẽ treo 5 phút.
        """
        outcome = await WaitExecutor().run(make_ctx(dry_run=True), {"seconds": 999_999})
        assert outcome.output["would_wait"] == 300.0

    async def test_wait_dry_run_khong_ngu_that(self) -> None:
        ctx = make_ctx(dry_run=True)
        outcome = await WaitExecutor().run(ctx, {"seconds": 100})
        assert outcome.success is True
        assert outcome.output["dry_run"] is True

    async def test_rollback_mac_dinh_khong_lam_gi(self) -> None:
        """
        Executor không hỗ trợ rollback thì dùng hành vi mặc định của lớp cha
        (template method pattern), không phải override rỗng ở từng class.
        """
        outcome = await NoopExecutor().rollback(make_ctx(), {})
        assert outcome.success is True
        assert "không hỗ trợ rollback" in outcome.output["skipped"]


class TestStepOutcome:
    def test_factory_ok_va_fail(self) -> None:
        ok = StepOutcome.ok(stdout="done", code=0)
        assert ok.success is True
        assert ok.output == {"stdout": "done", "code": 0}
        assert ok.error == ""

        fail = StepOutcome.fail("timeout", exit_code=1)
        assert fail.success is False
        assert fail.error == "timeout"
        assert fail.output == {"exit_code": 1}

    def test_outcome_bat_bien(self) -> None:
        """frozen -> không ai sửa được kết quả sau khi step đã chạy xong."""
        outcome = StepOutcome.ok()
        with pytest.raises(AttributeError):
            outcome.success = False  # type: ignore[misc]


class TestNotifyExecutor:
    """
    Test executor NOTIFY - minh hoạ vì sao Protocol tiện cho việc test.

    Không cần Kafka, không cần mock library: chỉ cần một class 5 dòng có đúng
    method `publish` là hợp lệ với PublisherPort (structural typing).
    """

    async def test_dung_publisher_dung_chung(self) -> None:
        from app.services.executors.simple import NotifyExecutor

        sent: list[dict[str, Any]] = []

        class FakePublisher:
            """Không kế thừa PublisherPort - chỉ cần có đúng method là đủ."""

            async def publish(
                self,
                topic: str,
                payload: dict[str, Any],
                *,
                key: str | None = None,
                event_type: str = "",
            ) -> None:
                sent.append({"topic": topic, "payload": payload, "event_type": event_type})

        ctx = make_ctx(publisher=FakePublisher())
        outcome = await NotifyExecutor().run(
            ctx, {"message": "Đã restart {service}", "channel": "sre", "level": "warning"}
        )

        assert outcome.success is True
        assert outcome.output["notified"] is True
        assert len(sent) == 1
        # Template đã được render với giá trị thật.
        assert sent[0]["payload"]["message"] == "Đã restart payment-api"
        assert sent[0]["event_type"] == "notification"

    async def test_khong_co_publisher_van_thanh_cong(self) -> None:
        """
        Kafka tắt -> chỉ log, KHÔNG được coi là thất bại.

        Gửi thông báo lỗi mà làm sập cả runbook thì Slack sập là hệ thống mất
        luôn khả năng tự phục hồi.
        """
        from app.services.executors.simple import NotifyExecutor

        outcome = await NotifyExecutor().run(make_ctx(), {"message": "test"})

        assert outcome.success is True
        assert outcome.output["notified"] is False
        assert outcome.output["reason"] == "no_publisher"
