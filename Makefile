# =============================================================================
# Makefile - gom các lệnh hay dùng lại cho gọn.
# Windows: cài "make" qua Chocolatey/Scoop, hoặc copy lệnh bên trong ra chạy tay.
# =============================================================================
.PHONY: help install up down proto api worker grpc seed test lint fmt type

help:            ## In danh sách lệnh
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

install:         ## Cài dependencies (Poetry)
	poetry install --extras all

up:              ## Bật Postgres + Redis + Kafka
	docker compose up -d

down:            ## Tắt hạ tầng
	docker compose down

proto:           ## Sinh code Python từ protos/*.proto
	python -m grpc_tools.protoc -I protos \
		--python_out=app/infra/grpc/generated \
		--pyi_out=app/infra/grpc/generated \
		--grpc_python_out=app/infra/grpc/generated \
		protos/agent.proto
	python scripts/fix_proto_imports.py

api:             ## Chạy web API (reload khi sửa code)
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

worker:          ## Chạy Kafka consumer worker
	python -m app.workers.remediation_worker

grpc:            ## Chạy gRPC agent giả lập (để test executor)
	python -m app.infra.grpc.server

seed:            ## Tạo bảng + nạp dữ liệu runbook mẫu
	python -m app.db.seed

test:            ## Chạy test
	pytest

lint:            ## Kiểm tra style
	ruff check app tests

fmt:             ## Tự sửa import + lỗi style đơn giản
	ruff check --fix app tests scripts
	@echo "Luu y: KHONG chay 'ruff format' o day."
	@echo "Ly do: project nay can can cot comment theo cot cho de doc, vd:"
	@echo "    RECEIVED = \"received\"        # vua nhan webhook"
	@echo "    REMEDIATING = \"remediating\"  # dang chay cac buoc"
	@echo "ruff format se don het ve 1 dau cach. Muon format chuan thi chay:"
	@echo "    ruff format app tests"

type:            ## Kiểm tra type hint
	mypy app
