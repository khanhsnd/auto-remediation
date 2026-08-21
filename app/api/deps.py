"""
Dependencies của FastAPI - "tiêm" tài nguyên vào endpoint.

DEPENDENCY INJECTION TRONG FASTAPI HOẠT ĐỘNG THẾ NÀO?
    async def endpoint(db: AsyncSession = Depends(get_session)): ...
FastAPI đọc signature, thấy Depends -> tự gọi get_session(), lấy kết quả
truyền vào. Nếu dependency đó lại có dependency khác thì nó giải quyết đệ
quy, và CACHE trong cùng một request (gọi 3 chỗ chỉ tạo 1 session).

LỢI ÍCH LỚN NHẤT là TEST: chỉ cần
    app.dependency_overrides[get_current_user] = lambda: fake_user
là mọi endpoint đều thấy user giả - không phải sửa code production.

KIẾN THỨC PYTHON
  1. Annotated[T, Depends(...)] - cú pháp hiện đại, tái dùng được
  2. Type alias cho dependency  - viết 1 lần, dùng khắp nơi
  3. Closure/factory dependency - dependency có tham số
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.context import set_actor
from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security import (
    Role,
    TokenPayload,
    TokenType,
    decode_token,
    has_permission,
)
from app.db.models.user import User
from app.db.repositories.execution import ExecutionRepository
from app.db.repositories.incident import IncidentRepository
from app.db.repositories.runbook import RunbookRepository
from app.db.repositories.user import UserRepository
from app.db.session import get_session
from app.infra.kafka import EventPublisher
from app.infra.redis_client import RedisCache, enforce_rate_limit, get_redis
from app.services.incident_service import IncidentService
from app.services.remediation_engine import RemediationEngine
from app.services.runbook_matcher import RunbookMatcher

# =============================================================================
# Type alias - mẹo giúp code endpoint gọn đi rất nhiều.
# Thay vì viết:  db: AsyncSession = Depends(get_session)
# ta viết:       db: DbSession
# =============================================================================
DbSession = Annotated[AsyncSession, Depends(get_session)]
Config = Annotated[Settings, Depends(get_settings)]


# =============================================================================
# Tài nguyên dùng chung, lấy từ app.state (được tạo lúc startup)
# =============================================================================
def get_publisher(request: Request) -> EventPublisher | None:
    """
    Lấy Kafka publisher đã tạo sẵn ở lifespan.

    VÌ SAO qua app.state? Vì producer phải là singleton (xem app/infra/kafka.py).
    request.app.state là "túi đồ" mà FastAPI cho phép gắn object sống suốt
    đời ứng dụng.
    """
    return getattr(request.app.state, "publisher", None)


def get_cache(request: Request) -> RedisCache | None:
    return getattr(request.app.state, "cache", None)


Publisher = Annotated[EventPublisher | None, Depends(get_publisher)]
Cache = Annotated[RedisCache | None, Depends(get_cache)]


# =============================================================================
# Repository - mỗi cái chỉ là "session + model"
# =============================================================================
def get_incident_repo(db: DbSession) -> IncidentRepository:
    return IncidentRepository(db)


def get_runbook_repo(db: DbSession) -> RunbookRepository:
    return RunbookRepository(db)


def get_execution_repo(db: DbSession) -> ExecutionRepository:
    return ExecutionRepository(db)


def get_user_repo(db: DbSession) -> UserRepository:
    return UserRepository(db)


IncidentRepo = Annotated[IncidentRepository, Depends(get_incident_repo)]
RunbookRepo = Annotated[RunbookRepository, Depends(get_runbook_repo)]
ExecutionRepo = Annotated[ExecutionRepository, Depends(get_execution_repo)]
UserRepo = Annotated[UserRepository, Depends(get_user_repo)]


# =============================================================================
# Service - lắp ghép repository + infra
# =============================================================================
def get_incident_service(repo: IncidentRepo, publisher: Publisher, cfg: Config) -> IncidentService:
    """
    Dependency LỒNG NHAU: hàm này tự có dependency riêng.

    FastAPI giải quyết cả cây phụ thuộc, và cache theo request - nên
    IncidentRepo dùng ở đây và ở chỗ khác vẫn là CÙNG một session.
    """
    return IncidentService(repo, publisher, cfg)


def get_matcher(repo: RunbookRepo, cache: Cache, executions: ExecutionRepo) -> RunbookMatcher:
    return RunbookMatcher(repo, cache, executions)


def get_engine(
    incident_repo: IncidentRepo,
    runbook_repo: RunbookRepo,
    execution_repo: ExecutionRepo,
    publisher: Publisher,
    cache: Cache,
    cfg: Config,
) -> RemediationEngine:
    return RemediationEngine(incident_repo, runbook_repo, execution_repo, publisher, cache, cfg)


IncidentSvc = Annotated[IncidentService, Depends(get_incident_service)]
Matcher = Annotated[RunbookMatcher, Depends(get_matcher)]
Engine = Annotated[RemediationEngine, Depends(get_engine)]


# =============================================================================
# Xác thực
# =============================================================================
async def get_token_payload(
    cfg: Config,
    authorization: Annotated[str | None, Header()] = None,
) -> TokenPayload:
    """
    Đọc và xác thực Bearer token từ header Authorization.

    Header(...) nói với FastAPI: lấy giá trị từ HTTP header, không phải query
    string. Tên tham số `authorization` tự map sang header "Authorization"
    (FastAPI đổi _ thành - và không phân biệt hoa thường).

    Định dạng chuẩn: "Authorization: Bearer eyJhbGci..."
    """
    if not authorization:
        raise UnauthorizedError("Thiếu header Authorization")

    # partition an toàn hơn split(): không raise khi chuỗi thiếu dấu cách.
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise UnauthorizedError("Header Authorization phải có dạng: Bearer <token>")

    payload = decode_token(cfg, token)

    # Refresh token KHÔNG được dùng để gọi API thường - nó chỉ dùng để lấy
    # access token mới. Không chặn ở đây là một lỗ hổng bảo mật.
    if payload.token_type is not TokenType.ACCESS:
        raise UnauthorizedError("Cần access token, không phải refresh token")

    # Gắn vào contextvars -> mọi dòng log và audit sau đó tự có actor.
    set_actor(payload.email or payload.subject)
    return payload


TokenDep = Annotated[TokenPayload, Depends(get_token_payload)]


async def get_current_user(payload: TokenDep, repo: UserRepo) -> User:
    """
    Lấy User từ DB dựa trên token.

    VÌ SAO CÒN TRA DB KHI TOKEN ĐÃ CÓ THÔNG TIN?
    Vì token sống 30 phút. Trong 30 phút đó user có thể bị vô hiệu hoá (nghỉ
    việc, bị khoá). Tra DB đảm bảo quyền được kiểm tra theo trạng thái HIỆN
    TẠI. Đây là đánh đổi giữa hiệu năng và bảo mật - với hệ thống điều khiển
    hạ tầng thì luôn chọn bảo mật.
    """
    user = await repo.get(payload.subject)
    if user is None:
        raise UnauthorizedError("User không còn tồn tại")
    if not user.is_active:
        raise UnauthorizedError("Tài khoản đã bị vô hiệu hoá")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require(role: Role) -> Callable[[User], Coroutine[object, object, User]]:
    """
    DEPENDENCY FACTORY: sinh ra dependency kiểm tra quyền.

    Đây là CLOSURE - hàm bên trong "nhớ" biến `role` của hàm ngoài, dù hàm
    ngoài đã chạy xong từ lâu.

    Dùng:
        @router.post("/runbooks", dependencies=[Depends(require(Role.ADMIN))])
    hoặc lấy luôn user:
        async def endpoint(user: Annotated[User, Depends(require(Role.OPERATOR))]):

    Nhờ factory này mà không phải viết require_admin, require_operator,
    require_viewer... riêng lẻ (DRY).
    """

    async def _dependency(user: CurrentUser) -> User:
        # So quyền theo user LẤY TỪ DB (user.role), không tin role trong token.
        if not has_permission(user.role, role):
            raise ForbiddenError(
                f"Cần quyền {role} trở lên (bạn đang là {user.role})",
                details={"required": str(role), "actual": str(user.role)},
            )
        return user

    return _dependency


# =============================================================================
# Rate limit
# =============================================================================
async def rate_limit_webhook(request: Request, cfg: Config) -> None:
    """
    Giới hạn tần suất cho endpoint webhook.

    Định danh theo IP. Trong thực tế, sau load balancer thì phải đọc
    X-Forwarded-For - và chỉ tin header đó khi LB của mình đã set nó
    (client tự gửi được header này nên không tin mù quáng).
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    client_ip = (
        forwarded.split(",")[0].strip()
        if forwarded
        else (request.client.host if request.client else "unknown")
    )
    await enforce_rate_limit(
        get_redis(), f"webhook:{client_ip}", limit=cfg.rate_limit_per_minute
    )
