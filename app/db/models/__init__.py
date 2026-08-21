"""
Gom toàn bộ model vào một chỗ.

VÌ SAO CẦN FILE NÀY? SQLAlchemy chỉ biết bảng nào tồn tại khi module chứa
model đã được import. Nếu chỉ import Base rồi gọi create_all() thì sẽ không
có bảng nào được tạo, vì chưa module nào chạm tới model.

Import ở đây cũng giúp `relationship("Runbook")` (tham chiếu bằng chuỗi)
tìm được class tương ứng khi SQLAlchemy "configure mappers".
"""

from app.db.models.audit import AuditLog
from app.db.models.execution import Execution, StepResult
from app.db.models.incident import Incident
from app.db.models.runbook import Runbook, RunbookStep
from app.db.models.user import User

# __all__ khai báo "API công khai" của package: `from ... import *` chỉ lấy
# những tên này, và linter sẽ không báo "imported but unused".
__all__ = [
    "AuditLog",
    "Execution",
    "Incident",
    "Runbook",
    "RunbookStep",
    "StepResult",
    "User",
]
