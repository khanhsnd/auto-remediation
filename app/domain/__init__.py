"""
app.domain - TRÁI TIM nghiệp vụ.

Nguyên tắc kiến trúc (Clean Architecture / Hexagonal):
    tầng domain KHÔNG import FastAPI, SQLAlchemy, Redis, Kafka.
    Nó chỉ chứa khái niệm nghiệp vụ thuần Python.

Lợi ích: test tầng này cực nhanh (không cần DB), và sau này đổi framework
hay đổi DB thì domain không phải sửa dòng nào.
"""
