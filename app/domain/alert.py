"""
Alert đã được CHUẨN HOÁ.

BÀI TOÁN THỰC TẾ: Prometheus, Datadog, Grafana, Zabbix... mỗi bên gửi JSON
một kiểu. Nếu để format thô đi sâu vào hệ thống thì mọi hàm phía sau đều
phải if-else theo nguồn -> code mục nhanh.

GIẢI PHÁP (Anti-Corruption Layer): ngay tại cửa vào, dịch mọi payload lạ
về một cấu trúc duy nhất là Alert. Bên trong hệ thống chỉ biết Alert.

KIẾN THỨC PYTHON
  1. @dataclass(frozen, slots, kw_only)
  2. field(default_factory=...) - bẫy mutable default argument
  3. hashlib -> tạo fingerprint để chống trùng lặp (dedupe)
  4. datetime timezone-aware
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.domain.enums import AlertSource, Severity


@dataclass(frozen=True, slots=True, kw_only=True)
class Alert:
    """
    Một alert đã chuẩn hoá.

    Ba tham số của dataclass ở đây đều có lý do:
      frozen=True  -> alert là SỰ KIỆN ĐÃ XẢY RA, không ai được sửa nó.
                      Bonus: frozen dataclass tự có __hash__ -> nhét set/dict được.
      slots=True   -> nhanh + nhẹ hơn (không có __dict__).
      kw_only=True -> bắt buộc gọi Alert(title=..., service=...).
                      Tránh thảm hoạ Alert("payment", "api", "high") mà không
                      ai biết tham số nào là gì.
    """

    source: AlertSource
    external_id: str          # id bên thứ 3 cấp, để đối chiếu 2 chiều
    title: str
    severity: Severity
    service: str              # service nào bị ảnh hưởng: "payment-api"
    environment: str = "production"
    description: str = ""

    # SAI:  labels: dict = {}          <- dict này bị CHIA SẺ giữa mọi instance!
    # ĐÚNG: default_factory=dict       <- mỗi instance một dict mới.
    # Đây là cái bẫy "mutable default argument" kinh điển của Python.
    labels: dict[str, str] = field(default_factory=dict)
    raw_payload: dict[str, Any] = field(default_factory=dict, repr=False)

    # datetime.now(UTC) chứ không phải datetime.now() (naive, không có tz).
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def fingerprint(self) -> str:
        """
        Vân tay của alert - dùng để CHỐNG TRÙNG.

        Thực tế: Prometheus gửi lại cùng một alert mỗi 30 giây trong suốt 2
        tiếng sự cố. Nếu không dedupe thì DB có 240 incident giống nhau và
        engine restart service 240 lần -> tự tay làm sập hệ thống.

        Fingerprint gom các thuộc tính định danh "cùng một vấn đề". Chú ý
        KHÔNG đưa timestamp vào, nếu không mỗi lần gửi lại ra vân tay khác.

        sorted(labels.items()) để thứ tự key trong JSON không ảnh hưởng kết quả.
        """
        material = "|".join(
            [
                str(self.source),
                self.service,
                self.environment,
                self.title,
                str(self.severity),
                # Chỉ lấy các label mang tính định danh, bỏ label biến động
                # (vd "value=91.2" thay đổi liên tục).
                ",".join(f"{k}={v}" for k, v in sorted(self.labels.items()) if k in _ID_LABELS),
            ]
        )
        # sha256 -> hex 64 ký tự; cắt 32 là đủ để không trùng trong thực tế.
        return hashlib.sha256(material.encode()).hexdigest()[:32]

    def match_context(self) -> dict[str, Any]:
        """
        Dữ liệu phẳng để rule engine so khớp.

        Toán tử ** (dict unpacking) gộp dict - nếu key trùng thì cái sau
        thắng, nên đặt labels ở dạng có tiền tố để không đè lên field chính.
        """
        return {
            "source": str(self.source),
            "service": self.service,
            "environment": self.environment,
            "severity": str(self.severity),
            "severity_weight": self.severity.weight,
            "title": self.title,
            "description": self.description,
            "labels": dict(self.labels),
        }


# Các label được coi là "định danh vấn đề". Tách hằng số ra ngoài để dễ sửa.
_ID_LABELS = frozenset({"alertname", "instance", "pod", "node", "namespace", "job", "check"})
