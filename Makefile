GO ?= go

.PHONY: run test build vet
run:
	$(GO) run ./cmd/server

test:
	$(GO) test -race ./...

build:
	$(GO) build -o bin/server ./cmd/server

vet:
	$(GO) vet ./...
