"""
Nền tảng ORM: Base class + các mixin dùng chung.

SQLALCHEMY 2.0 STYLE (khác hẳn 1.x, đừng học theo tutorial cũ):
    CŨ (1.x):  name = Column(String(50), nullable=False)
    MỚI (2.0): name: Mapped[str] = mapped_column(String(50))
Cái mới dùng type hint để suy ra kiểu và nullable:
    Mapped[str]        -> NOT NULL
    Mapped[str | None] -> cho phép NULL
=> mypy/IDE hiểu được model, gõ sai tên cột là biết ngay.

KIẾN THỨC PYTHON
  1. Mixin - kế thừa nhiều lớp để tái dùng code (DRY)
  2. Type alias - đặt tên cho một kiểu phức tạp
  3. Callable làm default - hiểu vì sao phải truyền hàm chứ không truyền giá trị
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum as PyEnum
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, MetaData, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# -----------------------------------------------------------------------------
# Quy ước đặt tên constraint.
# Nếu không khai báo, Postgres tự sinh tên kiểu "ix_a1b2c3" -> sau này muốn
# viết migration DROP CONSTRAINT thì không biết tên. Đặt quy ước ngay từ đầu
# là kinh nghiệm quan trọng khi làm việc lâu dài với DB.
# -----------------------------------------------------------------------------
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",                  # index
    "uq": "uq_%(table_name)s_%(column_0_name)s",    # unique
    "ck": "ck_%(table_name)s_%(constraint_name)s",  # check
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# -----------------------------------------------------------------------------
# JSONB là kiểu riêng của Postgres (nhị phân, index được, query nhanh).
# SQLite không có JSONB -> with_variant() nói: "gặp sqlite thì dùng JSON thường".
# Nhờ vậy chạy test bằng SQLite in-memory mà không phải sửa model.
# -----------------------------------------------------------------------------
JsonDict = JSONB().with_variant(JSON(), "sqlite")



# -----------------------------------------------------------------------------
# Cách lưu Enum vào DB - CHI TIẾT QUAN TRỌNG, rất dễ sai.
#
# Nếu khai báo:  status: Mapped[IncidentStatus] = mapped_column(String(32))
# thì ghi xuống DB vẫn đúng, NHƯNG lúc đọc lên SQLAlchemy trả về str thô.
# Hậu quả: incident.status.is_terminal -> AttributeError: 'str' object has no
# attribute 'is_terminal'. Bug này chỉ lộ ra khi đọc lại từ DB, nên test kiểu
# "tạo object rồi assert" sẽ KHÔNG bắt được.
#
# Cách đúng: dùng Enum() của SQLAlchemy để nó biết chuyển đổi hai chiều.
#   native_enum=False    -> lưu thành VARCHAR, KHÔNG tạo kiểu ENUM của Postgres
#                           (thêm giá trị mới không phải chạy ALTER TYPE)
#   values_callable      -> lưu VALUE ("received") thay vì NAME ("RECEIVED")
#   create_constraint    -> mặc định False ở SQLAlchemy 2.0, nên không sinh
#                           CHECK constraint cứng nhắc
# -----------------------------------------------------------------------------
def enum_column(enum_cls: type[PyEnum], *, length: int = 32) -> Enum:
    """Tạo kiểu cột cho một Enum của Python."""
    return Enum(
        enum_cls,
        native_enum=False,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
    )


class Base(DeclarativeBase):
    """
    Lớp gốc của mọi model.

    DeclarativeBase (2.0) thay cho declarative_base() (1.x).
    metadata giữ toàn bộ "bản đồ" schema -> dùng cho create_all / Alembic.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # type_annotation_map: dạy SQLAlchemy cách map kiểu Python -> kiểu SQL.
    # Nhờ dòng này, khai báo `Mapped[dict[str, Any]]` là tự ra JSONB,
    # `Mapped[datetime]` là tự có timezone - không phải nhắc lại ở từng cột.
    type_annotation_map = {
        dict[str, Any]: JsonDict,
        datetime: DateTime(timezone=True),
    }

    def to_dict(self) -> dict[str, Any]:
        """
        Chuyển row thành dict.

        __table__.columns do SQLAlchemy sinh ra; dùng nó thì thêm cột mới
        không phải sửa hàm này (DRY).
        """
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}

    def __repr__(self) -> str:
        """repr đẹp giúp debug và đọc log dễ hơn nhiều."""
        return f"<{type(self).__name__} id={getattr(self, 'id', None)}>"


class UUIDPrimaryKeyMixin:
    """
    Mixin: thêm cột id kiểu UUID.

    VÌ SAO UUID THAY VÌ AUTO-INCREMENT INT?
      - Sinh được id ở phía app TRƯỚC khi insert -> tiện cho log/trace.
      - Không tiết lộ số lượng bản ghi (int tăng dần thì đoán được).
      - Gộp dữ liệu từ nhiều cluster không sợ trùng id.
    Nhược điểm: nặng hơn int, index kém locality hơn. Với hệ thống incident
    thì đánh đổi này hoàn toàn hợp lý.

    MIXIN LÀ GÌ? Class chỉ chứa MỘT mảnh chức năng, không dùng độc lập, được
    "trộn" vào class khác qua kế thừa. Python cho đa kế thừa nên rất tiện:
        class Incident(Base, UUIDPrimaryKeyMixin, TimestampMixin)
    """

    # String(36) thay vì kiểu UUID native -> chạy được cả trên SQLite khi test.
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),  # callable -> gọi lại mỗi lần insert
    )


class TimestampMixin:
    """
    Mixin: thêm created_at / updated_at.

    server_default=func.now() -> để DATABASE sinh thời gian, không phải app.
    Quan trọng: nhiều pod app có thể lệch giờ nhau, còn DB thì chỉ có một.

    onupdate=func.now() -> tự cập nhật khi UPDATE row.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


def utcnow() -> datetime:
    """
    Helper dùng cho default ở phía Python.

    Viết thành HÀM (không truyền datetime.now(UTC) trực tiếp) vì default cần
    một callable - nếu truyền giá trị, nó bị "đóng băng" tại thời điểm import
    module và mọi row sẽ có cùng timestamp. Đây là bug rất hay gặp.
    """
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """
    Đảm bảo datetime có timezone. Thiếu thì coi như UTC.

    VÌ SAO CẦN? Dù cột khai `DateTime(timezone=True)`, mỗi database trả về
    khác nhau:
        Postgres -> datetime CÓ tzinfo (aware)
        SQLite   -> datetime KHÔNG có tzinfo (naive)
    Trừ hai datetime lệch loại nhau thì Python raise:
        TypeError: can't subtract offset-naive and offset-aware datetimes

    Bug này chỉ lộ ra khi chạy trên SQLite (tức là lúc test) hoặc ngược lại,
    nên rất dễ trượt. Gọi hàm này trước mọi phép tính thời gian lấy từ DB.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
