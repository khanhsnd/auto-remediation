"""
Repository tổng quát (generic) - CRUD dùng chung cho mọi model.

KIẾN THỨC PYTHON NÂNG CAO: GENERICS
Cú pháp mới (PEP 695, Python 3.12+):
    class BaseRepository[ModelT: Base]:   # ModelT bị chặn phải là con của Base

Cú pháp cũ (vẫn gặp nhiều trong code cũ, nên biết cả hai):
    from typing import Generic, TypeVar
    ModelT = TypeVar("ModelT", bound=Base)
    class BaseRepository(Generic[ModelT]): ...

Generic để làm gì? Để IDE/mypy biết `IncidentRepository.get()` trả về
Incident chứ không phải "một cái gì đó". Gõ sai thuộc tính là báo lỗi ngay,
không phải chờ tới lúc chạy.

LƯU Ý QUAN TRỌNG: Python XOÁ generic lúc runtime (type erasure). Vì vậy
KHÔNG lấy được class model từ ModelT khi chạy -> phải truyền class thật vào
qua thuộc tính `model`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.db.base import Base


class BaseRepository[ModelT: Base]:
    """
    CRUD cơ bản. Repository con chỉ cần khai báo `model` và thêm query riêng.

    Ví dụ:
        class UserRepository(BaseRepository[User]):
            model = User
    """

    # Repository con BẮT BUỘC ghi đè. type[ModelT] nghĩa là "class", không
    # phải instance (khác nhau: User vs User()).
    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        """
        Nhận session từ bên ngoài (Dependency Injection).

        KHÔNG tự tạo session trong đây. Vì sao? Để nhiều repository dùng
        CHUNG một session -> chung một transaction. Nếu mỗi repo một session,
        insert incident thành công nhưng insert execution lỗi thì dữ liệu
        không thể rollback cùng nhau -> DB rơi vào trạng thái nửa vời.
        """
        self.session = session

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """
        Hook chạy khi có class con được ĐỊNH NGHĨA (không phải khi khởi tạo).

        Dùng để kiểm tra ngay lúc import: class con nào quên khai báo `model`
        là báo lỗi luôn, thay vì đợi tới lúc gọi hàm mới AttributeError.

        Đây là cách nhẹ nhàng thay thế cho metaclass - dễ hiểu hơn nhiều.
        """
        super().__init_subclass__(**kwargs)
        if getattr(cls, "model", None) is None and not cls.__name__.startswith("_"):
            raise TypeError(f"{cls.__name__} phải khai báo thuộc tính 'model'")

    # ------------------------------------------------------------------ read
    async def get(self, entity_id: str) -> ModelT | None:
        """Lấy theo primary key. Không có thì trả None."""
        return await self.session.get(self.model, entity_id)

    async def get_or_404(self, entity_id: str) -> ModelT:
        """
        Lấy theo id, không có thì raise NotFoundError.

        Có sẵn hai biến thể (get / get_or_404) giúp chỗ gọi gọn hơn: nơi nào
        bắt buộc phải có thì dùng bản _or_404, không phải viết if mỗi lần.
        """
        obj = await self.get(entity_id)
        if obj is None:
            raise NotFoundError(
                f"Không tìm thấy {self.model.__name__} id={entity_id}",
                details={"id": entity_id},
            )
        return obj

    async def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        order_by: str = "created_at",
        descending: bool = True,
        **filters: Any,
    ) -> Sequence[ModelT]:
        """
        Liệt kê có phân trang + lọc động.

        **filters (VAR-KEYWORD): gom mọi tham số tên tuỳ ý thành dict.
            repo.list(status="received", service="payment-api")
            -> filters = {"status": "received", "service": "payment-api"}

        BẢO MẬT: chỉ nhận tên cột CÓ THẬT trong model. Nếu ghép chuỗi vào SQL
        thì đây là chỗ bị SQL injection. Ở đây dùng getattr trên model +
        tham số bind của SQLAlchemy nên an toàn.
        """
        stmt = select(self.model)

        for key, value in filters.items():
            if value is None:
                continue  # bỏ qua filter không truyền -> "optional filter"
            column = getattr(self.model, key, None)
            if column is None:
                continue  # tên cột lạ -> phớt lờ, không cho query bừa
            # Truyền list -> dịch thành IN (...)
            stmt = stmt.where(column.in_(value) if isinstance(value, list) else column == value)

        if (order_col := getattr(self.model, order_by, None)) is not None:
            stmt = stmt.order_by(order_col.desc() if descending else order_col.asc())

        stmt = stmt.limit(limit).offset(offset)

        result = await self.session.execute(stmt)
        # scalars() lấy đối tượng model (thay vì Row tuple); all() -> list.
        return result.scalars().all()

    async def count(self, **filters: Any) -> int:
        """
        Đếm số bản ghi.

        func.count() dịch thành COUNT(*) - đếm ở DB, KHÔNG load hết row về
        rồi len(). Với 1 triệu row thì khác biệt là sống và chết.
        """
        stmt = select(func.count()).select_from(self.model)
        for key, value in filters.items():
            if value is None:
                continue
            if (column := getattr(self.model, key, None)) is not None:
                stmt = stmt.where(column == value)
        return int((await self.session.execute(stmt)).scalar() or 0)

    async def exists(self, **filters: Any) -> bool:
        """Kiểm tra tồn tại - dùng LIMIT 1 nên nhanh hơn count()."""
        stmt = select(self.model.id).limit(1)  # type: ignore[attr-defined]
        for key, value in filters.items():
            if (column := getattr(self.model, key, None)) is not None:
                stmt = stmt.where(column == value)
        return (await self.session.execute(stmt)).first() is not None

    # ----------------------------------------------------------------- write
    def add(self, obj: ModelT) -> ModelT:
        """
        Đưa object vào session.

        KHÔNG async, và cũng KHÔNG commit. session.add() chỉ ghi vào bộ nhớ
        của session. Quyết định commit thuộc về service layer (Unit of Work).
        """
        self.session.add(obj)
        return obj

    async def flush(self) -> None:
        """
        Đẩy SQL xuống DB nhưng CHƯA commit.

        Dùng khi cần lấy id do DB sinh ra để insert bản ghi con, mà vẫn muốn
        có thể rollback toàn bộ nếu bước sau lỗi.
        """
        await self.session.flush()

    async def delete(self, obj: ModelT) -> None:
        await self.session.delete(obj)

    async def bulk_update(self, ids: Sequence[str], **values: Any) -> int:
        """
        UPDATE nhiều row bằng MỘT câu SQL.

        So sánh: load 1000 object rồi sửa từng cái = 1000 UPDATE.
        Cách này = 1 UPDATE. Đánh đổi: object đang nằm trong session sẽ
        không tự biết giá trị mới (nên thường gọi khi không cần chúng nữa).
        """
        if not ids:
            return 0
        stmt = (
            update(self.model)
            .where(self.model.id.in_(ids))  # type: ignore[attr-defined]
            .values(**values)
        )
        # cast() chỉ tác động tới type checker, KHÔNG đổi gì lúc chạy.
        # Cần nó vì session.execute() khai báo trả về Result, còn thuộc tính
        # rowcount chỉ có trên CursorResult (lệnh UPDATE/DELETE).
        result = cast(CursorResult[Any], await self.session.execute(stmt))
        return int(result.rowcount or 0)

    async def bulk_delete(self, ids: Sequence[str]) -> int:
        if not ids:
            return 0
        stmt = delete(self.model).where(self.model.id.in_(ids))  # type: ignore[attr-defined]
        result = cast(CursorResult[Any], await self.session.execute(stmt))
        return int(result.rowcount or 0)
