"""
Sửa lỗi in tiếng Việt ra terminal trên Windows.

VẤN ĐỀ THẬT: mặc định console Windows dùng bảng mã cp1252 (Tây Âu), không
có ký tự tiếng Việt. In "Đã tạo" ra là gặp:
    UnicodeEncodeError: 'charmap' codec can't encode character '\u0110'

Linux/macOS mặc định UTF-8 nên không gặp. Đây là loại bug chỉ xuất hiện trên
máy người khác - rất đáng biết.

CÁCH SỬA (Python 3.7+): reconfigure lại stream stdout/stderr sang UTF-8.
"""

from __future__ import annotations

import sys


def enable_utf8_output() -> None:
    """
    Bật UTF-8 cho stdout/stderr. Gọi ở đầu mọi script có in tiếng Việt.

    errors="replace" là lưới an toàn cuối: ký tự nào terminal vẫn không vẽ
    được thì thay bằng "?" chứ không làm chương trình chết.

    hasattr check vì stdout có thể đã bị thay bằng object khác (vd khi pytest
    capture output) và object đó không có method reconfigure.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
