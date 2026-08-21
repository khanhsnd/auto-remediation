"""
CLI gom các lệnh vận hành vào một chỗ.

Chạy:  python -m app.cli --help
       remediation --help          (sau khi poetry install)

KIẾN THỨC PYTHON
  1. argparse với sub-command (giống git commit / git push)
  2. asyncio.run trong hàm đồng bộ -> nối thế giới sync và async
  3. Trả exit code theo quy ước Unix (0 = thành công)
  4. Kiểu Callable để lưu hàm trong dict, thay cho if-elif dài
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Coroutine

from app import __version__
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.utils.console import enable_utf8_output


async def _cmd_seed(args: argparse.Namespace) -> int:
    """Tạo bảng + nạp dữ liệu mẫu."""
    from app.db.seed import main as seed_main

    await seed_main()
    return 0


async def _cmd_send_alert(args: argparse.Namespace) -> int:
    """
    Gửi một alert thử vào API đang chạy.

    Tiện hơn gõ curl dài dòng, và là cách nhanh nhất để xem toàn bộ luồng
    hoạt động sau khi dựng xong hệ thống.
    """
    import httpx

    payload = {
        "title": args.title,
        "service": args.service,
        "severity": args.severity,
        "environment": args.environment,
        "labels": {"alertname": args.alertname} if args.alertname else {},
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{args.url}/api/v1/webhooks/custom/simple",
            json=payload,
        )

    print(json.dumps(response.json(), indent=2, ensure_ascii=False))
    # response.is_success = status 2xx. Trả exit code khác 0 khi lỗi để dùng
    # được trong script shell (&&, ||).
    return 0 if response.is_success else 1


async def _cmd_match(args: argparse.Namespace) -> int:
    """Thử so khớp runbook mà không chạy gì - kiểm tra luật rất nhanh."""
    from app.db.models.incident import Incident
    from app.db.repositories.runbook import RunbookRepository
    from app.db.session import dispose_engine, init_engine, session_scope
    from app.domain.enums import AlertSource, Severity
    from app.services.runbook_matcher import RunbookMatcher

    init_engine()
    try:
        async with session_scope() as db:
            matcher = RunbookMatcher(RunbookRepository(db))
            fake = Incident(
                fingerprint="cli-test",
                source=AlertSource.CUSTOM,
                title=args.title,
                severity=Severity(args.severity),
                service=args.service,
                environment=args.environment,
                labels={"alertname": args.alertname} if args.alertname else {},
                attempt_count=0,
            )
            result = await matcher.match(fake)

        print(f"Khớp      : {result.matched}")
        print(f"Runbook   : {result.runbook.name if result.runbook else '(không có)'}")
        print(f"Lý do     : {result.reason}")
        print(f"Ứng viên  : {', '.join(result.candidates) or '(không có)'}")
        return 0 if result.matched else 1
    finally:
        await dispose_engine()


async def _cmd_config(args: argparse.Namespace) -> int:
    """
    In cấu hình hiện tại - để kiểm tra .env đã được đọc đúng chưa.

    CHE BÍ MẬT: không bao giờ in secret ra terminal/log. Ở đây chỉ hiện độ
    dài để biết là "có set" mà không tiết lộ giá trị.
    """
    cfg = get_settings()
    data = cfg.model_dump()

    secret_keys = {"jwt_secret", "webhook_shared_secret", "database_url"}
    for key in secret_keys:
        if value := data.get(key):
            data[key] = f"<đã set, {len(str(value))} ký tự>"

    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    return 0


# Bảng tra sub-command -> hàm xử lý. Thêm lệnh mới chỉ cần thêm 1 dòng.
COMMANDS: dict[str, Callable[[argparse.Namespace], Coroutine[object, object, int]]] = {
    "seed": _cmd_seed,
    "send-alert": _cmd_send_alert,
    "match": _cmd_match,
    "config": _cmd_config,
}


def build_parser() -> argparse.ArgumentParser:
    """
    Dựng parser với sub-command.

    add_subparsers tạo cấu trúc như git: `remediation seed`, `remediation match`.
    dest="command" lưu tên lệnh vào args.command để tra trong COMMANDS.
    """
    parser = argparse.ArgumentParser(
        prog="remediation",
        description="Công cụ dòng lệnh cho hệ thống Auto-Remediation",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command", required=True, metavar="LỆNH")

    sub.add_parser("seed", help="Tạo bảng và nạp dữ liệu mẫu")
    sub.add_parser("config", help="In cấu hình hiện tại (đã che secret)")

    p_send = sub.add_parser("send-alert", help="Gửi alert thử vào API")
    p_send.add_argument("--url", default="http://localhost:8000")
    p_send.add_argument("--title", default="High memory usage detected")
    p_send.add_argument("--service", default="payment-api")
    p_send.add_argument(
        "--severity",
        default="critical",
        choices=["critical", "high", "medium", "low", "info"],
    )
    p_send.add_argument("--environment", default="production")
    p_send.add_argument("--alertname", default="HighMemoryUsage")

    p_match = sub.add_parser("match", help="Thử so khớp runbook (không chạy)")
    p_match.add_argument("--service", default="payment-api")
    p_match.add_argument(
        "--severity",
        default="critical",
        choices=["critical", "high", "medium", "low", "info"],
    )
    p_match.add_argument("--environment", default="production")
    p_match.add_argument("--title", default="test alert")
    p_match.add_argument("--alertname", default="HighMemoryUsage")

    return parser


def main() -> int:
    """
    Điểm vào đồng bộ (entry point trong pyproject.toml phải là hàm sync).

    asyncio.run là cây cầu: nó tạo event loop, chạy coroutine, rồi dọn dẹp.
    Chỉ gọi asyncio.run ở TẦNG NGOÀI CÙNG, không bao giờ gọi lồng nhau.
    """
    enable_utf8_output()
    parser = build_parser()
    args = parser.parse_args()

    setup_logging(get_settings())

    handler = COMMANDS[args.command]
    return asyncio.run(handler(args))


if __name__ == "__main__":
    sys.exit(main())
