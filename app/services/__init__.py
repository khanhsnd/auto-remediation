"""
app.services - tầng nghiệp vụ (use case).

Nhiệm vụ: điều phối domain + repository + infra để hoàn thành một tình huống
nghiệp vụ trọn vẹn ("nhận alert", "chạy remediation").

Nguyên tắc: tầng API chỉ nên gọi service, KHÔNG gọi repository trực tiếp.
Vì sao? Vì một use case thường gồm nhiều bước (ghi DB + cache + gửi Kafka +
audit). Nếu API tự lắp các bước đó, logic sẽ bị lặp ở mỗi endpoint.
"""
