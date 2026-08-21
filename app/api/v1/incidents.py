"""
Endpoint quản lý incident: xem danh sách, chi tiết, kích hoạt xử lý.

KIẾN THỨC FASTAPI
  1. Query parameter với Annotated + Field -> validate và tài liệu tự động
  2. response_model -> lọc field trả ra, sinh OpenAPI
  3. Phân trang chuẩn (limit/offset) + trả về tổng số
  4. Phân quyền theo từng endpoint bằng dependency
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.api.deps import CurrentUser, Engine, IncidentRepo, IncidentSvc, require
from app.core.security import Role
from app.db.models.incident import Incident
from app.domain.enums import IncidentStatus, Severity
from app.schemas.common import Page
from app.schemas.execution import (
    ExecutionOut,
    RemediationReportOut,
    TriggerRemediationRequest,
)
from app.schemas.incident import (
    DashboardStats,
    IncidentDetailOut,
    IncidentOut,
    SuppressRequest,
)

router = APIRouter(prefix="/incidents", tags=["incidents"])


@router.get("", response_model=Page[IncidentOut], summary="Danh sách incident")
async def list_incidents(
    repo: IncidentRepo,
    user: CurrentUser,
    status_filter: Annotated[
        IncidentStatus | None,
        Query(alias="status", description="Lọc theo trạng thái"),
    ] = None,
    severity: Annotated[Severity | None, Query(description="Lọc theo mức độ")] = None,
    service: Annotated[str | None, Query(max_length=128)] = None,
    environment: Annotated[str | None, Query(max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[IncidentOut]:
    """
    Liệt kê incident, có lọc và phân trang.

    Chú ý `alias="status"`: tên biến Python là status_filter (vì `status` dễ
    trùng với module status của FastAPI), nhưng query string phía client vẫn
    là ?status=received. alias giúp tách biệt tên nội bộ với hợp đồng API.

    VÌ SAO limit CÓ TRẦN le=200? Không có trần thì client gọi ?limit=1000000
    là app kéo hết DB về RAM rồi chết. Luôn đặt trần cho mọi API danh sách.
    """
    # Chú thích dict[str, Any] để mypy hiểu **filters chỉ chứa tên cột,
    # không đụng tới các tham số keyword-only phía trước (limit/offset/...).
    filters: dict[str, Any] = {
        "status": status_filter,
        "severity": severity,
        "service": service,
        "environment": environment,
    }
    items = await repo.list(limit=limit, offset=offset, **filters)
    total = await repo.count(**filters)

    return Page[IncidentOut](
        # model_validate đọc từ ORM object nhờ from_attributes=True.
        items=[IncidentOut.model_validate(i) for i in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/stats", response_model=DashboardStats, summary="Số liệu tổng quan")
async def stats(service: IncidentSvc, user: CurrentUser) -> DashboardStats:
    """
    Dashboard: phân bố trạng thái, MTTR, service đang flapping.

    Đặt route /stats TRƯỚC /{incident_id} là bắt buộc: FastAPI khớp route
    theo thứ tự khai báo. Nếu đặt sau, "/stats" sẽ bị hiểu là incident_id.
    Đây là lỗi rất hay gặp với người mới.
    """
    data = await service.dashboard_stats()
    return DashboardStats(**data)


@router.get("/{incident_id}", response_model=IncidentDetailOut, summary="Chi tiết incident")
async def get_incident(
    incident_id: str, repo: IncidentRepo, user: CurrentUser
) -> IncidentDetailOut:
    """
    Chi tiết một incident, kèm runbook đã dùng.

    get_full() eager-load runbook + execution + step trong vài query -> tránh
    N+1. Với async SQLAlchemy thì eager load còn là điều kiện BẮT BUỘC, vì
    lazy load ngoài context async sẽ raise MissingGreenlet.
    """
    incident = await repo.get_full(incident_id)
    if incident is None:
        await repo.get_or_404(incident_id)  # raise NotFoundError với message chuẩn

    assert incident is not None
    detail = IncidentDetailOut.model_validate(incident)
    detail.runbook_name = incident.runbook.name if incident.runbook else None
    return detail


@router.get(
    "/{incident_id}/executions",
    response_model=list[ExecutionOut],
    summary="Lịch sử xử lý của incident",
)
async def list_executions(
    incident_id: str,
    repo: IncidentRepo,
    user: CurrentUser,
) -> list[ExecutionOut]:
    """
    Toàn bộ các lượt chạy runbook cho incident này, kèm chi tiết từng step.

    Đây là endpoint quan trọng nhất khi điều tra: "3h sáng hệ thống đã làm
    gì, bước nào lỗi, mất bao lâu".
    """
    from app.db.repositories.execution import ExecutionRepository

    exec_repo = ExecutionRepository(repo.session)
    executions = await exec_repo.list_for_incident(incident_id)
    return [ExecutionOut.model_validate(e) for e in executions]


@router.post(
    "/{incident_id}/remediate",
    response_model=RemediationReportOut,
    summary="Kích hoạt xử lý thủ công (OPERATOR)",
)
async def trigger_remediation(
    incident_id: str,
    payload: TriggerRemediationRequest,
    engine: Engine,
    user: Annotated[object, Depends(require(Role.OPERATOR))],
) -> RemediationReportOut:
    """
    Chạy runbook ngay, không chờ worker.

    Ai dùng? Người vận hành đã xử lý nguyên nhân gốc và muốn chạy lại, hoặc
    muốn thử dry_run để xem hệ thống DỰ ĐỊNH làm gì.

    Quyền OPERATOR trở lên (VIEWER chỉ được xem). Dependency require() lo
    việc kiểm tra, endpoint không phải viết if.
    """
    # user là email của người bấm -> ghi vào execution.triggered_by để audit.
    actor = getattr(user, "email", "unknown")
    report = await engine.process(incident_id, triggered_by=actor)

    # __dict__ của dataclass có slots=True thì không tồn tại, nên map tay.
    return RemediationReportOut(
        incident_id=report.incident_id,
        execution_id=report.execution_id,
        status=report.status,
        runbook_name=report.runbook_name,
        steps_total=report.steps_total,
        steps_succeeded=report.steps_succeeded,
        duration_ms=report.duration_ms,
        message=report.message,
    )


@router.post(
    "/{incident_id}/suppress",
    response_model=IncidentOut,
    summary="Bỏ qua incident (OPERATOR)",
)
async def suppress(
    incident_id: str,
    payload: SuppressRequest,
    service: IncidentSvc,
    user: Annotated[object, Depends(require(Role.OPERATOR))],
) -> Incident:
    """Đánh dấu bỏ qua - vd đang trong cửa sổ bảo trì đã lên kế hoạch."""
    return await service.suppress(incident_id, reason=payload.reason)


@router.post(
    "/{incident_id}/escalate",
    response_model=IncidentOut,
    summary="Chuyển cho người xử lý (OPERATOR)",
)
async def escalate(
    incident_id: str,
    payload: SuppressRequest,
    service: IncidentSvc,
    user: Annotated[object, Depends(require(Role.OPERATOR))],
) -> Incident:
    """Dừng tự động hoá, bàn giao cho người thật."""
    return await service.escalate(incident_id, reason=payload.reason)
