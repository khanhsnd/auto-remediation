"""
Endpoint nhận alert từ bên thứ 3 - CỬA VÀO của toàn hệ thống.

YÊU CẦU THIẾT KẾ (rút ra từ thực tế vận hành)
  1. NHANH: phải trả lời dưới ~50ms, nếu không bên gửi timeout và gửi lại.
  2. KHÔNG YÊU CẦU JWT: hệ thống monitoring không làm OAuth flow được.
     Thay vào đó xác thực bằng chữ ký HMAC trên body.
  3. CÓ RATE LIMIT: monitoring cấu hình sai có thể bắn hàng nghìn alert/phút.
  4. LUÔN TRẢ 202: dù trùng lặp cũng không trả lỗi, để bên gửi không retry vô ích.

KIẾN THỨC PYTHON / FASTAPI
  1. Đọc raw body (await request.body()) để verify HMAC - phải verify trên
     BYTES GỐC, vì parse rồi dump lại JSON sẽ ra chuỗi khác -> chữ ký sai.
  2. Path parameter với Enum -> FastAPI tự validate giá trị
  3. BackgroundTasks - chạy việc nhẹ sau khi đã trả response
"""

from __future__ import annotations

import json

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request, status

from app.api.deps import Config, IncidentSvc, rate_limit_webhook
from app.core.exceptions import UnauthorizedError, ValidationError
from app.core.logging import get_logger
from app.core.security import verify_signature
from app.domain.enums import AlertSource
from app.domain.normalizers import normalize
from app.schemas.incident import GenericAlertIn, IngestResponse

logger = get_logger(__name__)

# dependencies=[...] áp dụng cho MỌI endpoint trong router này.
# Rate limit là thứ bắt buộc ở đây nên khai báo ở cấp router, không thể quên.
router = APIRouter(
    prefix="/webhooks",
    tags=["webhooks"],
    dependencies=[Depends(rate_limit_webhook)],
)


@router.post(
    "/{source}",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Nhận alert từ hệ thống monitoring",
)
async def receive_webhook(
    source: AlertSource,
    request: Request,
    service: IncidentSvc,
    cfg: Config,
    x_signature: str | None = Header(default=None, alias="X-Signature"),
) -> IngestResponse:
    """
    Nhận webhook từ Prometheus / Datadog / Grafana / Zabbix.

    `source: AlertSource` là path param có kiểu Enum -> FastAPI tự trả 422
    nếu ai gọi /webhooks/abcxyz, và Swagger hiện sẵn dropdown các giá trị hợp lệ.

    Ví dụ gọi thử:
        curl -X POST http://localhost:8000/api/v1/webhooks/custom \
             -H "Content-Type: application/json" \
             -d '{"title":"High memory","service":"payment-api","severity":"critical"}'
    """
    # ĐỌC BYTES GỐC trước khi parse - bắt buộc để verify HMAC cho đúng.
    raw_body = await request.body()

    # Ở môi trường local cho phép bỏ qua chữ ký để dễ thử bằng curl.
    # Production thì BẮT BUỘC phải có.
    if cfg.is_production and not verify_signature(cfg.webhook_shared_secret, raw_body, x_signature):
        logger.warning("Webhook có chữ ký không hợp lệ", extra={"source": str(source)})
        raise UnauthorizedError("Chữ ký X-Signature không hợp lệ")

    try:
        payload = json.loads(raw_body or b"{}")
    except json.JSONDecodeError as err:
        raise ValidationError("Body không phải JSON hợp lệ") from err

    if not isinstance(payload, dict):
        raise ValidationError("Body phải là một JSON object")

    # normalize: dịch payload riêng của từng hãng -> Alert chuẩn hoá.
    alert = normalize(source, payload)

    # ingest: dedupe + ghi DB + audit + đẩy Kafka. Tất cả rất nhanh.
    result = await service.ingest(alert)

    return IngestResponse(
        incident_id=result.incident.id,
        fingerprint=result.incident.fingerprint,
        created=result.created,
        deduplicated=result.deduplicated,
        message=result.message,
    )


@router.post(
    "/custom/simple",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Gửi alert theo schema của mình (tiện để test)",
)
async def receive_simple(
    payload: GenericAlertIn,
    service: IncidentSvc,
    background: BackgroundTasks,
) -> IngestResponse:
    """
    Endpoint gọn cho script nội bộ - có schema rõ ràng nên Swagger dùng được ngay.

    Khác endpoint trên: ở đây FastAPI tự validate qua Pydantic (không cần
    normalizer), nên gõ sai field là biết ngay với thông báo cụ thể.

    BackgroundTasks: việc chạy SAU khi response đã gửi cho client. Phù hợp
    cho việc nhẹ và không quan trọng (ghi metric, log bổ sung).
    CẢNH BÁO: BackgroundTasks chạy TRONG process này. App restart là mất
    việc. Vì vậy remediation vẫn phải đi qua Kafka, không dùng cái này.
    """
    # model_dump() đổi Pydantic model -> dict để normalizer xử lý.
    alert = normalize(AlertSource.CUSTOM, payload.model_dump(mode="json"))
    result = await service.ingest(alert)

    background.add_task(
        logger.info,
        "Đã nhận alert đơn giản cho service %s",
        payload.service,
    )

    return IngestResponse(
        incident_id=result.incident.id,
        fingerprint=result.incident.fingerprint,
        created=result.created,
        deduplicated=result.deduplicated,
        message=result.message,
    )
