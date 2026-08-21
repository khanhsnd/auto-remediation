"""
Bảo mật: băm mật khẩu, phát/kiểm JWT, xác thực chữ ký webhook.

KIẾN THỨC PYTHON / BẢO MẬT
  1. hashlib.pbkdf2_hmac      -> băm mật khẩu có salt + nhiều vòng lặp
  2. hmac.compare_digest      -> so sánh chống "timing attack"
  3. pyjwt encode/decode      -> token tự chứa thông tin, không cần tra DB
  4. dataclass(frozen=True)   -> object bất biến, an toàn khi truyền đi
  5. secrets                  -> sinh số ngẫu nhiên an toàn mật mã
                                 (khác random - random KHÔNG dùng cho bảo mật)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Self

import jwt

from app.core.config import Settings
from app.core.exceptions import ForbiddenError, UnauthorizedError

# Tham số PBKDF2. Càng nhiều vòng càng chậm -> attacker brute-force càng khổ.
_PBKDF2_ROUNDS = 260_000  # dấu _ trong số chỉ để dễ đọc, Python bỏ qua
_SALT_BYTES = 16
_HASH_NAME = "sha256"


class Role(StrEnum):
    """
    Phân quyền theo vai trò (RBAC).

    Thứ tự khai báo có ý nghĩa: dùng cho hàm has_permission bên dưới.
    """

    VIEWER = "viewer"      # chỉ xem incident
    OPERATOR = "operator"  # được chạy runbook thủ công
    ADMIN = "admin"        # được sửa runbook, quản lý user


# Bậc quyền: số lớn hơn = quyền cao hơn. Dict tra cứu O(1), rõ hơn if-else dài.
_ROLE_LEVEL: dict[Role, int] = {Role.VIEWER: 10, Role.OPERATOR: 20, Role.ADMIN: 30}


def has_permission(actual: Role, required: Role) -> bool:
    """ADMIN làm được việc của OPERATOR, OPERATOR làm được việc của VIEWER."""
    return _ROLE_LEVEL[actual] >= _ROLE_LEVEL[required]


# =============================================================================
# 1. Mật khẩu
# =============================================================================
def hash_password(plain: str) -> str:
    """
    Băm mật khẩu, kết quả có dạng: pbkdf2_sha256 $ rounds $ salt_b64 $ hash_b64

    Tại sao không lưu mật khẩu thô, và cũng không dùng md5/sha1 trần?
      - Băm là một chiều: DB bị lộ thì attacker vẫn không có mật khẩu.
      - Salt (random cho từng user) chống bảng tra ngược (rainbow table):
        2 người cùng mật khẩu "123456" vẫn ra 2 hash khác nhau.
      - Nhiều vòng lặp làm việc brute-force trở nên cực đắt đỏ.

    [Unverified] Ở production thường dùng argon2 hoặc bcrypt (passlib /
    argon2-cffi). Đây dùng pbkdf2 của stdlib để không thêm dependency và để
    anh thấy rõ từng thành phần của một hash.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(_HASH_NAME, plain.encode(), salt, _PBKDF2_ROUNDS)
    parts = [
        "pbkdf2_sha256",
        str(_PBKDF2_ROUNDS),
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    ]
    return "$".join(parts)


def verify_password(plain: str, stored: str) -> bool:
    """
    Kiểm tra mật khẩu.

    hmac.compare_digest so sánh trong thời gian HẰNG SỐ. Nếu dùng ==,
    Python dừng ngay khi gặp byte khác nhau -> thời gian phản hồi tiết lộ
    "đúng được mấy ký tự đầu" -> attacker dò dần được (timing attack).
    """
    try:
        algo, rounds_s, salt_b64, hash_b64 = stored.split("$")
    except ValueError:
        return False  # dữ liệu trong DB hỏng / không đúng định dạng

    if algo != "pbkdf2_sha256":
        return False

    expected = base64.b64decode(hash_b64)
    actual = hashlib.pbkdf2_hmac(
        _HASH_NAME, plain.encode(), base64.b64decode(salt_b64), int(rounds_s)
    )
    return hmac.compare_digest(expected, actual)


# =============================================================================
# 2. JWT
# =============================================================================
class TokenType(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


@dataclass(frozen=True, slots=True)
class TokenPayload:
    """
    Nội dung đã giải mã của token.

    frozen=True -> immutable: gán lại thuộc tính sẽ raise. Rất phù hợp cho
                   dữ liệu bảo mật, tránh chỗ nào đó sửa quyền giữa đường.
    slots=True   -> không tạo __dict__, tiết kiệm RAM + truy cập nhanh hơn.
    """

    subject: str  # thường là user id
    email: str
    role: Role
    token_type: TokenType
    expires_at: datetime
    jti: str  # JWT ID - định danh token, dùng để revoke

    @classmethod
    def from_claims(cls, claims: dict[str, Any]) -> Self:
        """
        classmethod làm constructor thay thế (alternative constructor).

        Self (Python 3.11+) nghĩa là "trả về chính class này" - subclass kế
        thừa sẽ tự có kiểu đúng, không phải viết lại type hint.
        """
        return cls(
            subject=claims["sub"],
            email=claims.get("email", ""),
            role=Role(claims.get("role", Role.VIEWER)),
            token_type=TokenType(claims.get("typ", TokenType.ACCESS)),
            expires_at=datetime.fromtimestamp(claims["exp"], tz=UTC),
            jti=claims.get("jti", ""),
        )


def create_token(
    settings: Settings,
    *,
    subject: str,
    email: str,
    role: Role,
    token_type: TokenType = TokenType.ACCESS,
) -> str:
    """
    Tạo JWT.

    JWT gồm 3 phần ngăn bởi dấu chấm: header.payload.signature
      - header/payload chỉ là base64 -> AI CŨNG ĐỌC ĐƯỢC.
        => Không bao giờ nhét mật khẩu hay dữ liệu mật vào payload.
      - signature tính bằng secret -> sửa payload là signature sai ngay.

    Claim chuẩn (RFC 7519): sub (subject), exp (expire), iat (issued at),
    nbf (not before), iss (issuer), jti (token id).
    """
    now = datetime.now(UTC)  # LUÔN dùng UTC, tránh lệch múi giờ giữa các máy
    ttl = (
        timedelta(minutes=settings.jwt_access_ttl_minutes)
        if token_type is TokenType.ACCESS
        else timedelta(days=settings.jwt_refresh_ttl_days)
    )

    claims: dict[str, Any] = {
        "sub": subject,
        "email": email,
        "role": str(role),
        "typ": str(token_type),
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "iss": settings.app_name,
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(settings: Settings, token: str) -> TokenPayload:
    """
    Giải mã + xác thực token.

    QUAN TRỌNG: luôn truyền algorithms=[...] tường minh. Nếu để pyjwt tự
    đoán, attacker có thể gửi token với alg="none" -> bỏ qua chữ ký.
    """
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.app_name,
            options={"require": ["exp", "sub", "iss"]},
        )
    except jwt.ExpiredSignatureError as err:
        # "raise ... from err" giữ lại nguyên nhân gốc -> debug dễ hơn nhiều.
        raise UnauthorizedError("Token đã hết hạn") from err
    except jwt.InvalidTokenError as err:
        raise UnauthorizedError("Token không hợp lệ") from err

    return TokenPayload.from_claims(claims)


def require_role(payload: TokenPayload, required: Role) -> None:
    """Guard clause: không đủ quyền thì chặn ngay."""
    if not has_permission(payload.role, required):
        raise ForbiddenError(
            f"Cần quyền {required} trở lên (bạn đang là {payload.role})",
            details={"required": str(required), "actual": str(payload.role)},
        )


# =============================================================================
# 3. Chữ ký webhook (HMAC)
# =============================================================================
def sign_payload(secret: str, body: bytes) -> str:
    """
    Tính chữ ký HMAC-SHA256 cho body.

    Đây là cách Datadog/GitHub/Stripe bảo vệ webhook: bên gửi và bên nhận
    cùng biết secret. Kẻ thứ 3 không có secret -> không tạo được chữ ký hợp lệ.
    """
    mac = hmac.new(secret.encode(), body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """So sánh chữ ký an toàn với thời gian hằng số."""
    if not signature:
        return False
    return hmac.compare_digest(sign_payload(secret, body), signature)
