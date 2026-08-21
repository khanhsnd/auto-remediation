"""
app.infra - "cổng ra" tới thế giới bên ngoài: Redis, Kafka, HTTP, gRPC.

Trong Clean Architecture đây là tầng ngoài cùng (adapter). Domain và service
không biết Redis tồn tại; chúng chỉ biết interface (Protocol) mà tầng này
hiện thực hoá. Nhờ đó test được mà không cần bật Redis thật.
"""
