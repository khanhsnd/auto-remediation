"""
Model User - phục vụ đăng nhập và phân quyền.

KIẾN THỨC
  1. unique + index trên cột email
  2. Enum của Python lưu vào DB dưới dạng chuỗi
  3. Property tính toán (không phải cột DB)
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.security import Role
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_column


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    Người dùng hệ thống.

    Thứ tự kế thừa: Base trước (để SQLAlchemy nhận là model), rồi tới mixin.
    Python duyệt MRO (Method Resolution Order) từ trái sang phải.
    """

    __tablename__ = "users"

    # unique=True -> DB tự chặn 2 user cùng email (chặn ở tầng DB mới chắc,
    # vì check ở app vẫn có thể lọt khi 2 request vào cùng lúc - race condition).
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)

    # Không bao giờ đặt tên cột là "password" - tên phải nói rõ đây là HASH,
    # để không ai nhầm mà log nó ra hoặc so sánh trực tiếp.
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)

    full_name: Mapped[str] = mapped_column(String(255), default="")

    # enum_column: lưu thành VARCHAR (không dùng kiểu ENUM của Postgres, để
    # thêm role mới không phải ALTER TYPE) nhưng vẫn đọc lên thành Role.
    # Xem giải thích đầy đủ trong app/db/base.py.
    role: Mapped[Role] = mapped_column(
        enum_column(Role), default=Role.VIEWER, nullable=False
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    @property
    def is_admin(self) -> bool:
        """
        Property = thuộc tính tính toán, KHÔNG phải cột DB.

        Gọi user.is_admin (không có dấu ngoặc) như một thuộc tính bình thường.
        """
        return self.role is Role.ADMIN

    def __repr__(self) -> str:
        return f"<User {self.email} role={self.role}>"
