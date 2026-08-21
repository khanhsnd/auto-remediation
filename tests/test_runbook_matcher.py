"""
Test RunbookMatcher - bước "check runbook".

Đây là test TÍCH HỢP nhẹ: dùng DB thật (SQLite in-memory) nhưng không cần
Redis/Kafka. Nó kiểm chứng cả query lẫn logic xếp hạng.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.incident import Incident
from app.db.models.runbook import Runbook, RunbookStep
from app.db.repositories.runbook import RunbookRepository
from app.domain.enums import ActionType, AlertSource, IncidentStatus, Severity
from app.services.runbook_matcher import RunbookMatcher


def make_incident(**kwargs: object) -> Incident:
    """Helper: incident trong bộ nhớ với giá trị mặc định hợp lý."""
    defaults: dict[str, object] = {
        "fingerprint": "fp",
        "source": AlertSource.CUSTOM,
        "title": "High memory usage",
        "severity": Severity.CRITICAL,
        "service": "payment-api",
        "environment": "production",
        "status": IncidentStatus.RECEIVED,
        "attempt_count": 0,
        "labels": {"alertname": "HighMemoryUsage"},
    }
    return Incident(**{**defaults, **kwargs})  # type: ignore[arg-type]


async def add_runbook(
    db: AsyncSession,
    name: str,
    *,
    priority: int = 100,
    rules: dict[str, object] | None = None,
    enabled: bool = True,
    steps: int = 1,
    max_attempts: int = 2,
) -> Runbook:
    """Helper tạo runbook - gom lại để test đọc gọn, không lặp code."""
    rb = Runbook(
        name=name,
        priority=priority,
        match_rules=rules or {},
        enabled=enabled,
        max_attempts=max_attempts,
    )
    for i in range(steps):
        rb.steps.append(
            RunbookStep(order_index=i, name=f"step-{i}", action_type=ActionType.NOOP, params={})
        )
    db.add(rb)
    await db.commit()
    return rb


class TestMatching:
    async def test_khop_runbook_dung_luat(self, db: AsyncSession) -> None:
        await add_runbook(
            db,
            "restart-oom",
            priority=10,
            rules={
                "all": [
                    {"field": "service", "op": "eq", "value": "payment-api"},
                    {"field": "labels.alertname", "op": "regex", "value": "(?i)memory"},
                ]
            },
        )
        matcher = RunbookMatcher(RunbookRepository(db))
        result = await matcher.match(make_incident())

        assert result.matched is True
        assert result.runbook is not None
        assert result.runbook.name == "restart-oom"

    async def test_khong_khop_thi_bao_ly_do(self, db: AsyncSession) -> None:
        """
        MatchResult mang theo lý do - người vận hành cần biết vì sao hệ thống
        không tự xử lý, chứ không chỉ nhận về None.
        """
        await add_runbook(
            db,
            "chi-cho-order-api",
            rules={"all": [{"field": "service", "op": "eq", "value": "order-api"}]},
        )
        matcher = RunbookMatcher(RunbookRepository(db))
        result = await matcher.match(make_incident())

        assert result.matched is False
        assert "Không runbook nào khớp" in result.reason
        assert "chi-cho-order-api" in result.candidates

    async def test_runbook_tat_thi_bo_qua(self, db: AsyncSession) -> None:
        """enabled=false là cách an toàn để vô hiệu runbook mà không mất lịch sử."""
        await add_runbook(db, "da-tat", rules={}, enabled=False)
        matcher = RunbookMatcher(RunbookRepository(db))
        result = await matcher.match(make_incident())

        assert result.matched is False
        assert "Chưa có runbook nào được bật" in result.reason

    async def test_khong_co_runbook_nao(self, db: AsyncSession) -> None:
        matcher = RunbookMatcher(RunbookRepository(db))
        result = await matcher.match(make_incident())
        assert result.matched is False


class TestRanking:
    async def test_priority_nho_hon_thang(self, db: AsyncSession) -> None:
        """Quy ước: số nhỏ = ưu tiên cao (giống nice của Linux)."""
        await add_runbook(db, "uu-tien-thap", priority=200, rules={})
        await add_runbook(db, "uu-tien-cao", priority=10, rules={})

        matcher = RunbookMatcher(RunbookRepository(db))
        result = await matcher.match(make_incident())

        assert result.runbook is not None
        assert result.runbook.name == "uu-tien-cao"

    async def test_cung_priority_thi_luat_cu_the_hon_thang(self, db: AsyncSession) -> None:
        """
        Đây là lý do có hàm specificity(): runbook catch-all không được
        giành alert của runbook chuyên biệt.
        """
        await add_runbook(db, "catch-all", priority=50, rules={})
        await add_runbook(
            db,
            "chuyen-biet",
            priority=50,
            rules={
                "all": [
                    {"field": "service", "op": "eq", "value": "payment-api"},
                    {"field": "severity", "op": "eq", "value": "critical"},
                ]
            },
        )

        matcher = RunbookMatcher(RunbookRepository(db))
        result = await matcher.match(make_incident())

        assert result.runbook is not None
        assert result.runbook.name == "chuyen-biet"

    async def test_ket_qua_on_dinh_khi_hoan_toan_bang_nhau(self, db: AsyncSession) -> None:
        """
        Tiêu chí cuối là tên -> chạy nhiều lần vẫn ra cùng kết quả.

        Vì sao quan trọng? Hệ thống tự động phải TIÊN ĐOÁN ĐƯỢC. Nếu kết quả
        đổi tuỳ lúc thì không ai debug được.
        """
        await add_runbook(db, "bbb", priority=50, rules={})
        await add_runbook(db, "aaa", priority=50, rules={})

        matcher = RunbookMatcher(RunbookRepository(db))
        for _ in range(5):
            result = await matcher.match(make_incident())
            assert result.runbook is not None
            assert result.runbook.name == "aaa"


class TestGuards:
    async def test_runbook_khong_co_step_thi_khong_dung(self, db: AsyncSession) -> None:
        await add_runbook(db, "rong", rules={}, steps=0)
        matcher = RunbookMatcher(RunbookRepository(db))
        result = await matcher.match(make_incident())

        assert result.matched is False
        assert "không có step nào" in result.reason

    async def test_vuot_max_attempts_thi_dung(self, db: AsyncSession) -> None:
        """
        Chốt an toàn quan trọng nhất: không retry vô hạn.

        Nếu thiếu, hệ thống sẽ restart service hàng nghìn lần khi nguyên nhân
        gốc chưa được sửa.
        """
        await add_runbook(db, "gioi-han-2", rules={}, max_attempts=2)
        matcher = RunbookMatcher(RunbookRepository(db))

        result = await matcher.match(make_incident(attempt_count=2))

        assert result.matched is False
        assert "cần người can thiệp" in result.reason


@pytest.mark.parametrize("severity", [Severity.CRITICAL, Severity.HIGH])
async def test_luat_theo_trong_so_severity(db: AsyncSession, severity: Severity) -> None:
    """severity_weight cho phép viết luật dạng ngưỡng thay vì liệt kê từng giá trị."""
    await add_runbook(
        db,
        "chi-nghiem-trong",
        rules={"all": [{"field": "severity_weight", "op": "gte", "value": 3}]},
    )
    matcher = RunbookMatcher(RunbookRepository(db))
    result = await matcher.match(make_incident(severity=severity))
    assert result.matched is True


async def test_severity_thap_thi_khong_khop(db: AsyncSession) -> None:
    await add_runbook(
        db,
        "chi-nghiem-trong",
        rules={"all": [{"field": "severity_weight", "op": "gte", "value": 3}]},
    )
    matcher = RunbookMatcher(RunbookRepository(db))
    result = await matcher.match(make_incident(severity=Severity.LOW))
    assert result.matched is False
