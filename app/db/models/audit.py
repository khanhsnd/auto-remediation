"""
Model AuditLog - sổ ghi "ai làm gì, lúc nào".

VÌ SAO BẮT BUỘC PHẢI CÓ với hệ thống tự động sửa hạ tầng?
Khi production sập lúc 2h sáng và có gì đó tự restart service, câu hỏi đầu
tiên luôn là "ai/cái gì đã làm việc đó?". Không có audit log thì không trả
lời được, và cũng không được các đợt kiểm toán (ISO/SOC2) thông qua.

Bảng này CHỈ GHI THÊM (append-only) - không bao giờ UPDATE hay DELETE.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AuditLog(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "audit_logs"

    # __table_args__ khai báo index/constraint ở mức BẢNG (nhiều cột).
    # Index tổ hợp (entity_type, entity_id) phục vụ truy vấn hay dùng nhất:
    # "cho tôi xem toàn bộ lịch sử của incident X".
    __table_args__ = (
        Index("ix_audit_entity", "entity_type", "entity_id"),
        Index("ix_audit_action_created", "action", "created_at"),
    )

    actor: Mapped[str] = mapped_column(String(255), default="system", index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)

    # Mapped[dict[str, Any]] -> tự thành JSONB nhờ type_annotation_map ở Base.
    # JSONB linh hoạt: mỗi loại action ghi payload khác nhau mà không cần
    # thêm cột. Đánh đổi: không có schema chặt, nên phải tự kỷ luật.
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)

    request_id: Mapped[str] = mapped_column(String(64), default="-", index=True)

    def __repr__(self) -> str:
        return f"<AuditLog {self.actor} {self.action} {self.entity_type}:{self.entity_id}>"
