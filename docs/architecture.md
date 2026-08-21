# Kiến trúc hệ thống

## Luồng chính

```
   ┌──────────────┐
   │ Bên thứ 3    │  Prometheus / Datadog / Grafana / Zabbix
   │ (monitoring) │
   └──────┬───────┘
          │  POST /api/v1/webhooks/{source}   (kèm chữ ký HMAC)
          ▼
   ┌──────────────────────────────────────────────────┐
   │ 1. GHI NHẬN  (app/api/v1/webhooks.py)            │
   │   - rate limit (Redis sliding window)            │
   │   - verify chữ ký HMAC                           │
   │   - normalize -> Alert chuẩn hoá                 │
   │   - fingerprint -> dedupe (Redis SET NX)         │
   │   - upsert Postgres (ON CONFLICT DO NOTHING)     │
   │   - ghi audit log                                │
   │   - publish Kafka  ->  trả 202 Accepted          │
   └──────┬───────────────────────────────────────────┘
          │  topic: incidents.received
          ▼
   ┌──────────────────────────────────────────────────┐
   │ 2. WORKER  (app/workers/remediation_worker.py)   │
   │   - consume Kafka (at-least-once, commit tay)    │
   │   - distributed lock theo incident_id            │
   │   - song song: reconcile loop quét DB bù việc sót│
   └──────┬───────────────────────────────────────────┘
          ▼
   ┌──────────────────────────────────────────────────┐
   │ 3. CHECK RUNBOOK  (services/runbook_matcher.py)  │
   │   - rule engine so khớp match_rules (JSONB)      │
   │   - xếp hạng: priority -> độ cụ thể -> tên       │
   │   - kiểm tra max_attempts, requires_approval     │
   │   - không khớp -> ESCALATE cho người             │
   └──────┬───────────────────────────────────────────┘
          ▼
   ┌──────────────────────────────────────────────────┐
   │ 4. XỬ LÝ  (services/remediation_engine.py)       │
   │   chạy tuần tự từng step, mỗi step có            │
   │   timeout + retry riêng:                         │
   │                                                  │
   │     NOTIFY  --> Kafka --> Slack/Telegram         │
   │     GRPC    --> Agent trên node --> systemctl    │
   │     HTTP    --> K8s API / Jenkins / Cloud API    │
   │     WAIT    --> chờ service warm-up              │
   │     HEALTH  --> XÁC MINH đã sống lại chưa        │
   │                                                  │
   │   lỗi -> rollback theo thứ tự NGƯỢC              │
   └──────┬───────────────────────────────────────────┘
          ▼
   RESOLVED  |  FAILED -> retry  |  ESCALATED -> gọi người
```

## Vòng đời incident (state machine)

```
                    ┌─────────────┐
                    │  RECEIVED   │  vừa ghi vào DB
                    └──┬───┬───┬──┘
       ┌───────────────┘   │   └───────────────┐
       ▼                   ▼                   ▼
┌─────────────┐     ┌──────────────┐    ┌─────────────┐
│ SUPPRESSED  │     │   TRIAGED    │    │  ESCALATED  │
│ (bảo trì)   │     │ đã có runbook│    │ (gọi người) │
└─────────────┘     └──────┬───────┘    └─────────────┘
  [kết thúc]               ▼                [kết thúc]
                    ┌─────────────┐
                    │ REMEDIATING │  đang chạy step
                    └──┬───────┬──┘
                       ▼       ▼
              ┌─────────────┐ ┌─────────────┐
              │  RESOLVED   │ │   FAILED    │
              └─────────────┘ └──────┬──────┘
                [kết thúc]           │ còn lượt thử
                                     └──► REMEDIATING
```

Bảng chuyển hợp lệ nằm ở `ALLOWED_TRANSITIONS` trong `app/domain/enums.py`.
`Incident.transition_to()` tra bảng này trước khi đổi, nên không ai nhảy tắt
từ RECEIVED sang RESOLVED được.

## Phân tầng (Clean Architecture)

```
   api/          <- HTTP: nhận/trả dữ liệu, KHÔNG chứa nghiệp vụ
     |
   services/     <- use case: điều phối các bước của một nghiệp vụ
     |
   domain/       <- khái niệm nghiệp vụ thuần Python (KHÔNG import framework)
     ^
   db/  infra/   <- adapter: Postgres, Redis, Kafka, HTTP, gRPC
```

Quy tắc phụ thuộc: mũi tên chỉ đi **vào trong**. `domain/` không import
FastAPI/SQLAlchemy/Redis dòng nào, nhờ vậy test tầng này chạy trong vài
milliseconds và đổi framework không phải sửa nghiệp vụ.

## Vì sao tách API và Worker thành hai tiến trình?

| | API | Worker |
|---|---|---|
| Nhiệm vụ | ghi nhận alert, phục vụ dashboard | thực sự chạy runbook |
| Yêu cầu độ trễ | phải dưới ~50ms (bên gửi sẽ timeout) | có thể mất vài phút |
| Scale theo | số request/giây | số incident đồng thời |
| Chết thì sao? | mất alert mới | việc đang chạy dở; đã có reconcile loop bù |

Nếu gộp làm một: một sự cố lớn khiến worker chiếm hết tài nguyên, API không
nhận được alert mới nữa - đúng lúc cần nó nhất.

## Chống trùng lặp: ba lớp bảo vệ

Prometheus gửi lại cùng một alert mỗi 30 giây trong suốt sự cố. Không chặn thì
DB có hàng trăm bản ghi và hệ thống restart service hàng trăm lần.

1. **Redis `SET NX`** (`claim_once`) - nhanh nhất, chặn phần lớn trường hợp, TTL 5 phút.
2. **Unique index trên `fingerprint`** + `ON CONFLICT DO NOTHING` - lưới an toàn
   ở tầng DB, chống được cả race condition giữa nhiều pod.
3. **Distributed lock theo `incident_id`** - chặn hai worker cùng xử lý một incident.

Ba lớp cho ba loại lỗi khác nhau. Đây là "defense in depth", không phải làm dư.

## Các chốt an toàn (quan trọng nhất của hệ thống loại này)

| Chốt | Chặn điều gì | Ở đâu |
|---|---|---|
| `max_attempts` | retry vô hạn khi nguyên nhân gốc chưa được sửa | `runbook_matcher.py` |
| `cooldown_seconds` | restart -> lỗi lại -> restart -> vòng lặp tai hoạ | model `Runbook` |
| `requires_approval` | tự động làm việc nguy hiểm (chạm DB, xoá dữ liệu) | `remediation_engine.py` |
| `DRY_RUN` | thử runbook mới trực tiếp trên production | mọi executor |
| Circuit breaker | gọi mãi vào dependency đã sập, kéo mình sập theo | `utils/circuit_breaker.py` |
| Rate limit | monitoring cấu hình sai bắn 10.000 alert/phút | `api/deps.py` |
| Rollback ngược thứ tự | để hệ thống ở trạng thái nửa vời sau khi lỗi | `remediation_engine.py` |
| `HEALTH_CHECK` bắt buộc | tự tuyên bố "đã sửa xong" mà thực tế vẫn chết | executor |
| `find_flapping()` | tiếp tục tự động khi service lỗi lặp lại liên tục | `repositories/incident.py` |
| Audit log | không trả lời được "3h sáng ai/cái gì đã làm việc đó" | mọi service |

## Quyết định thiết kế và cái giá phải trả

**Runbook lưu trong DB (JSONB) thay vì viết trong code**
Được: sửa luật không cần deploy; người vận hành tự thêm được.
Mất: không có type safety lúc compile, nên phải validate rất chặt lúc tạo -
xem `app/schemas/runbook.py`, nó chạy THẬT rule engine với dữ liệu mẫu.

**gRPC agent thay vì SSH**
Được: agent chỉ nhận đúng các action đã khai trong `.proto` (whitelist by
design); engine không cần private key hay quyền root ở đâu cả; audit tập trung.
Mất: phải deploy thêm một binary lên từng node.

**Kafka thay vì gọi trực tiếp**
Được: worker chết vẫn không mất việc; scale worker theo chiều ngang; API trả
lời nhanh.
Mất: thêm một thành phần hạ tầng phải vận hành; và ngữ nghĩa at-least-once
buộc mọi thứ phải idempotent.

**UUID thay vì auto-increment id**
Được: sinh được id ở phía app trước khi insert; không tiết lộ số lượng bản ghi.
Mất: nặng hơn int, index kém locality hơn.

**Escalate thay vì cố sửa tới cùng**
Đây là quyết định quan trọng nhất. Một hệ thống tự động **tốt** phải biết giới
hạn của mình và gọi người. Cố mãi chỉ làm sự cố lan rộng hơn.

## Những gì project này CHƯA có (nếu đưa lên production thật)

Nói rõ để anh không tưởng đây là bản hoàn chỉnh:

- **Alembic migration** - hiện dùng `create_all()`, chỉ tạo được bảng mới,
  không sửa được bảng đã tồn tại. Production bắt buộc phải có Alembic.
- **mTLS cho gRPC** - đang dùng `insecure_channel`. Agent có quyền tác động hạ
  tầng nên thực tế phải xác thực hai chiều.
- **Metrics / tracing** - chưa có Prometheus exporter hay OpenTelemetry.
- **Revoke JWT** - token đã có `jti` nhưng chưa có denylist trong Redis.
- **Agent thật** - `app/infra/grpc/server.py` là bản GIẢ LẬP (ngủ vài giây,
  thất bại ngẫu nhiên 15%) để chạy thử luồng ở local.
