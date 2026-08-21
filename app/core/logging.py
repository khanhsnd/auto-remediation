"""
Thiết lập logging.

BÀI HỌC QUAN TRỌNG: đừng dùng print(). print() không có level, không có
timestamp, không tắt được, không đẩy được sang file/ELK. Luôn dùng logging.

KIẾN THỨC PYTHON
  1. logging.Formatter tuỳ biến (kế thừa + override format())
  2. Dump JSON thủ công -> hiểu bản chất "structured logging"
  3. logging.Filter để nhồi thêm field vào mọi record
  4. dictConfig - cách cấu hình logging chuẩn, khai báo thay vì code lệnh
"""

from __future__ import annotations

import json
import logging
import logging.config
from typing import Any

from app.core.config import Settings
from app.core.context import get_actor, get_request_id
from app.utils.console import enable_utf8_output

# Các field mà logging tự tạo -> khi dump JSON phải loại ra để lấy được
# "extra" mà người dùng truyền vào. frozenset vì chỉ đọc, tra cứu O(1).
#
# CÁI BẪY QUAN TRỌNG: nếu truyền extra={"created": ...} - trùng một tên trong
# danh sách này - thì logging raise:
#     KeyError: "Attempt to overwrite 'created' in LogRecord"
# Và nó chỉ nổ khi log level ĐỦ THẤP để record được tạo thật. Ở test với level
# WARNING thì logger.info() thoát sớm, không lỗi gì; lên production với
# LOG_LEVEL=INFO là sập. Đây là lỗi thật project này đã gặp.
#
# Quy ước: tên key trong extra nên có tiền tố nghiệp vụ (incident_id, is_new,
# duration_ms...) để không bao giờ đụng vào danh sách dưới đây.
_RESERVED = frozenset(
    [
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    ]
)


class ContextFilter(logging.Filter):
    """
    Filter trong logging không chỉ để "lọc bỏ" - nó còn được dùng để
    LÀM GIÀU record (trick rất phổ biến).

    Trả về True = cho record đi tiếp.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Gắn thêm 2 field vào mọi dòng log, không cần sửa chỗ gọi logger.
        record.request_id = get_request_id()
        record.actor = get_actor()
        return True


class JsonFormatter(logging.Formatter):
    """
    In log thành JSON 1 dòng - dạng mà Loki/ELK/Datadog parse được ngay.

    Override format() là cách chuẩn để tự quyết định output.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "actor": getattr(record, "actor", "-"),
        }

        # Gom mọi field "lạ" -> chính là extra={...} người dùng truyền vào.
        # Dict comprehension: cách Pythonic để lọc/biến đổi dict.
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED}
        payload.update(extras)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # default=str để object lạ (datetime, UUID, Enum) không làm crash json.
        return json.dumps(payload, ensure_ascii=False, default=str)


class HumanFormatter(logging.Formatter):
    """Format dễ đọc cho lúc dev ở local."""

    _FMT = "%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s"

    def __init__(self) -> None:
        super().__init__(fmt=self._FMT, datefmt="%H:%M:%S")


def setup_logging(settings: Settings) -> None:
    """
    Cấu hình logging cho toàn app. Gọi 1 lần duy nhất khi khởi động.

    dictConfig ưu điểm hơn cách gọi lệnh: khai báo tường minh, dễ đọc,
    và reset sạch cấu hình cũ (disable_existing_loggers).
    """
    # Gọi ở đây là chỗ tốt nhất: mọi entrypoint (API, worker, gRPC, CLI) đều
    # gọi setup_logging, nên không nơi nào bị bỏ sót. Không có dòng này thì
    # log tiếng Việt trên Windows hiện thành đầ... rất khó đọc.
    enable_utf8_output()

    formatter = "json" if settings.log_json else "human"

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "context": {"()": ContextFilter},
            },
            "formatters": {
                "json": {"()": JsonFormatter},
                "human": {"()": HumanFormatter},
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": formatter,
                    "filters": ["context"],
                },
            },
            # root logger: mọi logger con không cấu hình riêng sẽ dùng cái này.
            "root": {"handlers": ["console"], "level": settings.log_level},
            "loggers": {
                # Hạ level mấy thư viện ồn ào để log của mình dễ đọc.
                "uvicorn.access": {"level": "WARNING", "propagate": True},
                "aiokafka": {"level": "WARNING", "propagate": True},
                "httpx": {"level": "WARNING", "propagate": True},
                # SQLAlchemy: bật DEBUG khi cần soi SQL thật
                "sqlalchemy.engine": {
                    "level": "INFO" if settings.db_echo else "WARNING",
                    "propagate": True,
                },
            },
        }
    )


def get_logger(name: str) -> logging.Logger:
    """
    Quy ước: mỗi module gọi `logger = get_logger(__name__)`.

    __name__ là đường dẫn module (vd "app.services.engine") -> nhìn log biết
    ngay code nào in ra, và bật/tắt log theo từng module rất dễ.
    """
    return logging.getLogger(name)
