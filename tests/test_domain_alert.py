"""Test chuẩn hoá alert và fingerprint - lá chắn chống trùng lặp."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.exceptions import ValidationError
from app.domain.alert import Alert
from app.domain.enums import AlertSource, Severity
from app.domain.normalizers import normalize, normalize_datadog, normalize_prometheus


class TestFingerprint:
    def test_cung_van_de_thi_cung_fingerprint(self) -> None:
        """
        ĐÂY LÀ TEST QUAN TRỌNG NHẤT của cả hệ thống dedupe.

        Cùng một sự cố, gửi lại ở thời điểm khác -> vân tay phải GIỐNG nhau,
        nếu không hệ thống sẽ tạo hàng trăm incident trùng và restart service
        hàng trăm lần.
        """
        base = {
            "source": AlertSource.PROMETHEUS,
            "external_id": "abc",
            "title": "High memory",
            "severity": Severity.CRITICAL,
            "service": "payment-api",
            "labels": {"alertname": "HighMemory", "pod": "payment-7d9f"},
        }
        a1 = Alert(**base, occurred_at=datetime(2026, 1, 1, tzinfo=UTC))
        a2 = Alert(**base, occurred_at=datetime(2026, 1, 1, 2, 30, tzinfo=UTC))

        assert a1.fingerprint() == a2.fingerprint()

    def test_khac_service_thi_khac_fingerprint(self) -> None:
        common = {
            "source": AlertSource.PROMETHEUS,
            "external_id": "abc",
            "title": "High memory",
            "severity": Severity.CRITICAL,
        }
        a1 = Alert(**common, service="payment-api")
        a2 = Alert(**common, service="order-api")
        assert a1.fingerprint() != a2.fingerprint()

    def test_label_bien_dong_khong_anh_huong(self) -> None:
        """
        Label như "value=91.2" thay đổi mỗi lần gửi. Nếu tính vào fingerprint
        thì dedupe mất tác dụng hoàn toàn -> chỉ dùng các label định danh.
        """
        common = {
            "source": AlertSource.PROMETHEUS,
            "external_id": "abc",
            "title": "High memory",
            "severity": Severity.CRITICAL,
            "service": "payment-api",
        }
        a1 = Alert(**common, labels={"alertname": "HighMemory", "value": "91.2"})
        a2 = Alert(**common, labels={"alertname": "HighMemory", "value": "93.7"})
        assert a1.fingerprint() == a2.fingerprint()

    def test_thu_tu_label_khong_anh_huong(self) -> None:
        """sorted() trong fingerprint đảm bảo điều này."""
        common = {
            "source": AlertSource.PROMETHEUS,
            "external_id": "abc",
            "title": "t",
            "severity": Severity.HIGH,
            "service": "s",
        }
        a1 = Alert(**common, labels={"alertname": "A", "pod": "p1"})
        a2 = Alert(**common, labels={"pod": "p1", "alertname": "A"})
        assert a1.fingerprint() == a2.fingerprint()


class TestImmutability:
    def test_alert_khong_sua_duoc(self) -> None:
        """
        frozen=True -> gán lại thuộc tính là raise.

        Alert là SỰ KIỆN ĐÃ XẢY RA, không ai được sửa lịch sử.
        """
        alert = Alert(
            source=AlertSource.CUSTOM,
            external_id="1",
            title="t",
            severity=Severity.LOW,
            service="s",
        )
        # dataclass frozen ném FrozenInstanceError (là con của AttributeError).
        with pytest.raises(AttributeError):
            alert.title = "doi"  # type: ignore[misc]

    def test_moi_alert_co_labels_rieng(self) -> None:
        """
        Kiểm tra bẫy "mutable default argument".

        Nếu dùng `labels: dict = {}` thì hai alert sẽ CHIA SẺ cùng một dict.
        default_factory=dict tránh được điều đó.
        """
        common = {
            "source": AlertSource.CUSTOM,
            "external_id": "1",
            "title": "t",
            "severity": Severity.LOW,
            "service": "s",
        }
        a1 = Alert(**common)
        a2 = Alert(**common)
        a1.labels["x"] = "1"
        assert a2.labels == {}


class TestNormalizers:
    def test_prometheus(self) -> None:
        payload = {
            "alerts": [
                {
                    "labels": {
                        "alertname": "HighMemoryUsage",
                        "severity": "critical",
                        "service": "payment-api",
                        "environment": "production",
                    },
                    "annotations": {"description": "Memory > 90%"},
                    "startsAt": "2026-01-15T10:30:00Z",
                    "fingerprint": "abc123",
                }
            ]
        }
        alert = normalize_prometheus(payload)

        assert alert.source is AlertSource.PROMETHEUS
        assert alert.severity is Severity.CRITICAL
        assert alert.service == "payment-api"
        assert alert.title == "HighMemoryUsage"
        assert alert.description == "Memory > 90%"
        assert alert.occurred_at.tzinfo is not None  # phải là timezone-aware

    def test_prometheus_payload_sai_thi_raise(self) -> None:
        with pytest.raises(ValidationError, match="thiếu 'alerts'"):
            normalize_prometheus({"khong_dung": True})

    def test_datadog_tach_tags(self) -> None:
        """Datadog gửi tags dạng list "key:value" -> phải tách thành dict."""
        payload = {
            "alert_id": "999",
            "title": "CPU cao",
            "priority": "P1",
            "tags": ["env:production", "service:order-api", "adhoc"],
            "date": 1_760_000_000_000,  # epoch milliseconds
        }
        alert = normalize_datadog(payload)

        assert alert.severity is Severity.CRITICAL  # P1 -> critical
        assert alert.service == "order-api"
        assert alert.environment == "production"
        assert alert.labels["adhoc"] == ""  # tag không có dấu hai chấm

    def test_generic_chi_can_title(self) -> None:
        alert = normalize(AlertSource.CUSTOM, {"title": "Chỉ có title"})
        assert alert.title == "Chỉ có title"
        assert alert.service == "unknown"
        assert alert.severity is Severity.INFO  # không khai báo -> mức thấp nhất

    def test_generic_thieu_title_thi_raise(self) -> None:
        with pytest.raises(ValidationError, match="title"):
            normalize(AlertSource.CUSTOM, {"service": "abc"})

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("fatal", Severity.CRITICAL),
            ("SEV1", Severity.CRITICAL),
            ("error", Severity.HIGH),
            ("warning", Severity.MEDIUM),
            ("minor", Severity.LOW),
            ("gi-do-la", Severity.INFO),
            (None, Severity.INFO),
        ],
    )
    def test_anh_xa_severity(self, raw: object, expected: Severity) -> None:
        """Mỗi hãng dùng từ khác nhau cho cùng một mức độ."""
        alert = normalize(AlertSource.CUSTOM, {"title": "t", "severity": raw})
        assert alert.severity is expected


def test_severity_so_sanh_duoc() -> None:
    """Enum có property và method -> dùng như object thật."""
    assert Severity.CRITICAL.weight > Severity.LOW.weight
    assert Severity.CRITICAL.at_least(Severity.HIGH) is True
    assert Severity.LOW.at_least(Severity.HIGH) is False
