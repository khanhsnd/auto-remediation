# syntax=docker/dockerfile:1
FROM golang:1.27.3-bookworm AS build
WORKDIR /src
ENV CGO_ENABLED=1
COPY go.mod go.sum ./
# Optional CA bundle for builds behind a trusted HTTPS proxy.
RUN --mount=type=secret,id=proxy_ca \
    --mount=type=cache,target=/go/pkg/mod \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    go mod download
COPY cmd/ ./cmd/
COPY internal/ ./internal/
RUN --mount=type=secret,id=proxy_ca \
    --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    go build -trimpath -ldflags='-s -w' -o /out/server ./cmd/server

FROM debian:bookworm-slim AS runtime
WORKDIR /app
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /out/server /app/server
RUN mkdir /data && chown 10001:10001 /data
ENV HTTP_ADDR=:8080 DATABASE_PATH=/data/messages.db
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/app/server"]
