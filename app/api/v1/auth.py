"""
Endpoint xác thực: đăng nhập, làm mới token, xem thông tin mình.

KIẾN THỨC BẢO MẬT TRONG FILE NÀY
  1. Thông báo lỗi đăng nhập KHÔNG được nói rõ "sai email" hay "sai mật khẩu"
     -> tránh cho attacker dò xem email nào tồn tại (user enumeration).
  2. Vẫn băm mật khẩu dù email không tồn tại -> thời gian phản hồi giống nhau,
     chống timing attack.
  3. Refresh token chỉ đổi được access token, không dùng gọi API.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, status

from app.api.deps import Config, CurrentUser, UserRepo
from app.core.exceptions import UnauthorizedError
from app.core.security import (
    Role,
    TokenType,
    create_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.db.models.user import User
from app.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    TokenResponse,
    UserCreate,
    UserOut,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# Hash của một mật khẩu ngẫu nhiên, dùng làm "mồi" khi email không tồn tại.
# Nhờ nó, verify_password vẫn chạy đủ số vòng PBKDF2 -> thời gian phản hồi
# của "email sai" và "mật khẩu sai" gần như bằng nhau.
_DUMMY_HASH = hash_password("khong-ton-tai-mat-khau-nay")


@router.post("/login", response_model=TokenResponse, summary="Đăng nhập")
async def login(payload: LoginRequest, repo: UserRepo, cfg: Config) -> TokenResponse:
    """
    Đăng nhập bằng email + mật khẩu, trả về cặp token.

    Để ý cấu trúc: chỉ có MỘT thông báo lỗi duy nhất cho mọi trường hợp
    thất bại. Đó là cố ý.
    """
    user = await repo.get_by_email(payload.email)

    # Luôn gọi verify_password, dù user là None (dùng hash mồi).
    stored_hash = user.hashed_password if user else _DUMMY_HASH
    password_ok = verify_password(payload.password, stored_hash)

    if user is None or not password_ok or not user.is_active:
        raise UnauthorizedError("Email hoặc mật khẩu không đúng")

    user.last_login_at = datetime.now(UTC)
    await repo.session.commit()

    return TokenResponse(
        access_token=create_token(
            cfg, subject=user.id, email=user.email, role=user.role,
            token_type=TokenType.ACCESS,
        ),
        refresh_token=create_token(
            cfg, subject=user.id, email=user.email, role=user.role,
            token_type=TokenType.REFRESH,
        ),
        expires_in=cfg.jwt_access_ttl_minutes * 60,
    )


@router.post("/refresh", response_model=TokenResponse, summary="Làm mới access token")
async def refresh(payload: RefreshRequest, repo: UserRepo, cfg: Config) -> TokenResponse:
    """
    Đổi refresh token thành access token mới.

    Vẫn phải kiểm tra user trong DB: refresh token sống 7 ngày, trong thời
    gian đó user có thể đã bị khoá.
    """
    token_data = decode_token(cfg, payload.refresh_token)

    if token_data.token_type is not TokenType.REFRESH:
        raise UnauthorizedError("Cần refresh token")

    user = await repo.get(token_data.subject)
    if user is None or not user.is_active:
        raise UnauthorizedError("Tài khoản không còn hiệu lực")

    return TokenResponse(
        access_token=create_token(
            cfg, subject=user.id, email=user.email, role=user.role,
            token_type=TokenType.ACCESS,
        ),
        # Cấp lại luôn refresh token mới (rotation) - nếu token cũ bị lộ thì
        # cửa sổ tấn công ngắn hơn.
        refresh_token=create_token(
            cfg, subject=user.id, email=user.email, role=user.role,
            token_type=TokenType.REFRESH,
        ),
        expires_in=cfg.jwt_access_ttl_minutes * 60,
    )


@router.get("/me", response_model=UserOut, summary="Thông tin user hiện tại")
async def me(user: CurrentUser) -> User:
    """
    Trả về chính user đang đăng nhập.

    Chỉ cần khai báo `user: CurrentUser` là FastAPI tự lo: đọc header, verify
    JWT, tra DB, kiểm tra is_active. Toàn bộ nằm trong dependency.

    response_model=UserOut đảm bảo hashed_password KHÔNG bị lọt ra, dù hàm
    này trả về object User đầy đủ.
    """
    return user


@router.post(
    "/users",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Tạo user (chỉ ADMIN)",
)
async def create_user(payload: UserCreate, repo: UserRepo, admin: CurrentUser) -> User:
    """
    Tạo user mới.

    Ở đây kiểm tra quyền thủ công để anh thấy cách làm "chay"; các endpoint
    khác dùng dependency require(Role.ADMIN) cho gọn hơn.
    """
    if admin.role is not Role.ADMIN:
        raise UnauthorizedError("Chỉ ADMIN được tạo user")

    email = payload.email.strip().lower()
    if await repo.get_by_email(email) is not None:
        from app.core.exceptions import ConflictError

        raise ConflictError(f"Email {email} đã tồn tại")

    user = User(
        email=email,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
        role=payload.role,
    )
    repo.add(user)
    await repo.session.commit()
    return user
