"""
app.db.repositories - Repository Pattern.

Ý TƯỞNG: service layer không viết SQL/ORM trực tiếp. Nó gọi repository với
những câu hỏi nghiệp vụ ("tìm incident theo fingerprint"). Repository lo
việc dịch sang truy vấn.

LỢI ÍCH
  - Service dễ test: chỉ cần thay repository bằng bản giả (fake).
  - Query phức tạp nằm gọn một chỗ, không rải rác khắp code.
  - Đổi cách lưu trữ (Postgres -> MongoDB) thì chỉ sửa repository.
"""

from app.db.repositories.base import BaseRepository
from app.db.repositories.execution import ExecutionRepository
from app.db.repositories.incident import IncidentRepository
from app.db.repositories.runbook import RunbookRepository
from app.db.repositories.user import UserRepository

__all__ = [
    "BaseRepository",
    "ExecutionRepository",
    "IncidentRepository",
    "RunbookRepository",
    "UserRepository",
]
