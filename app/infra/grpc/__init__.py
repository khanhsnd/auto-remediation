"""
app.infra.grpc - giao tiếp với Agent chạy trên node hạ tầng.

Luồng: Engine (web app)  --gRPC-->  Agent (trên node)  --exec-->  systemctl/kubectl

Chạy `make proto` để sinh code từ protos/agent.proto vào thư mục generated/.
"""
