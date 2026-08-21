"""
Cấu hình toàn hệ thống - đọc từ biến môi trường / file .env.

TRIẾT LÝ (12-factor app): code giống nhau ở mọi môi trường, chỉ khác config.
Vì vậy TUYỆT ĐỐI không hardcode host/password trong code.

KIẾN THỨC PYTHON XUẤT HIỆN Ở ĐÂY
  1. Type hints + Annotated  -> mô tả kiểu và ràng buộc ngay tại khai báo
  2. pydantic-settings       -> tự đọc env, tự ép kiểu, tự validate
  3. @lru_cache              -> biến hàm thành singleton (chỉ tạo 1 lần)
  4. @field_validator        -> kiểm tra logic tuỳ ý
  5. @computed_field         -> thuộc tính suy ra từ thuộc tính khác
  6. Enum                    -> tập giá trị hữu hạn, tránh so sánh chuỗi bừa
"""

from __future__ import annotations  # cho phép dùng type hint chưa định nghĩa

from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    """
    StrEnum (Python 3.11+): enum mà mỗi member CŨNG LÀ str.

    Nhờ vậy `Environment.LOCAL == "local"` -> True, in ra JSON cũng đẹp.
    Trước 3.11 phải viết `class Environment(str, Enum)`.
    """

    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """
    Toàn bộ config của app.

    Mỗi thuộc tính = 1 biến môi trường (không phân biệt hoa thường).
    Nếu env thiếu biến mà thuộc tính không có default -> app crash NGAY lúc
    khởi động. Đây là điều tốt: "fail fast", tốt hơn là chạy được nửa ngày
    rồi mới lỗi vì thiếu password.
    """

    # model_config: khai báo hành vi của pydantic-settings.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # env lạ thì bỏ qua, không báo lỗi
    )

    # ---------------------------------------------------------------- chung
    app_name: str = "auto-remediation"
    app_env: Environment = Environment.LOCAL
    app_debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = False

    # ------------------------------------------------------------- database
    database_url: str = "postgresql+asyncpg://remediation:remediation@localhost:5432/remediation"
    db_echo: bool = False

    # Annotated[type, Field(...)] = kiểu + ràng buộc.
    # ge = greater or equal, le = less or equal.
    db_pool_size: Annotated[int, Field(ge=1, le=100)] = 10
    db_max_overflow: Annotated[int, Field(ge=0, le=100)] = 5

    # ---------------------------------------------------------------- redis
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_seconds: Annotated[int, Field(ge=1)] = 60

    # ---------------------------------------------------------------- kafka
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_topic_incidents: str = "incidents.received"
    kafka_topic_remediation: str = "remediation.results"
    kafka_consumer_group: str = "remediation-worker"
    kafka_enabled: bool = True

    # ------------------------------------------------------------------ jwt
    jwt_secret: str = "doi-secret-nay-truoc-khi-len-production"
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_minutes: Annotated[int, Field(ge=1)] = 30
    jwt_refresh_ttl_days: Annotated[int, Field(ge=1)] = 7

    # ----------------------------------------------------------------- grpc
    grpc_agent_target: str = "localhost:50051"
    grpc_server_port: int = 50051
    grpc_timeout_seconds: Annotated[int, Field(ge=1)] = 15

    # -------------------------------------------------------------- webhook
    webhook_shared_secret: str = "secret-cho-ben-thu-3-ky-HMAC"
    rate_limit_per_minute: Annotated[int, Field(ge=1)] = 120

    # --------------------------------------------------------------- engine
    max_concurrent_remediations: Annotated[int, Field(ge=1, le=100)] = 5
    default_step_timeout: Annotated[int, Field(ge=1)] = 30
    dry_run: bool = False

    # ------------------------------------------------------------ validator
    @field_validator("database_url")
    @classmethod
    def _must_be_async_driver(cls, v: str) -> str:
        """
        field_validator chạy SAU khi pydantic ép kiểu xong.

        Lỗi kinh điển của người mới: khai báo `postgresql://...` rồi dùng
        AsyncEngine -> SQLAlchemy báo lỗi rất khó hiểu. Chặn sớm ở đây,
        kèm thông báo rõ ràng.
        """
        if "+asyncpg" not in v and "+aiosqlite" not in v:
            raise ValueError(
                "DATABASE_URL phải dùng driver async, ví dụ: "
                "postgresql+asyncpg://user:pass@host:5432/db"
            )
        return v

    @field_validator("jwt_secret")
    @classmethod
    def _secret_strength(cls, v: str) -> str:
        """
        HS256 dùng HMAC-SHA256, block size 32 byte. RFC 7518 mục 3.2 yêu cầu
        key dài tối thiểu bằng độ dài output của hàm băm (32 byte).

        Secret ngắn hơn thì brute-force khả thi - và pyjwt sẽ cảnh báo
        InsecureKeyLengthWarning. Chặn ngay lúc khởi động tốt hơn là để
        cảnh báo trôi trong log.
        """
        if len(v.encode()) < 32:
            raise ValueError(
                "JWT_SECRET cần tối thiểu 32 byte (RFC 7518). Sinh nhanh bằng: "
                'python -c "import secrets;print(secrets.token_urlsafe(48))"'
            )
        return v

    # --------------------------------------------------------- computed
    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        """
        @computed_field: thuộc tính tính ra từ thuộc tính khác, vẫn xuất hiện
        khi .model_dump(). Ở đây là "đường tắt" cho điều kiện hay dùng.
        """
        return self.app_env is Environment.PRODUCTION

    @computed_field  # type: ignore[prop-decorator]
    @property
    def kafka_servers_list(self) -> list[str]:
        """aiokafka nhận list, còn env chỉ là chuỗi "a:9092,b:9092"."""
        return [s.strip() for s in self.kafka_bootstrap_servers.split(",") if s.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Trả về Settings dạng singleton.

    @lru_cache ghi nhớ kết quả theo tham số đầu vào. Hàm này không có tham số
    nên chỉ có duy nhất 1 kết quả -> lần gọi thứ 2 trở đi lấy từ cache,
    không đọc lại file .env nữa.

    Đây cũng là "dependency" chuẩn của FastAPI:
        def handler(cfg: Settings = Depends(get_settings)): ...
    Trong test có thể ghi đè bằng `get_settings.cache_clear()` hoặc
    `app.dependency_overrides[get_settings] = ...`.
    """
    return Settings()


# Alias tiện dụng cho các module không đi qua DI của FastAPI (worker, script...).
settings = get_settings()
