"""
RunbookMatcher - bước "check runbook" trong luồng nghiệp vụ.

NHIỆM VỤ: cho một incident, tìm runbook phù hợp nhất để chạy.

THUẬT TOÁN CHỌN
  1. Lấy mọi runbook đang enabled (có cache Redis để không query DB liên tục)
  2. Chạy rule engine so khớp từng cái
  3. Nhiều cái khớp -> xếp hạng theo:
        (a) priority nhỏ hơn thắng
        (b) cùng priority -> luật CỤ THỂ HƠN thắng (nhiều điều kiện hơn)
        (c) vẫn bằng -> theo tên, để kết quả ỔN ĐỊNH (deterministic)
  4. Kiểm tra cooldown: vừa chạy runbook này cho cùng incident thì chờ đã

KIẾN THỨC PYTHON
  1. sorted với key = tuple -> sắp xếp nhiều tiêu chí trong 1 dòng
  2. Cache-aside với Redis
  3. Kiểu trả về dạng NamedTuple/dataclass để mang theo lý do quyết định
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.logging import get_logger
from app.db.base import ensure_utc
from app.db.models.incident import Incident
from app.db.models.runbook import Runbook
from app.db.repositories.execution import ExecutionRepository
from app.db.repositories.runbook import RunbookRepository
from app.domain import rules
from app.infra.redis_client import RedisCache

logger = get_logger(__name__)

# Key cache. Đặt version ":v1" để khi đổi cấu trúc cache thì chỉ cần tăng
# version, không phải đi xoá key cũ bằng tay.
_CACHE_KEY = "runbooks:enabled:v1"


@dataclass(frozen=True, slots=True)
class MatchResult:
    """
    Kết quả so khớp - mang theo CẢ LÝ DO.

    Vì sao không chỉ trả về `Runbook | None`? Vì khi không match được, người
    vận hành cần biết TẠI SAO (không có runbook nào? hay có nhưng đang
    cooldown?). Thiếu thông tin này thì debug rất khổ.
    """

    runbook: Runbook | None
    reason: str
    candidates: tuple[str, ...] = ()

    @property
    def matched(self) -> bool:
        """Toán tử `is not None` gói lại cho chỗ gọi đọc dễ hơn."""
        return self.runbook is not None


class RunbookMatcher:
    """
    Service so khớp runbook.

    Nhận repository + cache qua constructor (Dependency Injection) -> test
    được bằng cách truyền đối tượng giả, không cần DB/Redis thật.
    """

    def __init__(
        self,
        repo: RunbookRepository,
        cache: RedisCache | None = None,
        executions: ExecutionRepository | None = None,
    ) -> None:
        self._repo = repo
        self._cache = cache
        # Optional: không có thì bỏ qua kiểm tra cooldown. Nhờ vậy test chỉ
        # quan tâm tới logic so khớp vẫn khởi tạo được matcher gọn nhẹ.
        self._executions = executions

    async def _load_runbooks(self) -> list[Runbook]:
        """
        Lấy danh sách runbook đang bật.

        LƯU Ý VỀ CACHE: ta KHÔNG cache object Runbook (ORM object không
        serialize được và còn gắn với session đã đóng). Thay vào đó chỉ cache
        DANH SÁCH ID, rồi load lại từ DB. Nghe hơi vòng vo, nhưng:
          - Query theo primary key rất nhanh và có thể dùng identity map
          - Tránh hoàn toàn lớp bug "detached instance"
        Trong project thật, nhiều nơi chọn cache DTO (dict) thay vì ORM object.
        """
        if self._cache is None:
            return list(await self._repo.list_enabled())

        cached_ids = await self._cache.get_json(_CACHE_KEY)
        if cached_ids is None:
            runbooks = list(await self._repo.list_enabled())
            await self._cache.set_json(_CACHE_KEY, [rb.id for rb in runbooks], ttl=60)
            return runbooks

        # Có cache: load theo id. Nếu id nào đã bị xoá thì bỏ qua.
        runbooks = []
        for rb_id in cached_ids:
            if (rb := await self._repo.get_with_steps(rb_id)) is not None and rb.enabled:
                runbooks.append(rb)

        # Cache lệch với DB (runbook bị tắt/xoá) -> làm mới cache.
        if len(runbooks) != len(cached_ids):
            await self._cache.delete(_CACHE_KEY)
        return runbooks

    async def invalidate_cache(self) -> None:
        """
        Xoá cache - gọi mỗi khi runbook được tạo/sửa/xoá.

        Đây là điểm yếu cố hữu của cache: PHẢI nhớ invalidate. Quên thì hệ
        thống chạy runbook cũ tới 60 giây sau khi ta đã sửa.
        """
        if self._cache is not None:
            await self._cache.delete(_CACHE_KEY)
            logger.info("Đã xoá cache runbook")

    async def match(self, incident: Incident) -> MatchResult:
        """
        Tìm runbook cho incident.

        Trả về MatchResult, không raise: "không tìm thấy" là kết quả nghiệp
        vụ hợp lệ (dẫn tới escalate), không phải lỗi hệ thống.
        """
        context = _incident_context(incident)
        runbooks = await self._load_runbooks()

        if not runbooks:
            return MatchResult(None, "Chưa có runbook nào được bật")

        # List comprehension với điều kiện - Pythonic hơn vòng for + append.
        matched = [rb for rb in runbooks if rules.matches(context, rb.match_rules)]

        if not matched:
            return MatchResult(
                None,
                f"Không runbook nào khớp (đã kiểm tra {len(runbooks)} runbook)",
                candidates=tuple(rb.name for rb in runbooks),
            )

        # SẮP XẾP NHIỀU TIÊU CHÍ bằng cách trả về tuple trong key.
        # Python so sánh tuple theo từng phần tử từ trái sang phải.
        # Dấu trừ trước specificity để "nhiều điều kiện hơn" xếp trước
        # (vì sorted mặc định tăng dần).
        matched.sort(key=lambda rb: (rb.priority, -rules.specificity(rb.match_rules), rb.name))

        best = matched[0]

        # Runbook không có step nào thì chạy cũng vô nghĩa.
        if not best.steps:
            return MatchResult(
                None,
                f"Runbook '{best.name}' khớp nhưng không có step nào",
                candidates=tuple(rb.name for rb in matched),
            )

        # Vừa chạy runbook này cho chính incident này -> phải chờ hết cooldown.
        if (wait_left := await self._cooldown_remaining(incident, best)) > 0:
            return MatchResult(
                None,
                f"Runbook '{best.name}' vừa chạy, còn {wait_left}s cooldown",
                candidates=tuple(rb.name for rb in matched),
            )

        # Đã chạy quá số lần cho phép -> dừng, gọi người.
        if incident.attempt_count >= best.max_attempts:
            return MatchResult(
                None,
                f"Đã thử {incident.attempt_count}/{best.max_attempts} lần với "
                f"'{best.name}', cần người can thiệp",
                candidates=tuple(rb.name for rb in matched),
            )

        logger.info(
            "Khớp runbook '%s' cho incident %s (trong %d ứng viên)",
            best.name,
            incident.id,
            len(matched),
        )
        return MatchResult(
            best,
            f"Khớp runbook '{best.name}'",
            candidates=tuple(rb.name for rb in matched),
        )


    async def _cooldown_remaining(self, incident: Incident, runbook: Runbook) -> int:
        """
        Còn phải chờ bao nhiêu giây nữa mới được chạy lại? 0 = chạy được ngay.

        VÌ SAO CẦN COOLDOWN? Kịch bản tai hoạ thật:
            restart service -> service lỗi lại sau 10s -> alert mới ->
            restart lại -> lỗi lại -> ... vòng lặp vô hạn.
        Nguyên nhân gốc (vd memory leak, config sai) chưa được sửa thì restart
        bao nhiêu lần cũng vô nghĩa, mà mỗi lần restart lại làm gián đoạn
        thêm. Cooldown buộc hệ thống dừng lại và nhường chỗ cho con người.

        Chỉ đếm từ execution GẦN NHẤT của chính incident này. Dùng
        finished_at, và nếu chưa xong thì lấy started_at (nghĩa là đang chạy
        -> chắc chắn phải chờ).
        """
        if self._executions is None or runbook.cooldown_seconds <= 0:
            return 0

        last = await self._executions.last_execution(incident.id)
        if last is None:
            return 0  # chưa từng chạy

        reference = last.finished_at or last.started_at
        if reference is None:
            return 0

        # ensure_utc: SQLite trả datetime naive, Postgres trả aware.
        elapsed = (datetime.now(UTC) - ensure_utc(reference)).total_seconds()
        remaining = runbook.cooldown_seconds - elapsed
        # max(0, ...) để không bao giờ trả số âm; int() cho gọn khi in ra.
        return max(0, int(remaining))


def _incident_context(incident: Incident) -> dict[str, object]:
    """
    Chuyển Incident thành dict phẳng cho rule engine.

    Tách thành hàm riêng (không phải method) vì nó là phép biến đổi thuần -
    không cần state gì, và test được độc lập.
    """
    from app.domain.enums import Severity

    severity = Severity(incident.severity)
    return {
        "source": str(incident.source),
        "service": incident.service,
        "environment": incident.environment,
        "severity": str(severity),
        "severity_weight": severity.weight,
        "title": incident.title,
        "description": incident.description,
        "status": str(incident.status),
        "attempt_count": incident.attempt_count,
        "labels": dict(incident.labels or {}),
    }
