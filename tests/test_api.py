"""
Test API end-to-end qua HTTP (không cần chạy server).

Test ở tầng này đắt hơn unit test nhưng bắt được những lỗi mà unit test
không thấy: sai status code, sai schema trả về, quên phân quyền, thứ tự
route bị chồng chéo.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Role, hash_password
from app.db.models.user import User


class TestHealth:
    async def test_liveness_khong_can_dang_nhap(self, client: Any) -> None:
        """
        Probe của K8s không có token -> health phải công khai.

        Quên điều này thì K8s luôn nhận 401 và restart pod vô hạn.
        """
        response = await client.get("/health/live")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_trang_goc_tra_thong_tin_co_ban(self, client: Any) -> None:
        response = await client.get("/")
        assert response.status_code == 200
        assert response.json()["docs"] == "/docs"


class TestWebhook:
    async def test_nhan_alert_tra_202(self, client: Any) -> None:
        """
        202 Accepted, KHÔNG phải 200 OK.

        Ngữ nghĩa HTTP: "đã nhận, sẽ xử lý sau" - đúng với xử lý bất đồng bộ.
        """
        response = await client.post(
            "/api/v1/webhooks/custom/simple",
            json={
                "title": "High memory usage",
                "service": "payment-api",
                "severity": "critical",
            },
        )
        assert response.status_code == 202

        body = response.json()
        assert body["created"] is True
        assert body["deduplicated"] is False
        assert body["incident_id"]

    async def test_alert_trung_van_tra_202(self, client: Any) -> None:
        """
        Trùng lặp KHÔNG phải lỗi -> vẫn 202.

        Nếu trả 4xx, hệ thống monitoring sẽ coi là thất bại và gửi lại mãi.
        """
        payload = {"title": "Disk full", "service": "db-primary", "severity": "high"}

        first = await client.post("/api/v1/webhooks/custom/simple", json=payload)
        second = await client.post("/api/v1/webhooks/custom/simple", json=payload)

        assert first.status_code == 202
        assert second.status_code == 202
        assert second.json()["deduplicated"] is True
        assert second.json()["incident_id"] == first.json()["incident_id"]

    async def test_payload_prometheus(self, client: Any) -> None:
        """Endpoint /{source} nhận đúng format gốc của từng hãng."""
        response = await client.post(
            "/api/v1/webhooks/prometheus",
            json={
                "alerts": [
                    {
                        "labels": {
                            "alertname": "HighMemoryUsage",
                            "severity": "critical",
                            "service": "order-api",
                        },
                        "annotations": {"description": "Mem > 90%"},
                        "startsAt": "2026-01-15T10:30:00Z",
                    }
                ]
            },
        )
        assert response.status_code == 202
        assert response.json()["created"] is True

    async def test_source_khong_ho_tro_thi_422(self, client: Any) -> None:
        """
        Path param có kiểu Enum -> FastAPI tự validate, ta không viết code nào.
        """
        response = await client.post("/api/v1/webhooks/khong-ton-tai", json={"title": "x"})
        assert response.status_code == 422

    async def test_thieu_field_bat_buoc_thi_422(self, client: Any) -> None:
        response = await client.post("/api/v1/webhooks/custom/simple", json={"title": "x"})
        assert response.status_code == 422

        body = response.json()
        # Định dạng lỗi đã được chuẩn hoá ở app/api/errors.py
        assert body["error"] == "validation_error"
        assert "body.service" in body["details"]

    async def test_payload_prometheus_sai_thi_422(self, client: Any) -> None:
        response = await client.post("/api/v1/webhooks/prometheus", json={"sai": "format"})
        assert response.status_code == 422
        assert response.json()["error"] == "validation_error"


class TestAuth:
    async def test_dang_nhap_thanh_cong(self, client: Any, db: AsyncSession) -> None:
        db.add(
            User(
                email="a@test.dev",
                hashed_password=hash_password("matkhau12345"),
                role=Role.OPERATOR,
            )
        )
        await db.commit()

        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "a@test.dev", "password": "matkhau12345"},
        )
        assert response.status_code == 200

        body = response.json()
        assert body["access_token"]
        assert body["refresh_token"]
        assert body["token_type"] == "bearer"
        assert body["expires_in"] > 0

    @pytest.mark.parametrize(
        ("email", "password"),
        [
            ("a@test.dev", "sai-mat-khau-roi"),   # đúng email, sai mật khẩu
            ("khong-co@test.dev", "matkhau12345"),  # email không tồn tại
        ],
    )
    async def test_thong_bao_loi_giong_nhau(
        self, client: Any, db: AsyncSession, email: str, password: str
    ) -> None:
        """
        BẢO MẬT: hai trường hợp phải có thông báo Y HỆT nhau.

        Nếu phân biệt "email không tồn tại" vs "mật khẩu sai", attacker dò
        được danh sách email hợp lệ (user enumeration).
        """
        db.add(
            User(
                email="a@test.dev",
                hashed_password=hash_password("matkhau12345"),
                role=Role.VIEWER,
            )
        )
        await db.commit()

        response = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
        assert response.status_code == 401
        assert response.json()["message"] == "Email hoặc mật khẩu không đúng"

    async def test_tai_khoan_bi_vo_hieu_hoa(self, client: Any, db: AsyncSession) -> None:
        db.add(
            User(
                email="off@test.dev",
                hashed_password=hash_password("matkhau12345"),
                role=Role.ADMIN,
                is_active=False,
            )
        )
        await db.commit()

        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "off@test.dev", "password": "matkhau12345"},
        )
        assert response.status_code == 401

    async def test_email_sai_dinh_dang_thi_422(self, client: Any) -> None:
        """EmailStr của Pydantic validate hộ, không phải tự viết regex."""
        response = await client.post(
            "/api/v1/auth/login", json={"email": "khong-phai-email", "password": "matkhau12345"}
        )
        assert response.status_code == 422

    async def test_me_tra_thong_tin_va_khong_lo_hash(
        self, client: Any, db: AsyncSession, auth_header: Any
    ) -> None:
        """
        response_model=UserOut đảm bảo hashed_password KHÔNG lọt ra ngoài,
        dù endpoint trả về object User đầy đủ.
        """
        user = User(
            email="me@test.dev",
            hashed_password=hash_password("matkhau12345"),
            role=Role.ADMIN,
        )
        db.add(user)
        await db.commit()

        response = await client.get(
            "/api/v1/auth/me", headers=auth_header(user.id, Role.ADMIN, "me@test.dev")
        )
        assert response.status_code == 200

        body = response.json()
        assert body["email"] == "me@test.dev"
        assert "hashed_password" not in body
        assert "password" not in body


class TestAuthorization:
    """Phân quyền - phần dễ quên nhất và hậu quả nặng nhất."""

    async def test_khong_co_token_thi_401(self, client: Any) -> None:
        response = await client.get("/api/v1/incidents")
        assert response.status_code == 401
        assert response.json()["error"] == "unauthorized"

    async def test_header_sai_dinh_dang_thi_401(self, client: Any) -> None:
        response = await client.get(
            "/api/v1/incidents", headers={"Authorization": "khong-phai-bearer"}
        )
        assert response.status_code == 401
        assert "Bearer" in response.json()["message"]

    async def test_token_rac_thi_401(self, client: Any) -> None:
        response = await client.get(
            "/api/v1/incidents", headers={"Authorization": "Bearer khong-phai-jwt"}
        )
        assert response.status_code == 401

    async def test_user_khong_ton_tai_trong_db_thi_401(
        self, client: Any, auth_header: Any
    ) -> None:
        """
        Token hợp lệ về mặt chữ ký nhưng user đã bị xoá -> vẫn phải chặn.

        Đây là lý do dependency get_current_user còn tra DB dù token đã có
        đủ thông tin: token sống 30 phút, trong đó user có thể bị khoá.
        """
        response = await client.get(
            "/api/v1/incidents", headers=auth_header("id-khong-ton-tai", Role.ADMIN)
        )
        assert response.status_code == 401

    async def test_viewer_khong_tao_duoc_runbook(
        self, client: Any, db: AsyncSession, auth_header: Any
    ) -> None:
        """VIEWER chỉ được xem. Tạo runbook = điều khiển hạ tầng -> phải ADMIN."""
        user = User(
            email="viewer@test.dev",
            hashed_password=hash_password("matkhau12345"),
            role=Role.VIEWER,
        )
        db.add(user)
        await db.commit()

        response = await client.post(
            "/api/v1/runbooks",
            headers=auth_header(user.id, Role.VIEWER),
            json={
                "name": "thu-tao",
                "steps": [{"name": "s", "action_type": "noop", "params": {}}],
            },
        )
        assert response.status_code == 403
        assert response.json()["error"] == "forbidden"

    async def test_admin_tao_duoc_runbook(
        self, client: Any, db: AsyncSession, auth_header: Any
    ) -> None:
        user = User(
            email="admin2@test.dev",
            hashed_password=hash_password("matkhau12345"),
            role=Role.ADMIN,
        )
        db.add(user)
        await db.commit()

        response = await client.post(
            "/api/v1/runbooks",
            headers=auth_header(user.id, Role.ADMIN),
            json={
                "name": "restart-service",
                "description": "Runbook thử",
                "priority": 10,
                "match_rules": {
                    "all": [{"field": "service", "op": "eq", "value": "payment-api"}]
                },
                "steps": [
                    {"name": "noop", "action_type": "noop", "params": {}},
                    {"name": "cho", "action_type": "wait", "params": {"seconds": 1}},
                ],
            },
        )
        assert response.status_code == 201

        body = response.json()
        assert body["name"] == "restart-service"
        # order_index được sinh tự động theo thứ tự trong mảng.
        assert [s["order_index"] for s in body["steps"]] == [0, 1]


async def make_user(db: AsyncSession, email: str, role: Role) -> User:
    """Helper gom việc tạo user để các test bên dưới đọc gọn hơn."""
    user = User(email=email, hashed_password=hash_password("matkhau12345"), role=role)
    db.add(user)
    await db.commit()
    return user


class TestRunbookValidation:
    async def test_thieu_url_cho_http_call_thi_422(
        self, client: Any, db: AsyncSession, auth_header: Any
    ) -> None:
        """
        model_validator kiểm tra params theo action_type.

        Chặn ở đây quan trọng: runbook thiếu url mà vẫn lưu được thì lúc
        production cần cứu, nó sẽ nổ ValueError giữa đường.
        """
        user = await make_user(db, "a4@test.dev", Role.ADMIN)
        response = await client.post(
            "/api/v1/runbooks",
            headers=auth_header(user.id, Role.ADMIN),
            json={
                "name": "thieu-url",
                "steps": [{"name": "goi-api", "action_type": "http_call", "params": {}}],
            },
        )
        assert response.status_code == 422

    async def test_luat_co_toan_tu_sai_thi_422(
        self, client: Any, db: AsyncSession, auth_header: Any
    ) -> None:
        """
        field_validator chạy THẬT rule engine với dữ liệu mẫu -> op sai
        chính tả bị bắt ngay lúc tạo, không phải lúc đang có sự cố.
        """
        user = await make_user(db, "a5@test.dev", Role.ADMIN)
        response = await client.post(
            "/api/v1/runbooks",
            headers=auth_header(user.id, Role.ADMIN),
            json={
                "name": "luat-sai",
                "match_rules": {"all": [{"field": "service", "op": "equalss", "value": "x"}]},
                "steps": [{"name": "s", "action_type": "noop", "params": {}}],
            },
        )
        assert response.status_code == 422

    async def test_runbook_khong_co_step_thi_422(
        self, client: Any, db: AsyncSession, auth_header: Any
    ) -> None:
        user = await make_user(db, "a6@test.dev", Role.ADMIN)
        response = await client.post(
            "/api/v1/runbooks",
            headers=auth_header(user.id, Role.ADMIN),
            json={"name": "khong-step", "steps": []},
        )
        assert response.status_code == 422

    async def test_ten_runbook_trung_thi_409(
        self, client: Any, db: AsyncSession, auth_header: Any
    ) -> None:
        user = await make_user(db, "a7@test.dev", Role.ADMIN)
        payload: dict[str, Any] = {
            "name": "trung-ten",
            "steps": [{"name": "s", "action_type": "noop", "params": {}}],
        }
        headers = auth_header(user.id, Role.ADMIN)

        first = await client.post("/api/v1/runbooks", headers=headers, json=payload)
        second = await client.post("/api/v1/runbooks", headers=headers, json=payload)

        assert first.status_code == 201
        assert second.status_code == 409
        assert second.json()["error"] == "conflict"

    async def test_test_match_khong_chay_gi(
        self, client: Any, db: AsyncSession, auth_header: Any
    ) -> None:
        """
        Endpoint /test-match cho phép thử luật trước khi bật runbook thật.

        Luôn nên có "dry-run" cho mọi thứ nguy hiểm.
        """
        user = await make_user(db, "a8@test.dev", Role.ADMIN)
        headers = auth_header(user.id, Role.ADMIN)

        await client.post(
            "/api/v1/runbooks",
            headers=headers,
            json={
                "name": "chi-payment",
                "match_rules": {
                    "all": [{"field": "service", "op": "eq", "value": "payment-api"}]
                },
                "steps": [{"name": "s", "action_type": "noop", "params": {}}],
            },
        )

        khop = await client.post(
            "/api/v1/runbooks/test-match",
            headers=headers,
            json={"service": "payment-api", "severity": "critical"},
        )
        assert khop.json()["matched"] is True
        assert khop.json()["runbook_name"] == "chi-payment"

        khong_khop = await client.post(
            "/api/v1/runbooks/test-match",
            headers=headers,
            json={"service": "service-khac", "severity": "critical"},
        )
        assert khong_khop.json()["matched"] is False


class TestIncidentEndpoints:
    async def test_liet_ke_va_phan_trang(
        self, client: Any, db: AsyncSession, auth_header: Any
    ) -> None:
        user = await make_user(db, "v1@test.dev", Role.VIEWER)
        headers = auth_header(user.id, Role.VIEWER)

        for i in range(3):
            await client.post(
                "/api/v1/webhooks/custom/simple",
                json={"title": f"Su co {i}", "service": f"svc-{i}", "severity": "high"},
            )

        response = await client.get("/api/v1/incidents?limit=2", headers=headers)
        assert response.status_code == 200

        body = response.json()
        assert len(body["items"]) == 2
        assert body["total"] == 3
        assert body["limit"] == 2

    async def test_loc_theo_service(
        self, client: Any, db: AsyncSession, auth_header: Any
    ) -> None:
        user = await make_user(db, "v2@test.dev", Role.VIEWER)
        headers = auth_header(user.id, Role.VIEWER)

        await client.post(
            "/api/v1/webhooks/custom/simple",
            json={"title": "a", "service": "payment-api", "severity": "high"},
        )
        await client.post(
            "/api/v1/webhooks/custom/simple",
            json={"title": "b", "service": "order-api", "severity": "high"},
        )

        response = await client.get("/api/v1/incidents?service=payment-api", headers=headers)
        assert response.json()["total"] == 1

    async def test_limit_vuot_tran_thi_422(
        self, client: Any, db: AsyncSession, auth_header: Any
    ) -> None:
        """Trần le=200 chặn ?limit=999999 - nếu không, client kéo hết DB về RAM."""
        user = await make_user(db, "v3@test.dev", Role.VIEWER)
        response = await client.get(
            "/api/v1/incidents?limit=999999", headers=auth_header(user.id, Role.VIEWER)
        )
        assert response.status_code == 422

    async def test_incident_khong_ton_tai_thi_404(
        self, client: Any, db: AsyncSession, auth_header: Any
    ) -> None:
        user = await make_user(db, "v4@test.dev", Role.VIEWER)
        response = await client.get(
            "/api/v1/incidents/khong-ton-tai", headers=auth_header(user.id, Role.VIEWER)
        )
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"

    async def test_route_stats_khong_bi_hieu_thanh_id(
        self, client: Any, db: AsyncSession, auth_header: Any
    ) -> None:
        """
        BẪY FASTAPI: nếu /incidents/{id} khai báo TRƯỚC /incidents/stats thì
        "stats" sẽ bị hiểu là incident_id -> luôn trả 404.

        Test này chốt đúng thứ tự khai báo route.
        """
        user = await make_user(db, "v5@test.dev", Role.VIEWER)
        response = await client.get(
            "/api/v1/incidents/stats", headers=auth_header(user.id, Role.VIEWER)
        )
        assert response.status_code == 200
        assert "by_status" in response.json()

    async def test_viewer_khong_kich_hoat_duoc_remediation(
        self, client: Any, db: AsyncSession, auth_header: Any
    ) -> None:
        """Chạy runbook = tác động hạ tầng -> tối thiểu OPERATOR."""
        user = await make_user(db, "v6@test.dev", Role.VIEWER)
        ingest = await client.post(
            "/api/v1/webhooks/custom/simple",
            json={"title": "x", "service": "svc", "severity": "high"},
        )
        incident_id = ingest.json()["incident_id"]

        response = await client.post(
            f"/api/v1/incidents/{incident_id}/remediate",
            headers=auth_header(user.id, Role.VIEWER),
            json={},
        )
        assert response.status_code == 403


class TestRequestTracing:
    async def test_tra_ve_request_id(self, client: Any) -> None:
        """Client báo lỗi kèm request id -> ta tra log ra ngay."""
        response = await client.get("/health/live")
        assert response.headers.get("X-Request-ID")

    async def test_dung_lai_request_id_cua_client(self, client: Any) -> None:
        """Nếu API gateway đã sinh id thì DÙNG LẠI, để trace xuyên nhiều service."""
        response = await client.get("/health/live", headers={"X-Request-ID": "trace-abc-123"})
        assert response.headers["X-Request-ID"] == "trace-abc-123"

    async def test_co_header_thoi_gian_xu_ly(self, client: Any) -> None:
        """X-Response-Time-Ms giúp phát hiện endpoint chậm ngay từ phía client."""
        response = await client.get("/api/v1/incidents")
        assert "X-Response-Time-Ms" in response.headers
