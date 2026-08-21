# Bản đồ kiến thức Python trong project

Tài liệu này là **lộ trình đọc code**. Mỗi khái niệm đều trỏ tới file cụ thể
để anh mở ra xem nó được dùng thật trong ngữ cảnh nào, chứ không phải ví dụ
`foo/bar` khô khan.

Thứ tự dưới đây đi từ cơ bản tới nâng cao. Đọc theo thứ tự này sẽ thấy mạch.

---

## Tầng 1 - Nền tảng

| Khái niệm | Đọc ở đâu | Điểm cần chú ý |
|---|---|---|
| Package & `__init__.py` | `app/__init__.py`, `app/db/models/__init__.py` | vì sao phải import model trong `__init__` mới tạo được bảng |
| Type hints | rải khắp project | `Mapped[str]` vs `Mapped[str \| None]` quyết định NULL/NOT NULL |
| f-string | `app/domain/alert.py` | dùng trong generator để ghép chuỗi |
| Dict / list comprehension | `app/core/logging.py`, `app/db/base.py` | lọc và biến đổi trong một dòng |
| `try/except/finally` | `app/db/session.py` | `finally` đảm bảo luôn đóng tài nguyên |
| `with` statement | `app/db/session.py` | `session_scope()` tự commit/rollback |
| Exception tuỳ biến | `app/core/exceptions.py` | cây kế thừa để `except AppError` bắt được cả họ |
| `raise ... from err` | `app/core/security.py` | giữ nguyên nguyên nhân gốc trong traceback |
| Docstring | mọi module | quy ước: mô tả **vì sao**, không chỉ **cái gì** |

## Tầng 2 - Cấu trúc dữ liệu và OOP

| Khái niệm | Đọc ở đâu | Điểm cần chú ý |
|---|---|---|
| `Enum` / `StrEnum` | `app/domain/enums.py` | enum là class thật: thêm được `@property` và method |
| `IntEnum` + `auto()` | `app/domain/enums.py` (`Priority`) | so sánh và sắp xếp như số |
| `frozenset` | `app/domain/enums.py`, `app/core/logging.py` | tập bất biến, kiểm tra `in` là O(1) |
| `@dataclass` | `app/domain/alert.py`, `app/services/executors/base.py` | ba tham số `frozen` / `slots` / `kw_only` và lý do dùng |
| `field(default_factory=...)` | `app/domain/alert.py` | bẫy **mutable default argument** |
| `@property` | `app/db/models/execution.py`, `app/domain/enums.py` | thuộc tính tính toán, không phải cột DB |
| `@classmethod` làm constructor | `app/core/security.py` (`TokenPayload.from_claims`) | alternative constructor |
| Hàm thuần ngoài class | `app/domain/rules.py` | không cần state thì để ngoài cho dễ test |
| Mixin & đa kế thừa | `app/db/base.py` | `UUIDPrimaryKeyMixin`, `TimestampMixin` và thứ tự MRO |
| Encapsulation | `app/db/models/incident.py` (`transition_to`) | object tự bảo vệ tính đúng đắn của mình |
| Đa hình | `app/services/executors/` | engine gọi `executor.run()` mà không cần biết loại nào |
| `__repr__` | `app/db/base.py` và các model | repr tốt giúp debug nhanh gấp nhiều lần |

## Tầng 3 - Nâng cao

| Khái niệm | Đọc ở đâu | Điểm cần chú ý |
|---|---|---|
| `ABC` + `@abstractmethod` | `app/services/executors/base.py` | quên override là lỗi ngay lúc khởi tạo |
| `Protocol` (structural typing) | `app/infra/redis_client.py` (`CachePort`) | interface không cần kế thừa |
| `__init_subclass__` | `app/services/executors/base.py`, `app/db/repositories/base.py` | tự động đăng ký + kiểm tra class con, thay cho metaclass |
| `ClassVar` | `app/core/exceptions.py`, executors | phân biệt biến class và field instance |
| Generic (PEP 695) | `app/db/repositories/base.py` | cú pháp mới `class X[T]`, kèm cú pháp cũ `TypeVar` để so sánh |
| Generic model Pydantic | `app/schemas/common.py` (`Page[T]`) | một schema phân trang dùng cho mọi loại |
| `Self` | `app/core/security.py` | trả về chính class này |
| `Literal` | `app/utils/circuit_breaker.py` (`__aexit__`) | `Literal[False]` chống lỗi missing return statement |
| `Annotated` | `app/core/config.py`, `app/api/deps.py` | kiểu + ràng buộc, và type alias cho dependency |
| `cast()` | `app/db/repositories/base.py` | chỉ tác động type checker, runtime không đổi |
| `TYPE_CHECKING` | `app/db/models/incident.py` | import chỉ để type hint, tránh import vòng |
| Decorator có tham số | `app/utils/decorators.py` | ba tầng hàm: config -> nhận hàm -> wrapper |
| `functools.wraps` | `app/utils/decorators.py` | thiếu nó thì mọi hàm đều tên `wrapper` |
| `ParamSpec` + `TypeVar` | `app/utils/decorators.py` | giữ nguyên chữ ký hàm sau khi bọc decorator |
| `functools.lru_cache` | `app/core/config.py`, `app/domain/rules.py` | biến hàm thành singleton; cache regex đã compile |
| Closure | `app/api/deps.py` (`require`) | dependency factory nhớ biến của hàm ngoài |
| `contextvars` | `app/core/context.py` | correlation id an toàn với asyncio (khác `threading.local`) |
| `@asynccontextmanager` | `app/db/session.py`, `app/infra/redis_client.py` | biến async generator thành `async with` |
| `__aenter__` / `__aexit__` | `app/utils/circuit_breaker.py` | tự viết async context manager đầy đủ |
| Metaclass - vì sao **không** dùng | `app/services/executors/base.py` | `__init_subclass__` giải quyết 90% nhu cầu, dễ hiểu hơn nhiều |
| Class factory | `app/infra/grpc/server.py` | tạo class lúc runtime khi lớp cha chỉ có sau khi sinh code |
| `importlib` | `app/infra/grpc/stubs.py` | import động + đổi lỗi thành hướng dẫn cho người dùng |

## Tầng 4 - Structural pattern matching (`match` / `case`)

Đây là tính năng mạnh nhất mà nhiều người mới chỉ dùng như `switch`.

| Cách dùng | Đọc ở đâu |
|---|---|
| Khớp theo **kiểu**: `case datetime():` | `app/domain/normalizers.py` (`_parse_time`) |
| **Bóc tách** dict lồng nhau: `case {"alerts": [{"labels": dict()} as first, *_]}` | `app/domain/normalizers.py` |
| Guard: `case int() \| float() if value > 0:` | `app/domain/normalizers.py` |
| Toán tử hoặc: `case "critical" \| "fatal":` | `app/domain/normalizers.py` |
| Khớp tuple nhiều giá trị: `case (c, m) if c > 90:` | `app/infra/grpc/server.py` |
| Thông dịch một DSL | `app/domain/rules.py` (`evaluate_condition`) |
| `**rest` gom phần còn lại | `app/domain/normalizers.py` (`normalize_generic`) |
| Phân loại theo kiểu để đệ quy | `app/services/executors/base.py` (`render_params`) |

## Tầng 5 - Asyncio

| Khái niệm | Đọc ở đâu | Điểm cần chú ý |
|---|---|---|
| `async def` / `await` | khắp project | `time.sleep` chặn cả event loop, phải dùng `asyncio.sleep` |
| Async generator | `app/infra/kafka.py` (`consume`), `app/db/session.py` | `yield` trong hàm async |
| `asyncio.Semaphore` | `app/services/remediation_engine.py` | giới hạn việc song song, chống cạn connection pool |
| `asyncio.Lock` | `app/utils/circuit_breaker.py` | chống race condition khi sửa state |
| `asyncio.Event` | `app/workers/remediation_worker.py`, `app/infra/grpc/server.py` | cờ dừng cho graceful shutdown |
| `asyncio.timeout` | `app/services/remediation_engine.py`, `app/utils/decorators.py` | huỷ việc quá hạn (Python 3.11+) |
| `asyncio.TaskGroup` | `app/services/remediation_engine.py`, `app/workers/remediation_worker.py` | structured concurrency, hơn `gather` ở chỗ tự huỷ task còn lại |
| `asyncio.gather` | `app/api/v1/health.py` | `return_exceptions=True` để một lỗi không phá cả nhóm |
| `asyncio.create_task` | `app/utils/decorators.py` (`singleflight`) | gộp các lời gọi trùng nhau |
| `asyncio.wait_for` trên Event | `app/workers/remediation_worker.py` | ngủ N giây nhưng tỉnh ngay khi có tín hiệu |
| `asyncio.run` | `app/cli.py`, các entrypoint | chỉ gọi ở tầng ngoài cùng, không lồng nhau |
| Gộp nhiều context manager | `app/services/remediation_engine.py` | `async with (a, b as x):` |
| Xử lý signal | `app/workers/remediation_worker.py` | `add_signal_handler` không có trên Windows -> `contextlib.suppress` |

## Tầng 6 - Kỹ thuật hệ thống phân tán

| Kỹ thuật | Đọc ở đâu | Bài toán nó giải |
|---|---|---|
| Idempotency (`SET NX`) | `app/infra/redis_client.py` (`claim_once`) | webhook gửi lại nhiều lần |
| Upsert `ON CONFLICT DO NOTHING` | `app/db/repositories/incident.py` | race condition khi nhiều pod ghi cùng lúc |
| SQL theo dialect | `app/db/repositories/incident.py` (`_dialect_insert`) | cùng ý định, Postgres và SQLite khác cú pháp |
| Distributed lock | `app/infra/redis_client.py` (`distributed_lock`) | nhiều worker cùng xử lý một incident |
| Lua script cho nguyên tử | `app/infra/redis_client.py` | nhả lock an toàn, rate limit cửa sổ trượt |
| Sliding window rate limit | `app/infra/redis_client.py` | chống spam alert, không có lỗ hổng biên như fixed window |
| Exponential backoff + jitter | `app/utils/decorators.py` | chống retry storm |
| Circuit breaker | `app/utils/circuit_breaker.py` | chống sập lan (cascading failure) |
| Bulkhead | `app/utils/circuit_breaker.py` (`get_breaker`) | mỗi dependency một cầu dao riêng |
| Cache-aside | `app/infra/redis_client.py` (`get_or_set`) | giảm tải DB |
| Singleflight | `app/utils/decorators.py` | chống cache stampede |
| `SCAN` thay vì `KEYS` | `app/infra/redis_client.py` (`delete_pattern`) | `KEYS` làm đứng cả Redis |
| `FOR UPDATE SKIP LOCKED` | `app/db/repositories/incident.py` (`list_pending`) | biến Postgres thành work queue |
| Reconciliation loop | `app/workers/remediation_worker.py` | bù việc bị sót khi Kafka lỗi |
| Correlation ID | `app/core/context.py`, `app/api/middleware.py` | trace một request xuyên API -> Kafka -> worker |
| State machine | `app/domain/enums.py`, `app/db/models/incident.py` | chặn bước chuyển trạng thái vô lý |
| Cooldown | `app/services/runbook_matcher.py` | vòng lặp restart -> lỗi lại -> restart |
| Graceful shutdown | `app/workers/`, `app/infra/grpc/server.py` | không cắt ngang việc đang chạy khi deploy |
| Snapshot dữ liệu lịch sử | `app/db/models/execution.py` (`StepResult`) | sửa runbook không làm sai lịch sử cũ |
| Chống N+1 query | `app/db/repositories/runbook.py` (`selectinload`) | với async còn là điều kiện bắt buộc |
| `ensure_utc` | `app/db/base.py` | SQLite trả naive, Postgres trả aware |

## Tầng 7 - Design pattern & SOLID

| Pattern | Đọc ở đâu |
|---|---|
| Strategy | `app/services/executors/` - mỗi action một executor |
| Registry | `app/services/executors/base.py` - `Executor.registry` |
| Repository | `app/db/repositories/` |
| Unit of Work | commit ở service layer, xem `app/services/incident_service.py` |
| Dependency Injection | `app/api/deps.py` + constructor của mọi service |
| Adapter / Anti-Corruption Layer | `app/domain/normalizers.py`, `app/infra/grpc/client.py` |
| Template Method | `Executor.rollback()` - cha có hành vi mặc định |
| Factory | `get_executor()`, `create_app()`, `_build_servicer()` |
| Factory as fixture | `tests/conftest.py` (`auth_header`) |

**SOLID trong project này**

- **S**ingle responsibility - `process()` lo lock, `_process_locked()` lo nghiệp
  vụ, `_run_steps()` lo chạy step.
- **O**pen/closed - thêm `ActionType` mới chỉ cần thêm một file executor, không
  sửa dòng nào của code cũ.
- **L**iskov - mọi executor thay thế được cho nhau qua interface `run()`.
- **I**nterface segregation - `CachePort` và `PublisherPort` chỉ khai đúng vài
  method mà bên dùng thật sự cần.
- **D**ependency inversion - service nhận repository/publisher qua constructor,
  không tự `import` global.

## Tầng 8 - Testing

| Kỹ thuật | Đọc ở đâu |
|---|---|
| `conftest.py` và fixture | `tests/conftest.py` |
| Fixture có teardown (`yield`) | `tests/conftest.py` (`db`, `client`) |
| Fixture `autouse=True` | `tests/conftest.py` (`_enable_logging`) |
| `@pytest.mark.parametrize` | `tests/test_domain_rules.py` |
| `pytest.raises` + `match=` | `tests/test_security.py` |
| `monkeypatch` | `tests/conftest.py` (`fake_redis`) |
| **Patch ở nơi SỬ DỤNG, không phải nơi định nghĩa** | `tests/conftest.py` - giải thích chi tiết |
| Fake vs Mock | `tests/conftest.py` (`FakeRedis`) |
| SQLite in-memory thay Postgres | `tests/conftest.py` (`db`) + `app/db/base.py` (`with_variant`) |
| `dependency_overrides` của FastAPI | `tests/conftest.py` (`client`) |
| Test API qua ASGI, không cần server | `tests/conftest.py` (`httpx.ASGITransport`) |
| Structural typing trong test | `tests/test_executors.py` (`FakePublisher`) |
| `nonlocal` trong test closure | `tests/test_utils.py` |
| Test bảo mật (user enumeration) | `tests/test_api.py` (`test_thong_bao_loi_giong_nhau`) |
| `model_copy(update=...)` để đổi settings | `tests/test_remediation_engine.py` |

---

## Sáu lỗi mà chính project này đã gặp trong lúc xây

Đây là phần đáng giá nhất - lỗi thật, không phải lý thuyết.

### 1. Enum đọc từ DB trả về `str`

Khai `Mapped[IncidentStatus] = mapped_column(String(32))` thì ghi xuống đúng
nhưng đọc lên là chuỗi thô, nên `incident.status.is_terminal` nổ
`AttributeError`. Bug chỉ lộ ra khi **đọc lại từ DB**, nên test kiểu "tạo
object rồi assert" hoàn toàn không bắt được.

Giải pháp: hàm `enum_column()` trong `app/db/base.py`.

### 2. JWT secret dưới 32 byte

pyjwt cảnh báo `InsecureKeyLengthWarning` (RFC 7518 mục 3.2). Cảnh báo rất dễ
bị trôi giữa hàng nghìn dòng log.

Giải pháp: validator trong `app/core/config.py` chặn ngay lúc khởi động.

### 3. `import app.services.executors` bên trong `app/main.py`

Dòng đó gán lại tên `app` thành module, che mất biến `app` (FastAPI) của chính
file. mypy bắt được, mắt người thường bỏ qua.

Giải pháp: import trực tiếp symbol - `from app.services.executors import Executor`.

### 4. Fake trong test trả về sai kiểu dữ liệu

`FakeRedis.eval()` ban đầu trả về số `1`, nhưng script Lua rate-limit thật trả
về một mảng `{allowed, count}`. Code production làm `result[0]` nên nổ
`TypeError: 'int' object is not subscriptable` - và chỉ nổ ở endpoint có rate
limit, tức là test API mới chạm tới.

Bài học: khi viết fake, phải fake **đúng kiểu trả về**, không chỉ "trả về cái
gì đó cho nó chạy". Fake sai kiểu còn tệ hơn không có fake, vì nó tạo cảm giác
an toàn giả.

### 5. `extra={"created": ...}` khi ghi log

`created` là một field nội bộ của `LogRecord`. Truyền nó qua `extra` làm
logging raise `KeyError: "Attempt to overwrite 'created' in LogRecord"`.

Điểm nguy hiểm: nó **chỉ nổ khi log level đủ thấp** để record được tạo thật.
Test chạy ở level WARNING thì `logger.info()` thoát sớm, không lỗi gì. Lên
production với `LOG_LEVEL=INFO` là sập ngay request đầu tiên.

Giải pháp: đổi tên key, và thêm fixture `autouse` bật logging INFO trong test
để mọi lời gọi log đều được chạy thật.

### 6. SQLite trả `datetime` naive, Postgres trả aware

Cột khai `DateTime(timezone=True)` nhưng khi đọc lên:

- Postgres -> có `tzinfo`
- SQLite -> **không** có `tzinfo`

Trừ hai datetime lệch loại thì Python raise
`TypeError: can't subtract offset-naive and offset-aware datetimes`.

Nghĩa là code chạy tốt trên production nhưng vỡ trong test, hoặc ngược lại,
tuỳ hướng nào được viết trước.

Giải pháp: `ensure_utc()` trong `app/db/base.py`, gọi trước mọi phép tính thời
gian lấy từ DB.

---

## Tổng kết: ai bắt được lỗi nào

| Lỗi | Ai bắt được | Chỉ lộ ra khi... |
|---|---|---|
| Enum về thành `str` | test tích hợp | đọc lại từ DB |
| JWT secret ngắn | cảnh báo pyjwt trong test | dùng HS256 |
| `import app...` che biến `app` | mypy | (không lộ ra lúc chạy) |
| Fake trả sai kiểu dữ liệu | test API | chạy đúng nhánh code đó |
| `extra={"created": ...}` | chạy thật end-to-end | `LOG_LEVEL=INFO` |
| datetime naive vs aware | test cooldown | chạy trên SQLite |

Không lỗi nào trong số này được tìm ra bằng cách **đọc lại code**. Đó chính là
lý do project bật `mypy --strict`, có bộ test đầy đủ, và luôn chạy thử thật
trước khi tin là xong.
