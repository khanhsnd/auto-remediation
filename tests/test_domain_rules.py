"""
Test rule engine - đây là tầng dễ test nhất và cũng quan trọng nhất.

Vì sao quan trọng? Vì luật sai dẫn tới hệ thống chạy runbook sai trên
production. Test kỹ ở đây là rẻ nhất và hiệu quả nhất.

KIẾN THỨC PYTEST
  1. @pytest.mark.parametrize - một hàm test chạy với nhiều bộ dữ liệu
  2. pytest.raises            - kiểm tra code có ném đúng loại lỗi
  3. Đặt tên test theo dạng test_<hành_vi_mong_đợi>
"""

from __future__ import annotations

import pytest

from app.core.exceptions import ValidationError
from app.domain import rules

# Ngữ cảnh mẫu, dùng lại cho nhiều test.
CONTEXT = {
    "service": "payment-api",
    "environment": "production",
    "severity": "critical",
    "severity_weight": 4,
    "title": "High memory usage on pod",
    "labels": {"alertname": "HighMemoryUsage", "pod": "payment-api-7d9f", "team": "sre"},
}


class TestResolveField:
    """
    Nhóm test theo class cho gọn.

    Class chỉ để gom nhóm, KHÔNG cần kế thừa gì (khác unittest.TestCase).
    """

    def test_lay_duoc_field_phang(self) -> None:
        assert rules.resolve_field(CONTEXT, "service") == "payment-api"

    def test_lay_duoc_field_long_nhau(self) -> None:
        """Đường dẫn có dấu chấm đi xuyên vào dict con."""
        assert rules.resolve_field(CONTEXT, "labels.alertname") == "HighMemoryUsage"

    def test_field_khong_ton_tai_tra_none(self) -> None:
        """Trả None thay vì raise -> luật viết sai key vẫn chạy được."""
        assert rules.resolve_field(CONTEXT, "khong_co") is None
        assert rules.resolve_field(CONTEXT, "labels.khong_co") is None

    def test_di_vao_ngo_cut_tra_none(self) -> None:
        """service là chuỗi, không thể đi tiếp .abc."""
        assert rules.resolve_field(CONTEXT, "service.abc") is None


class TestOperators:
    # parametrize: mỗi tuple là một test case độc lập. Pytest báo cáo riêng
    # từng cái, nên fail cái nào là biết ngay cái đó.
    @pytest.mark.parametrize(
        ("op", "field", "value", "expected"),
        [
            ("eq", "service", "payment-api", True),
            ("eq", "service", "PAYMENT-API", True),      # chuẩn hoá hoa/thường
            ("eq", "service", "other-api", False),
            ("ne", "service", "other-api", True),
            ("in", "severity", ["critical", "high"], True),
            ("in", "severity", ["low"], False),
            ("not_in", "severity", ["low"], True),
            ("contains", "title", "memory", True),
            ("contains", "title", "disk", False),
            ("regex", "labels.alertname", "^High", True),
            ("regex", "labels.alertname", "^Low", False),
            ("gte", "severity_weight", 4, True),
            ("gt", "severity_weight", 4, False),
            ("lte", "severity_weight", 4, True),
            ("lt", "severity_weight", 5, True),
            ("exists", "labels.pod", None, True),
            ("exists", "labels.khong_co", None, False),
            ("not_exists", "labels.khong_co", None, True),
        ],
    )
    def test_cac_toan_tu(self, op: str, field: str, value: object, expected: bool) -> None:
        condition = {"field": field, "op": op, "value": value}
        assert rules.evaluate_condition(CONTEXT, condition) is expected

    def test_so_sanh_so_dang_chuoi(self) -> None:
        """
        Dữ liệu từ JSON hay là chuỗi "4" thay vì số 4.

        Rule engine phải tự ép kiểu, nếu không luật ngưỡng sẽ luôn sai.
        """
        ctx = {"value": "91.5"}
        assert rules.evaluate_condition(ctx, {"field": "value", "op": "gt", "value": 90}) is True

    def test_toan_tu_khong_ho_tro_thi_raise(self) -> None:
        """
        "Fail loud": op sai chính tả phải báo lỗi, không im lặng trả False.

        pytest.raises kiểm tra đúng loại exception; `match` kiểm tra message.
        """
        with pytest.raises(ValidationError, match="Toán tử không hỗ trợ"):
            rules.evaluate_condition(CONTEXT, {"field": "service", "op": "khong_co_op"})

    def test_thieu_field_thi_raise(self) -> None:
        with pytest.raises(ValidationError, match="thiếu 'field'"):
            rules.evaluate_condition(CONTEXT, {"op": "eq", "value": "x"})

    def test_regex_sai_cu_phap_thi_raise(self) -> None:
        with pytest.raises(ValidationError, match="Regex không hợp lệ"):
            rules.evaluate_condition(CONTEXT, {"field": "title", "op": "regex", "value": "["})


class TestMatches:
    def test_rules_rong_khop_moi_thu(self) -> None:
        """
        Quy ước quan trọng: rules rỗng = catch-all.

        Nhờ đó tạo được runbook lưới an toàn cuối cùng.
        """
        assert rules.matches(CONTEXT, {}) is True
        assert rules.matches(CONTEXT, None) is True

    def test_all_phai_dung_het(self) -> None:
        ok = {
            "all": [
                {"field": "service", "op": "eq", "value": "payment-api"},
                {"field": "environment", "op": "eq", "value": "production"},
            ]
        }
        assert rules.matches(CONTEXT, ok) is True

        # Chỉ cần một điều kiện sai là cả khối "all" sai.
        fail = {
            "all": [
                {"field": "service", "op": "eq", "value": "payment-api"},
                {"field": "environment", "op": "eq", "value": "staging"},
            ]
        }
        assert rules.matches(CONTEXT, fail) is False

    def test_any_chi_can_mot_dung(self) -> None:
        r = {
            "any": [
                {"field": "service", "op": "eq", "value": "khong-phai"},
                {"field": "severity", "op": "eq", "value": "critical"},
            ]
        }
        assert rules.matches(CONTEXT, r) is True

    def test_none_khong_duoc_cai_nao_dung(self) -> None:
        """none dùng để loại trừ, vd bỏ qua khi đang bảo trì."""
        blocked = {"none": [{"field": "labels.alertname", "op": "exists"}]}
        assert rules.matches(CONTEXT, blocked) is False

        passed = {"none": [{"field": "labels.maintenance", "op": "exists"}]}
        assert rules.matches(CONTEXT, passed) is True

    def test_ket_hop_all_any_none(self) -> None:
        r = {
            "all": [{"field": "environment", "op": "eq", "value": "production"}],
            "any": [
                {"field": "labels.alertname", "op": "regex", "value": "(?i)memory"},
                {"field": "labels.alertname", "op": "regex", "value": "(?i)cpu"},
            ],
            "none": [{"field": "labels.maintenance", "op": "exists"}],
        }
        assert rules.matches(CONTEXT, r) is True


def test_specificity_dem_dung_so_dieu_kien() -> None:
    """
    specificity quyết định runbook nào thắng khi nhiều cái cùng khớp.

    Luật cụ thể hơn (nhiều điều kiện hơn) phải được ưu tiên.
    """
    assert rules.specificity(None) == 0
    assert rules.specificity({}) == 0
    assert rules.specificity({"all": [{"field": "a", "op": "eq"}]}) == 1
    assert (
        rules.specificity(
            {
                "all": [{"field": "a", "op": "eq"}, {"field": "b", "op": "eq"}],
                "none": [{"field": "c", "op": "exists"}],
            }
        )
        == 3
    )
