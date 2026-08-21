"""
Repository cho Incident - repository phức tạp nhất, nên có nhiều thứ để học.

KIẾN THỨC
  1. INSERT ... ON CONFLICT DO NOTHING (upsert) -> chống trùng an toàn
  2. Query thống kê với GROUP BY
  3. SELECT ... FOR UPDATE SKIP LOCKED -> nhiều worker chia việc không đụng nhau
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.db.models.execution import Execution
from app.db.models.incident import Incident
from app.db.repositories.base import BaseRepository
from app.domain.enums import IncidentStatus, Severity


class IncidentRepository(BaseRepository[Incident]):
    model = Incident

    async def get_by_fingerprint(self, fingerprint: str) -> Incident | None:
        """Tra theo vân tay - nhanh vì fingerprint có unique index."""
        stmt = select(Incident).where(Incident.fingerprint == fingerprint)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_full(self, incident_id: str) -> Incident | None:
        """
        Lấy incident kèm runbook và toàn bộ execution/step.

        Chuỗi selectinload lồng nhau: Incident -> executions -> step_results.
        Tất cả gói trong vài query, thay vì hàng chục query lẻ.
        """
        stmt = (
            select(Incident)
            .where(Incident.id == incident_id)
            .options(
                selectinload(Incident.runbook),
                selectinload(Incident.executions).selectinload(Execution.step_results),
            )
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def upsert_by_fingerprint(self, values: dict[str, Any]) -> tuple[Incident, bool]:
        """
        Thêm incident, nếu fingerprint đã tồn tại thì trả về bản cũ.

        Trả về (incident, is_new) - tuple để chỗ gọi biết có phải bản mới.

        VÌ SAO KHÔNG "SELECT rồi INSERT"?
            t0: worker A: SELECT -> không có
            t1: worker B: SELECT -> không có
            t2: worker A: INSERT -> OK
            t3: worker B: INSERT -> nổ UniqueViolation
        Đó là race condition. ON CONFLICT DO NOTHING đẩy việc kiểm tra xuống
        DB trong MỘT câu lệnh nguyên tử (atomic) -> hết cửa chen ngang.

        Lưu ý: pg_insert là cú pháp riêng của PostgreSQL. SQLite có
        sqlite_insert với on_conflict_do_nothing tương tự.
        """
        insert = _dialect_insert(self.session)
        stmt = (
            insert(Incident)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["fingerprint"])
            .returning(Incident.id)
        )
        inserted_id = (await self.session.execute(stmt)).scalar_one_or_none()

        if inserted_id is not None:
            await self.session.flush()
            incident = await self.get_or_404(inserted_id)
            return incident, True

        # inserted_id là None -> đã có row trùng, lấy bản cũ ra dùng.
        existing = await self.get_by_fingerprint(values["fingerprint"])
        assert existing is not None  # logic bảo đảm phải có
        return existing, False

    async def list_pending(self, *, limit: int = 20) -> Sequence[Incident]:
        """
        Lấy các incident chờ xử lý, ưu tiên nghiêm trọng trước.

        with_for_update(skip_locked=True) sinh ra:
            SELECT ... FOR UPDATE SKIP LOCKED
        Ý nghĩa: worker này khoá các row nó lấy; worker khác chạy cùng lúc sẽ
        BỎ QUA row đã bị khoá và lấy row khác. Đây là mẫu "work queue" trên
        Postgres, cho phép scale nhiều worker mà không xử lý trùng.

        CASE WHEN để sắp xếp theo mức nghiêm trọng: SQL không tự biết
        "critical" nặng hơn "low", nên phải ánh xạ sang số.
        """
        severity_rank = func.coalesce(
            _severity_case(),
            0,
        )
        stmt = (
            select(Incident)
            .where(Incident.status == IncidentStatus.RECEIVED)
            .order_by(severity_rank.desc(), Incident.occurred_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def stats_by_status(self, *, within_hours: int = 24) -> dict[str, int]:
        """
        Thống kê cho dashboard: mỗi status có bao nhiêu incident.

        Dict comprehension biến list tuple thành dict trong 1 dòng.
        """
        since = datetime.now(UTC) - timedelta(hours=within_hours)
        stmt = (
            select(Incident.status, func.count())
            .where(Incident.created_at >= since)
            .group_by(Incident.status)
        )
        rows = (await self.session.execute(stmt)).all()
        return {str(status): int(count) for status, count in rows}

    async def mttr_seconds(self, *, within_hours: int = 24) -> float | None:
        """
        MTTR - Mean Time To Recovery: trung bình bao lâu thì phục hồi xong.

        Đây là KPI quan trọng nhất của hệ thống auto-remediation: nó chứng
        minh giá trị của việc tự động hoá (từ 30 phút xuống 40 giây).

        EXTRACT(EPOCH FROM ...) là cú pháp Postgres để lấy số giây từ interval.
        """
        since = datetime.now(UTC) - timedelta(hours=within_hours)
        delta = func.extract("epoch", Incident.resolved_at - Incident.occurred_at)
        stmt = select(func.avg(delta)).where(
            Incident.status == IncidentStatus.RESOLVED,
            Incident.resolved_at.is_not(None),
            Incident.created_at >= since,
        )
        value = (await self.session.execute(stmt)).scalar()
        return float(value) if value is not None else None

    async def find_flapping(self, *, within_minutes: int = 30, threshold: int = 3) -> list[str]:
        """
        Tìm service "flapping" - lỗi lặp lại liên tục.

        Ý nghĩa vận hành: nếu 1 service tạo 5 incident trong 30 phút thì tự
        động sửa nữa là vô nghĩa (và có thể đang làm tình hình tệ hơn).
        Lúc này phải escalate cho người thật.

        HAVING lọc SAU khi GROUP BY (khác WHERE lọc trước khi nhóm).
        """
        since = datetime.now(UTC) - timedelta(minutes=within_minutes)
        stmt = (
            select(Incident.service, func.count().label("cnt"))
            .where(Incident.created_at >= since)
            .group_by(Incident.service)
            .having(func.count() >= threshold)
        )
        return [row[0] for row in (await self.session.execute(stmt)).all()]


def _dialect_insert(session: Any) -> Any:
    """
    Chọn hàm insert() đúng theo loại database đang dùng.

    VÌ SAO CẦN? "INSERT ... ON CONFLICT DO NOTHING" không thuộc SQL chuẩn.
    Mỗi database có cú pháp riêng, nên SQLAlchemy đặt hàm insert() mở rộng
    trong từng dialect:
        sqlalchemy.dialects.postgresql.insert  -> ON CONFLICT (Postgres)
        sqlalchemy.dialects.sqlite.insert      -> ON CONFLICT (SQLite 3.24+)
    Dùng sqlalchemy.insert() thường thì KHÔNG có method on_conflict_do_nothing.

    Nhờ hàm này mà code chạy được cả trên Postgres (production) và SQLite
    in-memory (test) - test nhanh mà vẫn kiểm chứng đúng logic chống trùng.

    Đây là mẫu ADAPTER ở mức nhỏ: cùng một ý định, hai cách diễn đạt.
    """
    dialect = session.get_bind().dialect.name

    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        return sqlite_insert

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    return pg_insert


def _severity_case() -> Any:
    """
    Sinh biểu thức CASE WHEN severity = 'critical' THEN 4 ... END.

    Tách ra hàm riêng cho dễ đọc và tái dùng. Dùng dict để định nghĩa bảng
    điểm, tránh viết tay 5 dòng when() giống nhau (DRY).
    """
    from sqlalchemy import case

    weights = {
        Severity.CRITICAL: 4,
        Severity.HIGH: 3,
        Severity.MEDIUM: 2,
        Severity.LOW: 1,
        Severity.INFO: 0,
    }
    # case() nhận các cặp (điều kiện, giá trị) - ở đây sinh bằng generator.
    return case(*[(Incident.severity == sev, score) for sev, score in weights.items()], else_=0)
