"""Repository cho Runbook."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.models.runbook import Runbook
from app.db.repositories.base import BaseRepository


class RunbookRepository(BaseRepository[Runbook]):
    model = Runbook

    async def get_by_name(self, name: str) -> Runbook | None:
        stmt = select(Runbook).where(Runbook.name == name)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_enabled(self) -> Sequence[Runbook]:
        """
        Lấy mọi runbook đang bật, kèm sẵn các step.

        selectinload(Runbook.steps): nói rõ "load step luôn trong cùng lượt
        này". Model đã khai lazy="selectin" rồi, nhưng viết tường minh ở đây
        để anh thấy cách chủ động chống N+1 khi query phức tạp hơn.

        Thứ tự sắp xếp = thứ tự ưu tiên khi so khớp:
          priority tăng dần (số nhỏ = ưu tiên cao), rồi tới tên cho ổn định.
        """
        stmt = (
            select(Runbook)
            .where(Runbook.enabled.is_(True))
            .options(selectinload(Runbook.steps))
            .order_by(Runbook.priority.asc(), Runbook.name.asc())
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def get_with_steps(self, runbook_id: str) -> Runbook | None:
        stmt = (
            select(Runbook)
            .where(Runbook.id == runbook_id)
            .options(selectinload(Runbook.steps))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()
