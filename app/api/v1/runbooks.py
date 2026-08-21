"""
Endpoint quản lý runbook - "bộ não" của hệ thống tự phục hồi.

CẢNH BÁO BẢO MẬT: ai tạo/sửa được runbook là điều khiển được hạ tầng
production. Vì vậy mọi endpoint ghi ở đây đều yêu cầu quyền ADMIN.

KIẾN THỨC
  1. CRUD đầy đủ với FastAPI
  2. PATCH bán phần với exclude_unset
  3. Invalidate cache sau khi ghi
  4. Endpoint "test" cho phép thử luật mà không chạy thật
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import CurrentUser, Matcher, RunbookRepo, require
from app.core.exceptions import ConflictError
from app.core.security import Role
from app.db.models.runbook import Runbook, RunbookStep
from app.domain import rules
from app.schemas.common import Page
from app.schemas.runbook import (
    MatchTestRequest,
    MatchTestResponse,
    RunbookIn,
    RunbookOut,
    RunbookUpdate,
)

router = APIRouter(prefix="/runbooks", tags=["runbooks"])


@router.get("", response_model=Page[RunbookOut], summary="Danh sách runbook")
async def list_runbooks(
    repo: RunbookRepo,
    user: CurrentUser,
    enabled: Annotated[bool | None, Query(description="Lọc theo trạng thái bật/tắt")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[RunbookOut]:
    """Ai đăng nhập cũng xem được runbook (VIEWER trở lên)."""
    items = await repo.list(limit=limit, offset=offset, order_by="priority",
                            descending=False, enabled=enabled)
    total = await repo.count(enabled=enabled)
    return Page[RunbookOut](
        items=[RunbookOut.model_validate(rb) for rb in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{runbook_id}", response_model=RunbookOut, summary="Chi tiết runbook")
async def get_runbook(runbook_id: str, repo: RunbookRepo, user: CurrentUser) -> Runbook:
    return await repo.get_or_404(runbook_id)


@router.post(
    "",
    response_model=RunbookOut,
    status_code=status.HTTP_201_CREATED,
    summary="Tạo runbook (ADMIN)",
)
async def create_runbook(
    payload: RunbookIn,
    repo: RunbookRepo,
    matcher: Matcher,
    admin: Annotated[object, Depends(require(Role.ADMIN))],
) -> Runbook:
    """
    Tạo runbook mới.

    match_rules đã được RunbookIn validate (chạy thử với dữ liệu mẫu), nên
    tới đây chắc chắn luật hợp lệ về cú pháp.

    order_index sinh tự động theo thứ tự trong mảng steps -> người dùng chỉ
    cần sắp đúng thứ tự, không phải tự đánh số (dễ sai và dễ trùng).
    """
    if await repo.get_by_name(payload.name) is not None:
        raise ConflictError(f"Runbook '{payload.name}' đã tồn tại")

    runbook = Runbook(
        name=payload.name,
        description=payload.description,
        enabled=payload.enabled,
        priority=payload.priority,
        match_rules=payload.match_rules,
        max_attempts=payload.max_attempts,
        cooldown_seconds=payload.cooldown_seconds,
        requires_approval=payload.requires_approval,
    )

    # enumerate(..., start=0) cho order_index bắt đầu từ 0.
    # Gán vào runbook.steps thì SQLAlchemy tự set runbook_id khi flush,
    # nhờ quan hệ back_populates đã khai báo trong model.
    for idx, step_in in enumerate(payload.steps):
        runbook.steps.append(
            RunbookStep(
                order_index=idx,
                name=step_in.name,
                action_type=step_in.action_type,
                params=step_in.params,
                timeout_seconds=step_in.timeout_seconds,
                retries=step_in.retries,
                continue_on_failure=step_in.continue_on_failure,
                rollback_params=step_in.rollback_params,
            )
        )

    repo.add(runbook)
    await repo.session.commit()

    # BẮT BUỘC: xoá cache, nếu không engine còn dùng danh sách cũ tới 60s.
    await matcher.invalidate_cache()
    return runbook


@router.patch("/{runbook_id}", response_model=RunbookOut, summary="Sửa runbook (ADMIN)")
async def update_runbook(
    runbook_id: str,
    payload: RunbookUpdate,
    repo: RunbookRepo,
    matcher: Matcher,
    admin: Annotated[object, Depends(require(Role.ADMIN))],
) -> Runbook:
    """
    Sửa một phần runbook.

    exclude_unset=True là mấu chốt của PATCH: nó CHỈ lấy field mà client
    thực sự gửi lên. Nhờ đó phân biệt được:
        {"enabled": false}  -> chỉ đổi enabled
        {}                  -> không đổi gì
    Nếu dùng model_dump() thường, mọi field None sẽ ghi đè dữ liệu cũ thành
    null - đây là bug rất hay gặp khi làm API PATCH.
    """
    runbook = await repo.get_or_404(runbook_id)

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return runbook

    # Validate lại luật nếu client đổi match_rules.
    if "match_rules" in changes and changes["match_rules"]:
        rules.matches({"service": "x", "severity": "high", "labels": {}}, changes["match_rules"])

    # setattr động: gọn hơn viết 7 câu if. An toàn vì `changes` chỉ chứa
    # các key đã được Pydantic kiểm duyệt (không phải input tuỳ ý).
    for key, value in changes.items():
        setattr(runbook, key, value)

    await repo.session.commit()
    await matcher.invalidate_cache()
    return runbook


@router.delete(
    "/{runbook_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Xoá runbook (ADMIN)",
)
async def delete_runbook(
    runbook_id: str,
    repo: RunbookRepo,
    matcher: Matcher,
    admin: Annotated[object, Depends(require(Role.ADMIN))],
) -> None:
    """
    Xoá runbook.

    LƯU Ý VẬN HÀNH: trong hệ thống thật, nên ưu tiên `enabled=false` thay vì
    xoá. Xoá làm mất lịch sử "incident này từng được xử lý bằng runbook nào".
    Ở đây cột runbook_id của incident dùng ON DELETE SET NULL nên incident
    không bị xoá theo, nhưng thông tin vẫn mất.

    204 No Content: theo chuẩn REST, DELETE thành công thì không trả body.
    """
    runbook = await repo.get_or_404(runbook_id)
    await repo.delete(runbook)
    await repo.session.commit()
    await matcher.invalidate_cache()


@router.post(
    "/test-match",
    response_model=MatchTestResponse,
    summary="Thử xem alert giả khớp runbook nào (KHÔNG chạy gì)",
)
async def test_match(
    payload: MatchTestRequest,
    matcher: Matcher,
    repo: RunbookRepo,
    user: CurrentUser,
) -> MatchTestResponse:
    """
    "Dry-run" cho việc so khớp.

    Đây là tính năng nhỏ nhưng cực giá trị: trước khi bật một runbook trên
    production, ta kiểm tra được nó có khớp đúng loại alert mình muốn hay
    không - và quan trọng hơn, có VÔ TÌNH khớp alert khác không.

    Cách làm: tạo một Incident TRONG BỘ NHỚ (không add vào session, không
    commit) rồi đưa qua matcher. Object ORM chưa persist vẫn dùng được bình
    thường như object Python.
    """
    from app.db.models.incident import Incident
    from app.domain.enums import AlertSource, Severity

    fake = Incident(
        fingerprint="test",
        source=AlertSource.CUSTOM,
        title=payload.title or "test alert",
        severity=Severity(payload.severity),
        service=payload.service,
        environment=payload.environment,
        labels=dict(payload.labels),
        attempt_count=0,
    )

    result = await matcher.match(fake)
    return MatchTestResponse(
        matched=result.matched,
        runbook_name=result.runbook.name if result.runbook else None,
        reason=result.reason,
        candidates=list(result.candidates),
    )
