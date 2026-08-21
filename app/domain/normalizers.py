"""
Dịch payload thô của bên thứ 3 -> Alert chuẩn hoá.

KIẾN THỨC PYTHON NÂNG CAO Ở ĐÂY
  1. match / case  - structural pattern matching (Python 3.10+).
                     Không phải "switch" tầm thường: nó bóc tách được cấu
                     trúc dict/list lồng nhau và gán biến ngay khi khớp.
  2. Protocol      - "duck typing có kiểm tra kiểu". Không cần kế thừa,
                     chỉ cần có đúng method là hợp lệ.
  3. Registry dict - tra hàm theo key, thay cho chuỗi if-elif dài.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.core.exceptions import ValidationError
from app.domain.alert import Alert
from app.domain.enums import AlertSource, Severity

# Kiểu hàm normalizer: nhận dict thô, trả về Alert.
# Callable[[đối số], kết quả] - đây là "type alias", đặt tên cho một kiểu.
Normalizer = Callable[[dict[str, Any]], Alert]


def _parse_time(value: Any) -> datetime:
    """
    Parse thời gian từ nhiều định dạng khác nhau bằng match/case.

    Đây là chỗ match/case toả sáng: mỗi case vừa KIỂM TRA KIỂU vừa GÁN BIẾN.
    """
    match value:
        # case <Kiểu>() -> kiểm tra isinstance
        case datetime():
            # Nếu thiếu timezone thì coi như UTC (tránh so sánh naive/aware bị lỗi).
            return value if value.tzinfo else value.replace(tzinfo=UTC)

        # Guard "if" phía sau case: chỉ khớp khi điều kiện đúng.
        case int() | float() if value > 0:
            # Datadog gửi epoch milliseconds, Prometheus gửi seconds.
            seconds = value / 1000 if value > 1e11 else value
            return datetime.fromtimestamp(seconds, tz=UTC)

        case str() as s if s:
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                return datetime.now(UTC)
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

        # case _ là "mọi trường hợp còn lại" (giống else).
        case _:
            return datetime.now(UTC)


def _map_severity(raw: Any) -> Severity:
    """
    Ánh xạ mức độ của bên thứ 3 về Severity của mình.

    Nhiều bên dùng từ khác nhau cho cùng một ý: critical/fatal/P1/error...
    """
    text = str(raw or "").strip().lower()
    match text:
        case "critical" | "fatal" | "emergency" | "p1" | "sev1":
            return Severity.CRITICAL
        case "high" | "error" | "major" | "p2" | "sev2":
            return Severity.HIGH
        case "medium" | "warning" | "warn" | "p3" | "sev3":
            return Severity.MEDIUM
        case "low" | "minor" | "p4":
            return Severity.LOW
        case _:
            return Severity.INFO


def normalize_prometheus(payload: dict[str, Any]) -> Alert:
    """
    Alertmanager gửi dạng:
        {"alerts": [{"labels": {...}, "annotations": {...}, "startsAt": "..."}]}

    Ở đây xử lý alert đầu tiên; luồng nhiều alert xem hàm normalize_batch().
    """
    match payload:
        # Bóc tách sâu: đòi payload phải có key "alerts" là list KHÔNG rỗng,
        # phần tử đầu là dict, và gán luôn vào biến `first`.
        case {"alerts": [{"labels": dict()} as first, *_]}:
            labels: dict[str, str] = {str(k): str(v) for k, v in first["labels"].items()}
            annotations = first.get("annotations") or {}
            return Alert(
                source=AlertSource.PROMETHEUS,
                external_id=str(first.get("fingerprint") or labels.get("alertname", "unknown")),
                title=labels.get("alertname", "PrometheusAlert"),
                severity=_map_severity(labels.get("severity")),
                service=labels.get("service") or labels.get("job", "unknown"),
                environment=labels.get("environment", "production"),
                description=str(annotations.get("description") or annotations.get("summary", "")),
                labels=labels,
                raw_payload=payload,
                occurred_at=_parse_time(first.get("startsAt")),
            )
        case _:
            raise ValidationError(
                "Payload Prometheus không đúng định dạng (thiếu 'alerts')",
                details={"received_keys": sorted(payload)},
            )


def normalize_datadog(payload: dict[str, Any]) -> Alert:
    """Datadog webhook: các key phẳng như alert_id, title, priority, tags."""
    match payload:
        case {"alert_id": alert_id, "title": title}:
            # Datadog gửi tags dạng ["env:prod", "service:payment"] -> tách ra dict.
            tags = _parse_tags(payload.get("tags"))
            return Alert(
                source=AlertSource.DATADOG,
                external_id=str(alert_id),
                title=str(title),
                severity=_map_severity(payload.get("priority") or payload.get("alert_type")),
                service=tags.get("service", "unknown"),
                environment=tags.get("env", "production"),
                description=str(payload.get("body", ""))[:2000],
                labels=tags,
                raw_payload=payload,
                occurred_at=_parse_time(payload.get("date")),
            )
        case _:
            raise ValidationError("Payload Datadog thiếu alert_id/title")


def normalize_generic(payload: dict[str, Any]) -> Alert:
    """
    Định dạng tự do của mình - dùng cho script nội bộ hoặc để test bằng curl.

    Chấp nhận cả "service" lẫn "service_name" cho linh hoạt (toán tử |
    trong pattern nghĩa là "hoặc").
    """
    match payload:
        case {"title": str(title), **rest}:
            labels = {str(k): str(v) for k, v in (rest.get("labels") or {}).items()}
            return Alert(
                source=AlertSource.CUSTOM,
                external_id=str(rest.get("id") or rest.get("external_id") or title),
                title=title,
                severity=_map_severity(rest.get("severity")),
                service=str(rest.get("service") or rest.get("service_name") or "unknown"),
                environment=str(rest.get("environment", "production")),
                description=str(rest.get("description", "")),
                labels=labels,
                raw_payload=payload,
                occurred_at=_parse_time(rest.get("occurred_at")),
            )
        case _:
            raise ValidationError("Payload tối thiểu phải có 'title'")


def _parse_tags(raw: Any) -> dict[str, str]:
    """
    ["env:prod", "team:sre", "adhoc"] -> {"env": "prod", "team": "sre", "adhoc": ""}

    Walrus operator (:=) vừa gán vừa dùng trong 1 biểu thức - ở đây giúp
    tránh gọi split() hai lần.
    """
    if not isinstance(raw, list):
        return {}
    out: dict[str, str] = {}
    for item in raw:
        text = str(item)
        if (idx := text.find(":")) > 0:
            out[text[:idx]] = text[idx + 1 :]
        else:
            out[text] = ""
    return out


# Registry: map nguồn -> hàm xử lý. Thêm nguồn mới chỉ cần thêm 1 dòng,
# không phải sửa hàm nào -> đúng nguyên tắc Open/Closed của SOLID.
NORMALIZERS: dict[AlertSource, Normalizer] = {
    AlertSource.PROMETHEUS: normalize_prometheus,
    AlertSource.DATADOG: normalize_datadog,
    AlertSource.GRAFANA: normalize_prometheus,  # Grafana dùng format Alertmanager
    AlertSource.CUSTOM: normalize_generic,
    AlertSource.ZABBIX: normalize_generic,
}


def normalize(source: AlertSource, payload: dict[str, Any]) -> Alert:
    """Điểm vào duy nhất cho tầng API - chọn normalizer theo nguồn."""
    normalizer = NORMALIZERS.get(source)
    if normalizer is None:
        raise ValidationError(f"Chưa hỗ trợ nguồn alert: {source}")
    return normalizer(payload)
