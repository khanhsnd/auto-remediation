"""
Các tập giá trị hữu hạn của hệ thống.

VÌ SAO DÙNG ENUM THAY VÌ CHUỖI?
    status = "reslved"   # sai chính tả -> chạy vẫn OK, bug âm thầm
    status = Status.RESOLVED  # sai tên -> AttributeError ngay lập tức
Ngoài ra IDE gợi ý được, và đổi tên 1 chỗ là đổi cả project.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum, auto


class Severity(StrEnum):
    """Mức nghiêm trọng của alert - dùng để ưu tiên xử lý."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def weight(self) -> int:
        """
        Enum trong Python là CLASS thật -> thêm được property và method.

        weight dùng để sắp xếp hàng đợi: critical xử lý trước.
        """
        return _SEVERITY_WEIGHT[self]

    def at_least(self, other: Severity) -> bool:
        """True nếu self nghiêm trọng bằng hoặc hơn other."""
        return self.weight >= other.weight


_SEVERITY_WEIGHT: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class IncidentStatus(StrEnum):
    """
    Vòng đời của một incident - đây là một MÁY TRẠNG THÁI (state machine).

        RECEIVED --> TRIAGED --> REMEDIATING --> RESOLVED
             |           |             |
             |           |             +------> FAILED --> ESCALATED
             |           +--------------------> ESCALATED  (không match runbook)
             +--------------------------------> SUPPRESSED (trùng / đang bảo trì)
    """

    RECEIVED = "received"        # vừa nhận webhook, đã ghi vào DB
    TRIAGED = "triaged"          # đã match được runbook
    REMEDIATING = "remediating"  # đang chạy các bước khắc phục
    RESOLVED = "resolved"        # khắc phục thành công, health check pass
    FAILED = "failed"            # chạy runbook nhưng không cứu được
    ESCALATED = "escalated"      # đã bàn giao cho người thật
    SUPPRESSED = "suppressed"    # bỏ qua có chủ đích

    @property
    def is_terminal(self) -> bool:
        """
        Trạng thái cuối - không chuyển đi đâu nữa.

        frozenset: tập hợp bất biến, kiểm tra "in" là O(1).
        """
        return self in _TERMINAL_STATUSES


_TERMINAL_STATUSES = frozenset(
    {IncidentStatus.RESOLVED, IncidentStatus.ESCALATED, IncidentStatus.SUPPRESSED}
)

# Bảng chuyển trạng thái hợp lệ. Có bảng này thì không ai "nhảy" bừa từ
# RECEIVED sang RESOLVED được nữa - logic tập trung 1 chỗ, dễ audit.
ALLOWED_TRANSITIONS: dict[IncidentStatus, frozenset[IncidentStatus]] = {
    IncidentStatus.RECEIVED: frozenset(
        {IncidentStatus.TRIAGED, IncidentStatus.ESCALATED, IncidentStatus.SUPPRESSED}
    ),
    IncidentStatus.TRIAGED: frozenset(
        {IncidentStatus.REMEDIATING, IncidentStatus.ESCALATED, IncidentStatus.SUPPRESSED}
    ),
    IncidentStatus.REMEDIATING: frozenset(
        {IncidentStatus.RESOLVED, IncidentStatus.FAILED, IncidentStatus.ESCALATED}
    ),
    # FAILED vẫn cho phép REMEDIATING: đó là cơ chế retry lần 2, lần 3.
    IncidentStatus.FAILED: frozenset({IncidentStatus.REMEDIATING, IncidentStatus.ESCALATED}),
    IncidentStatus.RESOLVED: frozenset(),
    IncidentStatus.ESCALATED: frozenset(),
    IncidentStatus.SUPPRESSED: frozenset(),
}


class ExecutionStatus(StrEnum):
    """Trạng thái một lượt chạy runbook."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    ROLLED_BACK = "rolled_back"
    SKIPPED = "skipped"


class ActionType(StrEnum):
    """
    Loại hành động của một bước trong runbook.

    Mỗi giá trị sẽ có 1 class Executor tương ứng (Strategy Pattern),
    xem app/services/executors/.
    """

    HTTP_CALL = "http_call"          # gọi REST API (K8s, Jenkins, Cloud API...)
    GRPC_COMMAND = "grpc_command"    # nhờ agent trên node chạy lệnh
    HEALTH_CHECK = "health_check"    # kiểm tra service đã sống lại chưa
    WAIT = "wait"                    # chờ N giây cho service warm-up
    NOTIFY = "notify"                # bắn thông báo (Kafka -> Slack/Telegram)
    NOOP = "noop"                    # không làm gì - dùng để test luồng


class AlertSource(StrEnum):
    """Bên thứ 3 nào gửi alert tới. Mỗi bên có format payload riêng."""

    PROMETHEUS = "prometheus"
    DATADOG = "datadog"
    GRAFANA = "grafana"
    ZABBIX = "zabbix"
    CUSTOM = "custom"


class Priority(IntEnum):
    """
    IntEnum: member là int -> so sánh, sắp xếp, cộng trừ như số bình thường.

    auto() tự tăng: 1, 2, 3, 4. Dùng khi giá trị cụ thể không quan trọng.
    """

    P4_LOW = auto()
    P3_NORMAL = auto()
    P2_HIGH = auto()
    P1_URGENT = auto()
