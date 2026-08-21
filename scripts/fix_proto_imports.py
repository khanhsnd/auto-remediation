"""
Sửa import trong code do protoc sinh ra.

VẤN ĐỀ ĐÃ BIẾT của protoc Python: file agent_pb2_grpc.py sinh ra dòng
    import agent_pb2 as agent__pb2
Đây là "implicit relative import" - kiểu import của Python 2, không còn hợp
lệ ở Python 3 khi file nằm trong package. Kết quả: ModuleNotFoundError.

Cách sửa: đổi thành import tuyệt đối theo package của mình.
    from app.infra.grpc.generated import agent_pb2 as agent__pb2

Chạy tự động sau mỗi lần `make proto`.

KIẾN THỨC PYTHON
  - pathlib.Path: xử lý đường dẫn hiện đại, chạy được cả Windows và Linux
  - re.sub với nhóm bắt (capturing group)
  - Kiểm tra idempotent: chạy lại nhiều lần vẫn cho kết quả đúng
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Path(__file__) là file này; .parent.parent là thư mục gốc project.
GENERATED_DIR = Path(__file__).resolve().parent.parent / "app" / "infra" / "grpc" / "generated"
PACKAGE = "app.infra.grpc.generated"

# r"..." = raw string, để dấu \ trong regex không bị Python hiểu là escape.
# (\w+) là nhóm bắt: phần tên module sẽ được tái sử dụng ở chuỗi thay thế.
PATTERN = re.compile(r"^import (\w+_pb2) as (\w+)$", re.MULTILINE)

# Windows console mặc định dùng cp1252 -> in tiếng Việt sẽ UnicodeEncodeError.
# Xem thêm giải thích trong app/utils/console.py
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def fix_file(path: Path) -> bool:
    """Trả True nếu file có thay đổi."""
    original = path.read_text(encoding="utf-8")

    # \1 \2 tham chiếu tới nhóm bắt trong PATTERN.
    fixed = PATTERN.sub(rf"from {PACKAGE} import \1 as \2", original)

    if fixed == original:
        return False  # đã sửa từ trước -> idempotent
    path.write_text(fixed, encoding="utf-8")
    return True


def main() -> int:
    """
    Trả về exit code: 0 = OK. Quy ước Unix, để dùng được trong CI/Makefile.
    """
    if not GENERATED_DIR.exists():
        print(f"[!] Không thấy {GENERATED_DIR}. Chạy 'make proto' trước.")
        return 1

    changed = [p.name for p in GENERATED_DIR.glob("*_pb2_grpc.py") if fix_file(p)]

    if changed:
        print(f"[+] Đã sửa import trong: {', '.join(changed)}")
    else:
        print("[=] Không có gì cần sửa.")
    return 0


# Thành ngữ Python: đoạn này chỉ chạy khi gọi trực tiếp `python scripts/...`,
# KHÔNG chạy khi file bị import từ nơi khác.
if __name__ == "__main__":
    sys.exit(main())
