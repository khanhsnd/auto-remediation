"""
Test máy trạng thái của Incident.

VÌ SAO TEST RIÊNG PHẦN NÀY? Vì trạng thái sai dẫn tới hành vi sai rất khó
truy: incident đã RESOLVED mà bị chạy runbook lại, hoặc incident FAILED bị
kẹt mãi không ai xử lý.
"""

from __future__ import annotations

import pytest

from app.core.exceptions import ConflictError
from app.db.models.incident import Incident
from app.domain.enums import AlertSource, IncidentStatus, Severity


def make_incident(status: IncidentStatus = IncidentStatus.RECEIVED) -> Incident:
    """
    Factory tạo incident trong bộ nhớ (không cần DB).

    Object ORM chưa persist vẫn dùng được như object Python bình thường -
    rất tiện để test logic thuần.
    """
    return Incident(
        fingerprint="fp",
        source=AlertSource.CUSTOM,
        title="test",
        severity=Severity.HIGH,
        service="svc",
        status=status,
        attempt_count=0,
    )


class TestTransitions:
    @pytest.mark.parametrize(
        ("start", "target"),
        [
            (IncidentStatus.RECEIVED, IncidentStatus.TRIAGED),
            (IncidentStatus.RECEIVED, IncidentStatus.ESCALATED),
            (IncidentStatus.RECEIVED, IncidentStatus.SUPPRESSED),
            (IncidentStatus.TRIAGED, IncidentStatus.REMEDIATING),
            (IncidentStatus.REMEDIATING, IncidentStatus.RESOLVED),
            (IncidentStatus.REMEDIATING, IncidentStatus.FAILED),
            # FAILED -> REMEDIATING là cơ chế retry lần 2.
            (IncidentStatus.FAILED, IncidentStatus.REMEDIATING),
            (IncidentStatus.FAILED, IncidentStatus.ESCALATED),
        ],
    )
    def test_buoc_chuyen_hop_le(self, start: IncidentStatus, target: IncidentStatus) -> None:
        incident = make_incident(start)
        incident.transition_to(target)
        assert incident.status is target

    @pytest.mark.parametrize(
        ("start", "target"),
        [
            # Nhảy tắt: chưa triage mà đã remediating.
            (IncidentStatus.RECEIVED, IncidentStatus.REMEDIATING),
            # Chưa làm gì mà đã resolved.
            (IncidentStatus.RECEIVED, IncidentStatus.RESOLVED),
            # Trạng thái cuối không đi đâu nữa.
            (IncidentStatus.RESOLVED, IncidentStatus.REMEDIATING),
            (IncidentStatus.ESCALATED, IncidentStatus.REMEDIATING),
            (IncidentStatus.SUPPRESSED, IncidentStatus.TRIAGED),
        ],
    )
    def test_buoc_chuyen_khong_hop_le_thi_raise(
        self, start: IncidentStatus, target: IncidentStatus
    ) -> None:
        """
        Đây chính là giá trị của máy trạng thái: chặn bug ngay tại nguồn,
        kèm thông báo nói rõ được phép chuyển sang đâu.
        """
        incident = make_incident(start)
        with pytest.raises(ConflictError, match="Không thể chuyển"):
            incident.transition_to(target)

    def test_chuyen_ve_chinh_no_thi_khong_lam_gi(self) -> None:
        """
        Idempotent: gọi lại cùng trạng thái không raise.

        Cần tính chất này vì Kafka có thể gửi lại message (at-least-once).
        """
        incident = make_incident(IncidentStatus.TRIAGED)
        incident.transition_to(IncidentStatus.TRIAGED)
        assert incident.status is IncidentStatus.TRIAGED


class TestSideEffects:
    def test_remediating_tang_attempt_count(self) -> None:
        """
        Tự động tăng bộ đếm -> không ai quên. Nếu để chỗ gọi tự tăng thì
        chắc chắn sẽ có nơi quên, dẫn tới retry vô hạn.
        """
        incident = make_incident(IncidentStatus.TRIAGED)
        assert incident.attempt_count == 0
        incident.transition_to(IncidentStatus.REMEDIATING)
        assert incident.attempt_count == 1

    def test_resolved_set_thoi_gian_va_xoa_loi(self) -> None:
        incident = make_incident(IncidentStatus.REMEDIATING)
        incident.last_error = "loi cu"
        incident.transition_to(IncidentStatus.RESOLVED)

        assert incident.resolved_at is not None
        assert incident.last_error == ""

    def test_escalated_luu_ly_do(self) -> None:
        incident = make_incident(IncidentStatus.RECEIVED)
        incident.transition_to(IncidentStatus.ESCALATED, error="không có runbook")

        assert incident.escalated_at is not None
        assert incident.last_error == "không có runbook"

    def test_failed_luu_loi(self) -> None:
        incident = make_incident(IncidentStatus.REMEDIATING)
        incident.transition_to(IncidentStatus.FAILED, error="health check fail")
        assert incident.last_error == "health check fail"


def test_trang_thai_cuoi() -> None:
    """is_terminal dùng để bỏ qua incident đã xong -> tránh xử lý lại."""
    assert IncidentStatus.RESOLVED.is_terminal is True
    assert IncidentStatus.ESCALATED.is_terminal is True
    assert IncidentStatus.SUPPRESSED.is_terminal is True
    assert IncidentStatus.RECEIVED.is_terminal is False
    # FAILED KHÔNG phải terminal: còn có thể retry.
    assert IncidentStatus.FAILED.is_terminal is False


def test_chu_ky_song_day_du() -> None:
    """Đi trọn một vòng đời thành công."""
    incident = make_incident()
    for target in (
        IncidentStatus.TRIAGED,
        IncidentStatus.REMEDIATING,
        IncidentStatus.RESOLVED,
    ):
        incident.transition_to(target)

    assert incident.status is IncidentStatus.RESOLVED
    assert incident.attempt_count == 1
    assert incident.resolved_at is not None
