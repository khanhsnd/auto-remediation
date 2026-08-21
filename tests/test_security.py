"""Test bảo mật: băm mật khẩu, JWT, chữ ký webhook, phân quyền."""

from __future__ import annotations

import time

import pytest

from app.core.config import Settings
from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security import (
    Role,
    TokenType,
    create_token,
    decode_token,
    has_permission,
    hash_password,
    require_role,
    sign_payload,
    verify_password,
    verify_signature,
)


class TestPassword:
    def test_hash_khac_nhau_moi_lan(self) -> None:
        """
        Cùng một mật khẩu nhưng salt random -> hash khác nhau.

        Đây là lý do rainbow table không dùng được: attacker không thể tính
        trước hash cho "123456" vì mỗi user có salt riêng.
        """
        h1 = hash_password("matkhau123")
        h2 = hash_password("matkhau123")
        assert h1 != h2

    def test_verify_dung_va_sai(self) -> None:
        h = hash_password("matkhau123")
        assert verify_password("matkhau123", h) is True
        assert verify_password("matkhau124", h) is False

    def test_hash_khong_chua_mat_khau_goc(self) -> None:
        """Kiểm tra hiển nhiên nhưng đáng có: hash là một chiều."""
        assert "matkhau123" not in hash_password("matkhau123")

    def test_hash_hong_thi_tra_false_khong_crash(self) -> None:
        """
        Dữ liệu trong DB có thể hỏng (migration lỗi, ai đó sửa tay).
        Lúc đó phải trả False, KHÔNG được ném exception làm sập API login.
        """
        assert verify_password("x", "du-lieu-rac") is False
        assert verify_password("x", "") is False
        assert verify_password("x", "md5$1$a$b") is False


class TestJWT:
    @pytest.fixture
    def cfg(self) -> Settings:
        return Settings(
            jwt_secret="secret-du-dai-cho-test-toi-thieu-32-byte",
            database_url="sqlite+aiosqlite:///:memory:",
        )

    def test_tao_va_giai_ma_token(self, cfg: Settings) -> None:
        token = create_token(
            cfg, subject="user-1", email="a@b.c", role=Role.OPERATOR
        )
        payload = decode_token(cfg, token)

        assert payload.subject == "user-1"
        assert payload.email == "a@b.c"
        assert payload.role is Role.OPERATOR
        assert payload.token_type is TokenType.ACCESS
        assert payload.jti  # có JWT ID để sau này revoke được

    def test_token_sai_secret_thi_tu_choi(self, cfg: Settings) -> None:
        """
        Đây là bản chất của JWT: signature được tính bằng secret.
        Không có secret thì không thể tạo token hợp lệ.
        """
        token = create_token(cfg, subject="u", email="e", role=Role.VIEWER)
        other = Settings(
            jwt_secret="secret-khac-hoan-toan-va-cung-du-32-byte",
            database_url="sqlite+aiosqlite:///:memory:",
        )
        with pytest.raises(UnauthorizedError, match="không hợp lệ"):
            decode_token(other, token)

    def test_token_bi_sua_thi_tu_choi(self, cfg: Settings) -> None:
        """Sửa một ký tự trong payload là signature sai ngay."""
        token = create_token(cfg, subject="u", email="e", role=Role.VIEWER)
        header, payload, sig = token.split(".")
        tampered = f"{header}.{payload[:-2]}XX.{sig}"

        with pytest.raises(UnauthorizedError):
            decode_token(cfg, tampered)

    def test_token_het_han(self, cfg: Settings) -> None:
        """
        TTL = 0 phút không tạo được token đã hết hạn (validator chặn ge=1),
        nên ta dùng cách khác: tạo token rồi lùi thời gian bằng cách ký lại
        với exp trong quá khứ.
        """
        import jwt as pyjwt

        expired = pyjwt.encode(
            {
                "sub": "u",
                "email": "e",
                "role": "viewer",
                "typ": "access",
                "iat": int(time.time()) - 7200,
                "exp": int(time.time()) - 3600,  # hết hạn 1 tiếng trước
                "iss": cfg.app_name,
            },
            cfg.jwt_secret,
            algorithm=cfg.jwt_algorithm,
        )
        with pytest.raises(UnauthorizedError, match="hết hạn"):
            decode_token(cfg, expired)

    def test_refresh_token_co_ttl_dai_hon(self, cfg: Settings) -> None:
        access = decode_token(
            cfg, create_token(cfg, subject="u", email="e", role=Role.VIEWER,
                              token_type=TokenType.ACCESS)
        )
        refresh = decode_token(
            cfg, create_token(cfg, subject="u", email="e", role=Role.VIEWER,
                              token_type=TokenType.REFRESH)
        )
        assert refresh.expires_at > access.expires_at

    def test_token_payload_bat_bien(self, cfg: Settings) -> None:
        """frozen dataclass: không ai sửa được role sau khi đã giải mã."""
        payload = decode_token(
            cfg, create_token(cfg, subject="u", email="e", role=Role.VIEWER)
        )
        with pytest.raises(AttributeError):
            payload.role = Role.ADMIN  # type: ignore[misc]


class TestRBAC:
    def test_bac_quyen(self) -> None:
        assert has_permission(Role.ADMIN, Role.VIEWER) is True
        assert has_permission(Role.ADMIN, Role.ADMIN) is True
        assert has_permission(Role.OPERATOR, Role.VIEWER) is True
        assert has_permission(Role.VIEWER, Role.OPERATOR) is False

    def test_require_role_chan_khi_thieu_quyen(self) -> None:
        from datetime import UTC, datetime

        from app.core.security import TokenPayload

        payload = TokenPayload(
            subject="u",
            email="e",
            role=Role.VIEWER,
            token_type=TokenType.ACCESS,
            expires_at=datetime.now(UTC),
            jti="x",
        )
        require_role(payload, Role.VIEWER)  # đủ quyền -> không raise

        with pytest.raises(ForbiddenError):
            require_role(payload, Role.ADMIN)


class TestWebhookSignature:
    def test_chu_ky_dung(self) -> None:
        body = b'{"title":"test"}'
        sig = sign_payload("secret", body)
        assert verify_signature("secret", body, sig) is True

    def test_secret_sai_thi_tu_choi(self) -> None:
        body = b'{"title":"test"}'
        sig = sign_payload("secret", body)
        assert verify_signature("secret-khac", body, sig) is False

    def test_body_bi_sua_thi_tu_choi(self) -> None:
        """
        Đây là mục đích chính của HMAC: phát hiện body bị can thiệp trên
        đường truyền.
        """
        sig = sign_payload("secret", b'{"replicas":1}')
        assert verify_signature("secret", b'{"replicas":100}', sig) is False

    def test_thieu_chu_ky_thi_tu_choi(self) -> None:
        assert verify_signature("secret", b"{}", None) is False
        assert verify_signature("secret", b"{}", "") is False
