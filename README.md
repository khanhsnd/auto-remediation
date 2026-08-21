# Auto-Remediation System

Hệ thống **tự phục hồi hạ tầng**: nhận lỗi từ bên thứ 3 → ghi nhận → check
runbook → tự xử lý. Không xử lý được thì bàn giao cho người.

Project này được viết để **học Python từ cơ bản tới nâng cao** trong một bài
toán thật, nên mọi file đều có comment giải thích *vì sao* làm như vậy, không
chỉ *làm gì*.

## Stack

| Thành phần | Thư viện | Dùng để |
|---|---|---|
| Web API | `fastapi` 0.136.3 + `uvicorn` 0.48.0 | nhận webhook, phục vụ dashboard |
| ORM | `sqlalchemy` 2.0.51 + `asyncpg` 0.31.0 | Postgres async |
| Cache / lock | `redis` 8.0.1 | dedupe, distributed lock, rate limit |
| Event queue | `aiokafka` 0.12.0 | tách API khỏi việc xử lý nặng |
| RPC nội bộ | `grpcio` 1.82.1 + `protobuf` 7.35.1 | gọi agent chạy trên node |
| HTTP client | `httpx` 0.28.1 | gọi K8s API, Jenkins, Slack |
| Auth | `pyjwt` 2.13.0 | JWT + RBAC 3 mức |

## Chạy thử trong 5 phút

```bash
# 1. Môi trường ảo + dependencies
python -m venv .venv
.venv\Scripts\activate            # Windows;  Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt

# 2. Cấu hình
cp .env.example .env               # Windows: copy .env.example .env
# Mở .env và ĐỔI JWT_SECRET trước khi dùng thật (tối thiểu 32 byte)

# 3. Hạ tầng: Postgres + Redis + Kafka
docker compose up -d

# 4. Sinh code gRPC từ file .proto
python -m grpc_tools.protoc -I protos --python_out=app/infra/grpc/generated \
  --pyi_out=app/infra/grpc/generated --grpc_python_out=app/infra/grpc/generated \
  protos/agent.proto
python scripts/fix_proto_imports.py

# 5. Tạo bảng + nạp runbook mẫu
python -m app.db.seed
```

Rồi mở **ba** cửa sổ terminal:

```bash
python -m app.infra.grpc.server            # agent giả lập (cửa sổ 1)
```

```bash
uvicorn app.main:app --reload               # web API (cửa sổ 2)
```

```bash
python -m app.workers.remediation_worker    # worker xử lý (cửa sổ 3)
```

Có `make` thì gọn hơn: `make up`, `make proto`, `make seed`, `make grpc`,
`make api`, `make worker`.

Swagger: <http://localhost:8000/docs>

## Bắn thử một alert

```bash
curl -X POST http://localhost:8000/api/v1/webhooks/prometheus \
  -H "Content-Type: application/json" \
  -d '{"alerts":[{"labels":{"alertname":"PodOOMKilled","severity":"critical","service":"payment-api","environment":"production"},"annotations":{"description":"OOMKilled 3 lan"},"startsAt":"2026-08-21T03:00:00Z"}]}'
```

API trả `202 Accepted` ngay lập tức. Nhìn sang cửa sổ worker sẽ thấy:

```
Xử lý event incident.received cho incident 26ffbf9f-...
Khớp runbook 'restart-oom-service' cho incident 26ffbf9f-... (trong 2 ứng viên)
Step 'Thông báo bắt đầu xử lý' OK (33ms)
Step 'Restart service qua agent' OK (2010ms)
Chờ 20.0s
Step 'Chờ service warm-up' OK (20004ms)
Step 'Xác minh health' OK (269ms)
Kết thúc remediation incident=26ffbf9f-... status=succeeded (4/4 step, 22455ms)
```

Xem lại lịch sử qua API:

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@local.dev","password":"admin12345"}' | jq -r .access_token)

curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/incidents/stats
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/incidents
```

Hoặc dùng CLI có sẵn:

```bash
python -m app.cli send-alert --service payment-api --alertname PodOOMKilled
python -m app.cli match --service payment-api --severity critical
python -m app.cli config
```

## Tài khoản mẫu (chỉ dùng ở local)

| Role | Email | Password | Quyền |
|---|---|---|---|
| `admin` | admin@local.dev | admin12345 | sửa runbook, quản lý user |
| `operator` | operator@local.dev | operator12345 | chạy runbook thủ công |
| `viewer` | viewer@local.dev | viewer12345 | chỉ xem |

## Kiểm tra chất lượng

```bash
pytest                       # 190 test
ruff check app tests         # lint
mypy app                     # type check (strict)
```

## Đọc tài liệu theo thứ tự này

1. **[docs/python-concepts.md](docs/python-concepts.md)** — bản đồ kiến thức
   Python: mỗi khái niệm (từ `Enum` tới `TaskGroup`, `Protocol`,
   `__init_subclass__`, `match/case`) trỏ tới đúng file dùng nó. **Bắt đầu từ đây.**
2. **[docs/architecture.md](docs/architecture.md)** — luồng xử lý, state machine,
   các chốt an toàn, và cái giá của từng quyết định thiết kế.
3. **[docs/runbook-dsl.md](docs/runbook-dsl.md)** — cách viết runbook: cú pháp
   `match_rules`, các loại step, rollback, checklist trước khi bật production.

## Cấu trúc project

```
app/
├── core/           config, logging, exceptions, security, contextvars
├── domain/         nghiệp vụ THUẦN Python - không import framework nào
│   ├── enums.py        Severity, IncidentStatus + bảng chuyển trạng thái
│   ├── alert.py        Alert chuẩn hoá + fingerprint chống trùng
│   ├── normalizers.py  dịch payload từng hãng -> Alert  (match/case)
│   └── rules.py        rule engine so khớp runbook
├── db/
│   ├── base.py         DeclarativeBase, mixin, enum_column, JSONB variant
│   ├── session.py      engine, session factory, dependency, session_scope
│   ├── models/         7 bảng: user, incident, runbook, step, execution, ...
│   ├── repositories/   Repository Pattern + generic BaseRepository[T]
│   └── seed.py         tạo bảng + nạp dữ liệu mẫu
├── infra/          cổng ra thế giới: Redis, Kafka, HTTP, gRPC
│   └── grpc/           client + agent giả lập + loader cho code sinh tự động
├── services/       use case
│   ├── incident_service.py     GHI NHẬN
│   ├── runbook_matcher.py      CHECK RUNBOOK
│   ├── remediation_engine.py   XỬ LÝ
│   └── executors/              Strategy Pattern, tự đăng ký vào registry
├── schemas/        DTO Pydantic (KHÔNG trả model ORM ra API)
├── api/            router, dependency, middleware, exception handler
├── workers/        consumer Kafka + reconcile loop
├── utils/          decorator (retry/timeout/singleflight), circuit breaker
├── cli.py          CLI với sub-command
└── main.py         application factory + lifespan

protos/agent.proto  hợp đồng gRPC giữa engine và agent
tests/              190 test, chạy trong ~12 giây (SQLite in-memory + fake)
docs/               ba tài liệu ở mục trên
```

## Vài chỗ đáng đọc kỹ

| Nếu anh muốn hiểu... | Mở file |
|---|---|
| Decorator có tham số, `ParamSpec`, backoff + jitter | `app/utils/decorators.py` |
| `ABC` + `__init_subclass__` làm registry tự động | `app/services/executors/base.py` |
| `match/case` bóc tách dict lồng nhau | `app/domain/normalizers.py` |
| Generic `class X[T]` (PEP 695) | `app/db/repositories/base.py` |
| `Protocol` để đảo ngược phụ thuộc | `app/infra/redis_client.py`, `app/services/executors/base.py` |
| `asyncio` TaskGroup / Semaphore / timeout | `app/services/remediation_engine.py` |
| Distributed lock + Lua script | `app/infra/redis_client.py` |
| State machine bảo vệ tính đúng đắn | `app/db/models/incident.py` |
| Dependency Injection của FastAPI | `app/api/deps.py` |
| Cách test API mà không cần chạy server | `tests/conftest.py` |

## Lưu ý về phiên bản thư viện

Các version trong `pyproject.toml` / `requirements.txt` được pin cứng theo đúng
danh sách yêu cầu và **đã cài + chạy thật** với Python 3.13 trên Windows:
fastapi 0.136.3, uvicorn 0.48.0, sqlalchemy 2.0.51, asyncpg 0.31.0, redis 8.0.1,
grpcio 1.82.1, protobuf 7.35.1, httpx 0.28.1, aiokafka 0.12.0, pyjwt 2.13.0.

Hai dependency phải thêm vì thư viện trên yêu cầu:
`pydantic-settings` (đọc config từ env) và `email-validator` (cho `EmailStr`).

`docker-compose.yml` dùng `apache/kafka:3.9.0`. Nhiều hướng dẫn cũ ghi
`bitnami/kafka:3.7` — tag đó đã bị xoá khỏi Docker Hub nên sẽ pull lỗi.

## Chưa có gì (nếu đưa lên production thật)

Xem mục cuối [docs/architecture.md](docs/architecture.md). Ngắn gọn: cần thêm
Alembic migration, mTLS cho gRPC, metrics/tracing, denylist để revoke JWT, và
viết agent thật (bản trong repo là giả lập).
