"""
app.workers - các tiến trình chạy nền, KHÔNG phục vụ HTTP.

Vì sao tách khỏi web API? Vì hai loại tiến trình này scale theo tiêu chí
khác nhau: API scale theo số request, worker scale theo số incident. Deploy
riêng thì tuỳ chỉnh CPU/RAM/replica độc lập được.
"""
