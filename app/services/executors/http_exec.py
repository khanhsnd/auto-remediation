"""
Executor gọi HTTP API - dùng nhiều nhất trong thực tế.

Ví dụ ứng dụng thật:
  - Kubernetes API : scale deployment, delete pod
  - Jenkins/GitLab : kích hoạt job rollback
  - Cloud API      : reboot VM, thêm node vào cluster
  - Slack webhook  : thông báo cho on-call
"""

from __future__ import annotations

from typing import Any, ClassVar

from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger
from app.domain.enums import ActionType
from app.infra.http_client import request_json
from app.services.executors.base import ExecutionContext, Executor, StepOutcome

logger = get_logger(__name__)


class HttpCallExecutor(Executor):
    """
    Gọi một HTTP endpoint.

    Tham số trong runbook (cột params của bảng runbook_steps):
        {
          "method": "POST",
          "url": "http://k8s-api/apis/apps/v1/namespaces/prod/deployments/{service}/scale",
          "json": {"spec": {"replicas": 5}},
          "headers": {"Authorization": "Bearer ..."},
          "expect_status": [200, 201],
          "service_name": "k8s-api"
        }
    Chuỗi có {service} sẽ được ExecutionContext.render thay bằng giá trị thật.
    """

    # Chỉ cần dòng này là class tự vào registry (nhờ __init_subclass__).
    action_type: ClassVar[ActionType] = ActionType.HTTP_CALL

    async def run(self, ctx: ExecutionContext, params: dict[str, Any]) -> StepOutcome:
        # Render trước để {service}, {incident_id}... thành giá trị thật.
        p = ctx.render_params(params)

        url = p.get("url")
        if not url:
            # Thiếu url là lỗi CẤU HÌNH runbook -> raise để người viết
            # runbook biết mà sửa, không âm thầm bỏ qua.
            raise ValueError("HTTP_CALL cần tham số 'url'")

        method = str(p.get("method", "GET")).upper()

        # Dry-run: không gọi thật, chỉ ghi lại ý định. Cực kỳ quan trọng khi
        # kiểm thử runbook mới trên production.
        if ctx.dry_run:
            logger.info("[DRY-RUN] Sẽ gọi %s %s", method, url)
            return StepOutcome.ok(dry_run=True, method=method, url=url)

        try:
            result = await request_json(
                method,
                url,
                service_name=str(p.get("service_name", "http")),
                json_body=p.get("json"),
                headers=p.get("headers"),
                params=p.get("params"),
                timeout=p.get("timeout"),
            )
        except ExternalServiceError as err:
            # Lỗi khi gọi bên ngoài là chuyện BÌNH THƯỜNG trong vận hành ->
            # trả fail để engine tự quyết định retry hay rollback.
            return StepOutcome.fail(err.message, service=err.service)

        # Cho phép runbook khai báo status nào là "thành công".
        expected = p.get("expect_status") or [200, 201, 202, 204]
        if not isinstance(expected, list):
            expected = [expected]

        status = result["status_code"]
        if status not in expected:
            return StepOutcome.fail(
                f"Status {status} không nằm trong danh sách mong đợi {expected}",
                status_code=status,
                body=result["body"],
            )

        return StepOutcome.ok(status_code=status, body=result["body"])
