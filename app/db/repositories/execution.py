"""Repository cho Execution / StepResult."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.db.models.execution import Execution
from app.db.repositories.base import BaseRepository
from app.domain.enums import ExecutionStatus


class ExecutionRepository(BaseRepository[Execution]):
    model = Execution

    async def list_for_incident(self, incident_id: str) -> Sequence[Execution]:
        """Toàn bộ lịch sử chạy của một incident, mới nhất trước."""
        stmt = (
            select(Execution)
            .where(Execution.incident_id == incident_id)
            .options(selectinload(Execution.step_results))
            .order_by(Execution.created_at.desc())
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def last_execution(self, incident_id: str) -> Execution | None:
        stmt = (
            select(Execution)
            .where(Execution.incident_id == incident_id)
            .order_by(Execution.attempt.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def count_recent_for_runbook(self, runbook_id: str, *, within_minutes: int = 60) -> int:
        """
        Đếm số lần chạy gần đây của một runbook.

        Dùng làm "cầu chì": nếu một runbook chạy 50 lần trong 1 giờ thì
        gần như chắc chắn nó đang lặp vô ích -> nên tắt và gọi người.
        """
        since = datetime.now(UTC) - timedelta(minutes=within_minutes)
        stmt = (
            select(func.count())
            .select_from(Execution)
            .where(Execution.runbook_id == runbook_id, Execution.created_at >= since)
        )
        return int((await self.session.execute(stmt)).scalar() or 0)

    async def success_rate(self, runbook_id: str) -> float:
        """
        Tỉ lệ thành công của runbook.

        GROUP BY ở DB rồi mới tính -> không kéo hàng nghìn row về app.
        Kết quả dùng cho dashboard: runbook nào hay thất bại thì phải sửa.
        """
        stmt = (
            select(Execution.status, func.count())
            .where(Execution.runbook_id == runbook_id)
            .group_by(Execution.status)
        )
        rows = (await self.session.execute(stmt)).all()

        # rows là list các Row (status, count). Chú thích kiểu tường minh để
        # mypy biết chính xác kiểu của counts.
        counts: dict[ExecutionStatus, int] = {row[0]: row[1] for row in rows}
        total = sum(counts.values())
        if total == 0:
            return 0.0
        return counts.get(ExecutionStatus.SUCCEEDED, 0) / total  # int / int -> float
