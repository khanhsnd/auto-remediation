"""
Rule engine - trái tim của bước "check runbook".

BÀI TOÁN: làm sao biết alert này thì chạy runbook nào? Nếu viết if-else
trong code thì mỗi lần thêm luật lại phải sửa code + deploy lại.

GIẢI PHÁP: luật được LƯU DƯỚI DẠNG DỮ LIỆU (JSONB trong Postgres), engine
chỉ việc đọc và thông dịch. Thêm/sửa luật = update 1 row, không cần deploy.

CẤU TRÚC LUẬT (DSL nhỏ tự định nghĩa):
    {
      "all":  [ {"field": "service",         "op": "eq",    "value": "payment-api"},
                {"field": "severity",        "op": "in",    "value": ["critical","high"]} ],
      "any":  [ {"field": "labels.alertname","op": "regex", "value": "^HighMemory"} ],
      "none": [ {"field": "labels.maintenance","op":"exists"} ]
    }
    all  -> mọi điều kiện phải đúng (AND)
    any  -> ít nhất một điều kiện đúng (OR)
    none -> không điều kiện nào được đúng (NOT)

KIẾN THỨC PYTHON
  1. match/case với dict pattern để thông dịch DSL
  2. re.compile + lru_cache -> biên dịch regex một lần rồi cache lại
  3. all() / any() với generator expression -> ngắn gọn và "lazy"
  4. Đường dẫn có dấu chấm ("labels.alertname") -> tự viết resolver
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from app.core.exceptions import ValidationError

# Số lượng regex khác nhau tối đa được cache. Bảo vệ RAM nếu ai đó
# sinh regex động vô hạn.
_REGEX_CACHE_SIZE = 256


@lru_cache(maxsize=_REGEX_CACHE_SIZE)
def _compiled(pattern: str) -> re.Pattern[str]:
    """
    Biên dịch regex và cache lại.

    re.compile khá tốn CPU. Rule engine chạy cho MỌI alert nhân MỌI runbook,
    nên cache ở đây tiết kiệm rất nhiều. lru_cache lo hết phần cache.
    """
    try:
        return re.compile(pattern)
    except re.error as err:
        raise ValidationError(f"Regex không hợp lệ: {pattern}") from err


def resolve_field(context: dict[str, Any], path: str) -> Any:
    """
    Lấy giá trị theo đường dẫn có dấu chấm.

    resolve_field(ctx, "labels.pod")  ->  ctx["labels"]["pod"]
    Không tìm thấy thì trả None (chứ không raise) - luật sai key vẫn chạy được.

    Vòng for + reassign `current` là cách duyệt cây đơn giản và dễ đọc nhất.
    """
    current: Any = context
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None  # đi vào ngõ cụt: giá trị không phải dict mà còn key
    return current


def evaluate_condition(context: dict[str, Any], condition: dict[str, Any]) -> bool:
    """
    Chạy MỘT điều kiện.

    match/case ở đây thông dịch DSL: mỗi case là một toán tử.
    Lưu ý case cuối cùng raise lỗi - "fail loud" tốt hơn âm thầm trả False,
    vì luật sai chính tả mà im lặng thì rất khó phát hiện.
    """
    field = condition.get("field")
    if not isinstance(field, str):
        raise ValidationError("Điều kiện thiếu 'field'", details={"condition": condition})

    actual = resolve_field(context, field)
    expected = condition.get("value")
    op = str(condition.get("op", "eq")).lower()

    match op:
        case "eq":
            # bool(...) tường minh: _norm trả về Any nên phép == cũng là Any,
            # mypy strict sẽ bắt lỗi nếu ta trả về thẳng.
            return bool(_norm(actual) == _norm(expected))
        case "ne":
            return bool(_norm(actual) != _norm(expected))
        case "in":
            # Đề phòng value không phải list.
            values = expected if isinstance(expected, list) else [expected]
            return _norm(actual) in {_norm(v) for v in values}
        case "not_in":
            values = expected if isinstance(expected, list) else [expected]
            return _norm(actual) not in {_norm(v) for v in values}
        case "contains":
            return actual is not None and str(expected) in str(actual)
        case "regex":
            return actual is not None and _compiled(str(expected)).search(str(actual)) is not None
        case "gt" | "gte" | "lt" | "lte":
            return _compare_numeric(op, actual, expected)
        case "exists":
            return actual is not None
        case "not_exists":
            return actual is None
        case _:
            raise ValidationError(
                f"Toán tử không hỗ trợ: {op}",
                details={"supported": sorted(SUPPORTED_OPS)},
            )


SUPPORTED_OPS = frozenset(
    {
        "eq", "ne", "in", "not_in", "contains", "regex",
        "gt", "gte", "lt", "lte", "exists", "not_exists",
    }
)


def _norm(value: Any) -> Any:
    """
    Chuẩn hoá trước khi so sánh: chuỗi thì lowercase + trim.

    Lý do: label từ bên thứ 3 hay có "Payment-API" vs "payment-api".
    So sánh phân biệt hoa thường ở đây gây bug rất khó tìm.
    """
    if isinstance(value, str):
        return value.strip().lower()
    return value


def _compare_numeric(op: str, actual: Any, expected: Any) -> bool:
    """
    So sánh số, nhưng dữ liệu từ JSON có thể là chuỗi "91.2".

    try/except ở đây là Pythonic ("xin tha thứ dễ hơn xin phép" - EAFP),
    thay vì kiểm tra trước cả chục kiểu.
    """
    try:
        a, b = float(actual), float(expected)
    except (TypeError, ValueError):
        return False

    match op:
        case "gt":
            return a > b
        case "gte":
            return a >= b
        case "lt":
            return a < b
        case _:  # "lte"
            return a <= b


def matches(context: dict[str, Any], rules: dict[str, Any] | None) -> bool:
    """
    Chạy toàn bộ khối luật.

    Quy ước quan trọng: rules rỗng/None -> KHỚP MỌI THỨ. Nhờ vậy tạo được
    runbook "catch-all" làm lưới an toàn cuối cùng.

    all()/any() dùng generator expression nên có "short-circuit": gặp
    điều kiện sai đầu tiên là dừng luôn, không tính nốt phần còn lại.
    """
    if not rules:
        return True

    all_conds: list[dict[str, Any]] = rules.get("all") or []
    any_conds: list[dict[str, Any]] = rules.get("any") or []
    none_conds: list[dict[str, Any]] = rules.get("none") or []

    if not all(evaluate_condition(context, c) for c in all_conds):
        return False
    if any_conds and not any(evaluate_condition(context, c) for c in any_conds):
        return False
    # not any(...) đọc gọn hơn if/return False/return True.
    return not any(evaluate_condition(context, c) for c in none_conds)


def specificity(rules: dict[str, Any] | None) -> int:
    """
    Đếm số điều kiện -> dùng để xếp hạng khi nhiều runbook cùng khớp.

    Nguyên tắc: luật càng cụ thể càng nên được ưu tiên (giống CSS specificity).
    Runbook catch-all (0 điều kiện) sẽ luôn xếp cuối.
    """
    if not rules:
        return 0
    return sum(len(rules.get(key) or []) for key in ("all", "any", "none"))
