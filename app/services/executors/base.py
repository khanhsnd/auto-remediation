"""
Nền tảng cho Executor - hiện thực STRATEGY PATTERN.

BÀI TOÁN: mỗi loại hành động (gọi HTTP, gọi gRPC, chờ, thông báo) có cách
thực thi hoàn toàn khác nhau. Nếu viết:

    if action == "http_call":   ...50 dòng...
    elif action == "grpc":      ...50 dòng...
    elif action == "wait":      ...

thì hàm đó phình ra vô tận và mỗi lần thêm action là phải sửa nó (vi phạm
nguyên tắc Open/Closed trong SOLID).

GIẢI PHÁP: mỗi action một class Executor riêng, tự đăng ký vào registry.
Thêm action mới = thêm 1 file, KHÔNG sửa dòng nào của code cũ.

KIẾN THỨC PYTHON NÂNG CAO
  1. ABC + @abstractmethod  - bắt buộc class con phải hiện thực method
  2. __init_subclass__      - tự động đăng ký class con vào registry
                              (nhẹ và dễ hiểu hơn metaclass)
  3. ClassVar               - biến thuộc class, không thuộc instance
  4. dataclass làm context  - truyền dữ liệu giữa các bước
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from app.core.logging import get_logger
from app.domain.enums import ActionType

logger = get_logger(__name__)


class PublisherPort(Protocol):
    """
    Interface tối thiểu mà executor cần để gửi thông báo.

    VÌ SAO DÙNG Protocol Ở ĐÂY? Tầng executor không nên biết Kafka tồn tại.
    Nó chỉ cần "một cái gì đó có method publish". Nhờ vậy:
      - đổi Kafka sang RabbitMQ chỉ sửa tầng infra
      - test truyền vào một object giả 5 dòng là xong

    Đây chính là chữ D (Dependency Inversion) và I (Interface Segregation)
    của SOLID: phụ thuộc vào cái trừu tượng, và cái trừu tượng đó nhỏ nhất
    có thể.
    """

    async def publish(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        key: str | None = None,
        event_type: str = "",
    ) -> None: ...


@dataclass(slots=True)
class ExecutionContext:
    """
    Dữ liệu đi kèm suốt một lượt chạy runbook.

    VÌ SAO CẦN? Vì step sau thường cần kết quả của step trước. Ví dụ:
      step 1: gọi API lấy danh sách pod  -> lưu vào outputs["pods"]
      step 2: restart pod đầu tiên       -> đọc từ outputs["pods"]

    Không frozen vì `outputs` phải cập nhật được sau mỗi step.
    """

    incident_id: str
    execution_id: str
    service: str
    environment: str
    severity: str
    labels: dict[str, str] = field(default_factory=dict)

    # Kết quả của các step đã chạy: {"step_name": {...}}
    outputs: dict[str, Any] = field(default_factory=dict)

    dry_run: bool = False
    attempt: int = 1

    # Publisher dùng chung, do engine truyền vào.
    # VÌ SAO KHÔNG ĐỂ EXECUTOR TỰ TẠO? Producer Kafka có buffer nội bộ và
    # phải bắt tay với broker khi khởi tạo. Tạo mới ở từng step nghĩa là mỗi
    # lần thông báo lại kết nối lại - rất chậm và làm log ngập.
    publisher: PublisherPort | None = None

    def render(self, template: str) -> str:
        """
        Thay biến trong chuỗi tham số.

        Cho phép runbook viết:  "https://api/pods/{service}/restart"
        -> lúc chạy thành:      "https://api/pods/payment-api/restart"

        str.format_map(dict) an toàn hơn eval hay f-string động: nó CHỈ thay
        biến, không thực thi code. Tuyệt đối không dùng eval() cho dữ liệu
        đến từ DB hay người dùng.
        """
        values: dict[str, Any] = {
            "incident_id": self.incident_id,
            "execution_id": self.execution_id,
            "service": self.service,
            "environment": self.environment,
            "severity": self.severity,
            "attempt": self.attempt,
            **self.labels,
        }
        try:
            return template.format_map(values)
        except (KeyError, IndexError, ValueError):
            # Thiếu biến -> giữ nguyên chuỗi gốc thay vì làm sập cả runbook.
            logger.warning("Không render được template: %s", template)
            return template

    def render_params(self, params: Any) -> Any:
        """
        Render đệ quy toàn bộ dict tham số.

        Đệ quy vì params có thể lồng nhau: {"json": {"name": "{service}"}}.
        match/case phân loại theo kiểu dữ liệu rất gọn cho việc này.
        """
        match params:
            case str() as s:
                return self.render(s)
            case dict() as d:
                return {k: self.render_params(v) for k, v in d.items()}
            case list() as items:
                return [self.render_params(v) for v in items]
            case _:
                return params


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """
    Kết quả của một step.

    Dùng object thay vì tuple (success, output, error): đọc code rõ nghĩa hơn
    nhiều, và thêm field mới không làm vỡ chỗ gọi.
    """

    success: bool
    output: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @classmethod
    def ok(cls, **output: Any) -> StepOutcome:
        """
        Factory method cho trường hợp thành công.

        Viết StepOutcome.ok(stdout="done") gọn hơn
        StepOutcome(success=True, output={"stdout": "done"}).
        """
        return cls(success=True, output=output)

    @classmethod
    def fail(cls, error: str, **output: Any) -> StepOutcome:
        return cls(success=False, output=output, error=error)


class Executor(ABC):
    """
    Lớp trừu tượng cho mọi executor.

    ABC (Abstract Base Class): không tạo instance trực tiếp được. Class con
    quên hiện thực `run` sẽ báo lỗi ngay lúc khởi tạo, không phải lúc chạy.

    KHÁC BIỆT VỚI PROTOCOL:
      - ABC      : dùng khi muốn ÉP kế thừa và chia sẻ code chung
      - Protocol : dùng khi chỉ cần "có method đúng chữ ký là được"
    Ở đây chọn ABC vì ta muốn có registry và hành vi mặc định dùng chung.
    """

    # ClassVar: thuộc tính của CLASS, không phải của instance. Mỗi class con
    # gán một giá trị khác nhau.
    action_type: ClassVar[ActionType]

    # Registry dùng CHUNG cho mọi class con (vì nó thuộc class cha).
    registry: ClassVar[dict[ActionType, type[Executor]]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """
        TỰ ĐỘNG ĐĂNG KÝ: chạy ngay khi Python định nghĩa xong một class con.

        Nhờ hook này, chỉ cần import module chứa executor là nó có mặt trong
        registry - không phải viết thêm dòng nào để "khai báo".

        Kiểm tra trùng action_type để tránh lỗi âm thầm khi copy-paste class.
        """
        super().__init_subclass__(**kwargs)

        action = getattr(cls, "action_type", None)
        if action is None:
            return  # class trung gian (abstract), không đăng ký

        if action in Executor.registry:
            raise TypeError(
                f"ActionType {action} đã được {Executor.registry[action].__name__} "
                f"đăng ký, {cls.__name__} bị trùng"
            )
        Executor.registry[action] = cls
        logger.debug("Đã đăng ký executor %s -> %s", action, cls.__name__)

    @abstractmethod
    async def run(self, ctx: ExecutionContext, params: dict[str, Any]) -> StepOutcome:
        """
        Thực thi hành động.

        @abstractmethod: class con BẮT BUỘC override, nếu không thì
        `MyExecutor()` sẽ raise TypeError.

        Quy ước quan trọng: KHÔNG raise khi hành động thất bại về mặt nghiệp
        vụ - hãy trả StepOutcome.fail(). Chỉ raise khi có lỗi thật sự bất
        thường (sai cấu hình, bug). Nhờ vậy engine phân biệt được
        "chạy xong nhưng không thành công" với "hệ thống đang có vấn đề".
        """
        ...

    async def rollback(self, ctx: ExecutionContext, params: dict[str, Any]) -> StepOutcome:
        """
        Hoàn tác. KHÔNG phải abstractmethod -> class con không cần override
        nếu không hỗ trợ rollback.

        Đây là kỹ thuật "template method": cha cung cấp hành vi mặc định hợp
        lý, con chỉ override khi cần.
        """
        return StepOutcome.ok(skipped="executor này không hỗ trợ rollback")


def get_executor(action: ActionType) -> Executor:
    """
    Factory: lấy executor theo action type.

    Import package executors ở TRONG hàm (không phải đầu file) để tránh
    import vòng: các executor con import base.py, mà hàm này lại cần chúng.
    """
    import app.services.executors  # noqa: F401 - import để kích hoạt đăng ký

    executor_cls = Executor.registry.get(action)
    if executor_cls is None:
        raise KeyError(
            f"Không có executor cho action '{action}'. "
            f"Hiện có: {sorted(str(a) for a in Executor.registry)}"
        )
    return executor_cls()
