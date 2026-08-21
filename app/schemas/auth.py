"""Schema cho đăng nhập / phân quyền."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, EmailStr, Field

from app.core.security import Role
from app.schemas.common import ORMModel


class LoginRequest(BaseModel):
    """
    Dữ liệu đăng nhập.

    EmailStr: Pydantic tự validate định dạng email, sai thì trả 422 kèm
    thông báo rõ ràng - không phải tự viết regex.

    min_length=8: chặn mật khẩu quá ngắn ngay tại cửa vào.
    """

    email: EmailStr
    password: Annotated[str, Field(min_length=8, max_length=128)]


class TokenResponse(BaseModel):
    """
    Cặp token trả về sau khi đăng nhập.

    VÌ SAO CÓ HAI TOKEN?
      access_token : sống ngắn (30 phút). Bị lộ thì thiệt hại có giới hạn.
      refresh_token: sống dài (7 ngày), CHỈ dùng để xin access token mới.
                     Không gửi kèm mỗi request nên ít nguy cơ bị lộ hơn.
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Số giây access_token còn hiệu lực")


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(ORMModel):
    """
    Thông tin user trả ra ngoài.

    Để ý: KHÔNG có hashed_password. Đây chính là lý do phải có DTO riêng
    thay vì trả model ORM.
    """

    id: str
    email: str
    full_name: str
    role: Role
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime


class UserCreate(BaseModel):
    """Tạo user mới - chỉ ADMIN được gọi."""

    email: EmailStr
    password: Annotated[str, Field(min_length=8, max_length=128)]
    full_name: Annotated[str, Field(max_length=255)] = ""
    role: Role = Role.VIEWER
