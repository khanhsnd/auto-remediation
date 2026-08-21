"""
Tạo bảng + nạp dữ liệu mẫu.

Chạy:  python -m app.db.seed     (hoặc: make seed)

LƯU Ý VỀ MIGRATION: ở đây dùng metadata.create_all() cho nhanh khi học.
Project thật PHẢI dùng Alembic, vì create_all() chỉ tạo bảng chưa có - nó
KHÔNG biết cách sửa bảng đã tồn tại (thêm cột, đổi kiểu). Lên production
với create_all là tự đặt bẫy cho mình.

KIẾN THỨC PYTHON
  1. run_sync() - chạy hàm đồng bộ (create_all) trong ngữ cảnh async
  2. Cấu trúc dữ liệu khai báo (list of dict) thay vì code lặp
  3. Idempotent seed: chạy lại nhiều lần không tạo trùng
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.core.security import Role, hash_password
from app.db.base import Base
from app.db.models import Runbook, RunbookStep, User  # noqa: F401 - để create_all thấy bảng
from app.db.repositories.runbook import RunbookRepository
from app.db.repositories.user import UserRepository
from app.db.session import dispose_engine, init_engine, session_scope
from app.domain.enums import ActionType
from app.utils.console import enable_utf8_output

logger = get_logger(__name__)


# =============================================================================
# Dữ liệu runbook mẫu - mô phỏng các tình huống thật hay gặp
# =============================================================================
RUNBOOK_SEEDS: list[dict[str, Any]] = [
    {
        "name": "restart-oom-service",
        "description": "Service bị OOM (hết RAM) -> restart pod rồi kiểm tra lại",
        "priority": 10,
        "match_rules": {
            "all": [
                {"field": "severity", "op": "in", "value": ["critical", "high"]},
                {"field": "labels.alertname", "op": "regex", "value": "(?i)(oom|memory)"},
            ],
            "none": [
                # Đang bảo trì thì đừng tự động can thiệp.
                {"field": "labels.maintenance", "op": "exists"},
            ],
        },
        "max_attempts": 2,
        "cooldown_seconds": 300,
        "steps": [
            {
                "name": "Thông báo bắt đầu xử lý",
                "action_type": ActionType.NOTIFY,
                "params": {
                    "channel": "sre-oncall",
                    "level": "warning",
                    "message": "Bắt đầu tự động restart {service} do OOM",
                },
                "continue_on_failure": True,  # thông báo lỗi thì vẫn chạy tiếp
            },
            {
                "name": "Restart service qua agent",
                "action_type": ActionType.GRPC_COMMAND,
                "params": {
                    "action": "RESTART_SERVICE",
                    "target": "{service}",
                    "args": {"namespace": "{environment}"},
                },
                "timeout_seconds": 60,
                "retries": 1,
            },
            {
                "name": "Chờ service warm-up",
                "action_type": ActionType.WAIT,
                "params": {"seconds": 20},
            },
            {
                "name": "Xác minh health",
                "action_type": ActionType.HEALTH_CHECK,
                "params": {
                    "url": "http://localhost:8000/health/live",
                    "retries": 5,
                    "interval": 5,
                },
                "timeout_seconds": 60,
            },
        ],
    },
    {
        "name": "scale-out-high-cpu",
        "description": "CPU cao kéo dài -> tăng số replica, có rollback",
        "priority": 20,
        "match_rules": {
            "all": [
                {"field": "labels.alertname", "op": "regex", "value": "(?i)cpu"},
                {"field": "severity_weight", "op": "gte", "value": 2},
                {"field": "environment", "op": "eq", "value": "production"},
            ],
        },
        "max_attempts": 1,
        "cooldown_seconds": 900,
        "steps": [
            {
                "name": "Scale up workload",
                "action_type": ActionType.GRPC_COMMAND,
                "params": {
                    "action": "SCALE_WORKLOAD",
                    "target": "{service}",
                    "args": {"replicas": "6"},
                },
                # Nếu bước sau thất bại thì trả về số replica cũ.
                "rollback_params": {
                    "action": "SCALE_WORKLOAD",
                    "target": "{service}",
                    "args": {"replicas": "3"},
                },
                "timeout_seconds": 120,
            },
            {
                "name": "Chờ pod mới sẵn sàng",
                "action_type": ActionType.WAIT,
                "params": {"seconds": 30},
            },
            {
                "name": "Xác minh health sau khi scale",
                "action_type": ActionType.HEALTH_CHECK,
                "params": {"url": "http://localhost:8000/health/live", "retries": 4},
            },
        ],
    },
]

# Bổ sung các runbook còn lại. Tách ra một lệnh extend cho dễ đọc từng nhóm.
RUNBOOK_SEEDS.extend(
    [
        {
            "name": "clear-disk-full",
            "description": "Đầy đĩa -> dọn log, xoá cache tạm",
            "priority": 15,
            "match_rules": {
                "any": [
                    {"field": "labels.alertname", "op": "regex", "value": "(?i)(disk|filesystem)"},
                    {"field": "title", "op": "contains", "value": "disk"},
                ],
            },
            "max_attempts": 2,
            "cooldown_seconds": 600,
            "steps": [
                {
                    "name": "Nén và xoá log cũ",
                    "action_type": ActionType.GRPC_COMMAND,
                    "params": {
                        "action": "ROTATE_LOG",
                        "target": "{service}",
                        "args": {"keep_days": "3"},
                    },
                    "timeout_seconds": 90,
                    "retries": 1,
                },
                {
                    "name": "Xoá cache tạm",
                    "action_type": ActionType.GRPC_COMMAND,
                    "params": {"action": "CLEAR_CACHE", "target": "{service}"},
                    "continue_on_failure": True,
                },
            ],
        },
        {
            "name": "db-connection-exhausted",
            "description": "Cạn connection pool DB - NGUY HIỂM, cần người duyệt",
            "priority": 5,
            "match_rules": {
                "all": [
                    {"field": "labels.alertname", "op": "regex", "value": "(?i)(connection|pool)"},
                    {"field": "severity", "op": "eq", "value": "critical"},
                ],
            },
            # requires_approval=True -> engine KHÔNG tự chạy, chỉ chuyển sang
            # TRIAGED rồi chờ người bấm nút. Mọi hành động chạm vào DB nên vậy.
            "requires_approval": True,
            "max_attempts": 1,
            "steps": [
                {
                    "name": "Kill các connection idle quá lâu",
                    "action_type": ActionType.GRPC_COMMAND,
                    "params": {
                        "action": "KILL_PROCESS",
                        "target": "{service}",
                        "args": {"filter": "idle_in_transaction"},
                    },
                    "timeout_seconds": 60,
                },
            ],
        },
        {
            "name": "catch-all-escalate",
            "description": "Lưới cuối: không runbook nào khớp -> ghi nhận và gọi người",
            # priority lớn nhất -> luôn được xét sau cùng.
            "priority": 999,
            # match_rules rỗng = khớp MỌI alert.
            "match_rules": {},
            "max_attempts": 1,
            "cooldown_seconds": 0,
            "steps": [
                {
                    "name": "Ghi nhận",
                    "action_type": ActionType.NOOP,
                    "params": {"note": "Không có runbook chuyên biệt"},
                },
                {
                    "name": "Gọi on-call",
                    "action_type": ActionType.NOTIFY,
                    "params": {
                        "channel": "sre-oncall",
                        "level": "critical",
                        "message": "Alert {service} ({severity}) chưa có runbook, cần người xem",
                    },
                },
            ],
        },
    ]
)

# Tài khoản mẫu. MẬT KHẨU NÀY CHỈ DÙNG Ở LOCAL.
USER_SEEDS: list[dict[str, Any]] = [
    {
        "email": "admin@local.dev",
        "password": "admin12345",
        "role": Role.ADMIN,
        "full_name": "Quan tri he thong",
    },
    {
        "email": "operator@local.dev",
        "password": "operator12345",
        "role": Role.OPERATOR,
        "full_name": "Van hanh",
    },
    {
        "email": "viewer@local.dev",
        "password": "viewer12345",
        "role": Role.VIEWER,
        "full_name": "Chi xem",
    },
]


async def create_tables() -> None:
    """
    Tạo toàn bộ bảng.

    engine.begin() mở transaction; run_sync cho phép gọi hàm ĐỒNG BỘ
    (create_all) bên trong ngữ cảnh async. SQLAlchemy làm việc này qua
    greenlet - ta chỉ cần biết là dùng run_sync khi gặp API sync của nó.
    """
    engine = init_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Đã tạo/kiểm tra %d bảng", len(Base.metadata.tables))


async def seed_users() -> int:
    """
    Nạp user mẫu. Bỏ qua nếu email đã tồn tại -> chạy lại nhiều lần vô hại
    (idempotent). Tính chất này rất quan trọng cho script deploy.
    """
    created = 0
    async with session_scope() as db:
        repo = UserRepository(db)
        for spec in USER_SEEDS:
            if await repo.get_by_email(spec["email"]) is not None:
                continue
            repo.add(
                User(
                    email=spec["email"],
                    hashed_password=hash_password(spec["password"]),
                    full_name=spec["full_name"],
                    role=spec["role"],
                )
            )
            created += 1
    logger.info("Đã tạo %d user mới", created)
    return created


async def seed_runbooks() -> int:
    """Nạp runbook mẫu, bỏ qua cái đã có."""
    created = 0
    async with session_scope() as db:
        repo = RunbookRepository(db)
        for spec in RUNBOOK_SEEDS:
            if await repo.get_by_name(spec["name"]) is not None:
                continue

            # dict.pop lấy giá trị và XOÁ key khỏi dict -> phần còn lại đưa
            # thẳng vào constructor bằng **spec, không phải liệt kê tay.
            steps_spec = spec.pop("steps")
            runbook = Runbook(**spec)

            for idx, step in enumerate(steps_spec):
                runbook.steps.append(RunbookStep(order_index=idx, **step))

            repo.add(runbook)
            created += 1
    logger.info("Đã tạo %d runbook mới", created)
    return created


async def main() -> None:
    """Chạy toàn bộ: tạo bảng -> nạp user -> nạp runbook."""
    enable_utf8_output()
    cfg = get_settings()
    setup_logging(cfg)

    logger.info("Bắt đầu seed database: %s", cfg.database_url.split("@")[-1])
    # split("@")[-1] để KHÔNG in mật khẩu trong connection string ra log.

    try:
        await create_tables()
        users = await seed_users()
        runbooks = await seed_runbooks()

        print()
        print("=" * 64)
        print(f"  Đã tạo {users} user, {runbooks} runbook")
        print("=" * 64)
        print("  Tài khoản để đăng nhập thử (chỉ dùng ở local):")
        for spec in USER_SEEDS:
            print(f"    {spec['role']:<9} {spec['email']:<24} / {spec['password']}")
        print()
        print("  Bước tiếp theo:")
        print("    1. make api      -> http://localhost:8000/docs")
        print("    2. make grpc     (cửa sổ khác - agent giả lập)")
        print("    3. make worker   (cửa sổ khác - xử lý nền)")
        print("=" * 64)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
