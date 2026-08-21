"""Repository cho User."""

from __future__ import annotations

from sqlalchemy import select

from app.db.models.user import User
from app.db.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    """
    Kế thừa BaseRepository[User] -> tự có get/list/count/add...

    Chỉ cần viết thêm query đặc thù của User.
    """

    model = User

    async def get_by_email(self, email: str) -> User | None:
        """
        Tìm user theo email.

        .lower() vì email không phân biệt hoa thường; nếu không chuẩn hoá thì
        "Duyet@fpt.com" và "duyet@fpt.com" thành 2 tài khoản khác nhau.
        """
        stmt = select(User).where(User.email == email.strip().lower())
        return (await self.session.execute(stmt)).scalar_one_or_none()
