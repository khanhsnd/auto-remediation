# Content → Telegram

Go API nhận nội dung, lưu SQLite kèm thời gian UTC, rồi gửi vào Telegram channel. SQLite đồng thời là hàng đợi bền vững: worker lấy bản ghi `pending`, gửi Telegram và cập nhật `sent`.

## Chạy local

Yêu cầu Go >= 1.24 và trình biên dịch C (GCC/Clang, `CGO_ENABLED=1`). Không cần Docker hoặc cài SQLite riêng.

```sh
cp .env.example .env
# Sửa .env theo hướng dẫn dưới đây
go mod download
go run ./cmd/server
```

Cấu hình `.env` (không commit file này):

| Biến | Ý nghĩa |
| --- | --- |
| `API_KEY` | Key ít nhất 32 byte; tạo bằng `openssl rand -hex 32` |
| `TELEGRAM_BOT_TOKEN` | Token bot lấy từ BotFather |
| `TELEGRAM_CHAT_ID` | `@channel_username` hoặc ID private channel dạng `-100...` |
| `HTTP_ADDR` | Mặc định `:8080` |
| `DATABASE_PATH` | Mặc định `./data/messages.db` |

Thêm bot làm admin của Telegram channel và cấp quyền đăng bài. Biến môi trường của tiến trình được ưu tiên hơn `.env`. App dừng ngay nếu thiếu key/token/channel. Không đưa key lên URL. Dùng HTTPS qua reverse proxy khi expose ra Internet.

## API

`POST /api/v1/messages`, header `X-API-Key`, JSON:

```sh
curl -X POST http://localhost:8080/api/v1/messages \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $API_KEY" \
  -d '{"content":"Xin chào từ Go"}'
```

Trong lệnh trên, `$API_KEY` là biến đã export trong shell; app tự đọc `.env` nhưng curl không tự đọc file đó.

Phản hồi `202 Accepted` sau khi lưu thành công:

```json
{"id":1,"created_at":"2026-10-28T03:00:00Z","status":"pending"}
```

`202` xác nhận đã lưu vào hàng đợi, chưa xác nhận Telegram đã nhận. Key sai/thiếu trả `401`; JSON lỗi, nội dung trống hoặc quá 4096 ký tự trả `400`. Body tối đa 32 KiB. Nội dung được gửi dưới dạng văn bản thuần.

`GET /health` trả `200` khi SQLite truy cập được; endpoint này không kiểm tra quyền bot Telegram.

## Lưu trữ và gửi lại

Bảng `messages` lưu `content`, `created_at`, `status`, `attempts`, `next_attempt_at`, `sent_at` và `telegram_message_id`. Thời gian dùng UTC RFC3339. Worker kiểm tra mỗi giây, gửi tuần tự, retry backoff từ 2 đến 512 giây khi lỗi. Tin chưa gửi vẫn còn sau khi khởi động lại. Lỗi token/quyền channel cũng được retry; sửa cấu hình rồi restart app.

Chạy **một instance** cho mỗi file SQLite. Cơ chế gửi ít nhất một lần: nếu Telegram nhận tin nhưng phản hồi bị mất, hoặc app dừng trước khi cập nhật SQLite, có thể gửi trùng. API không chống gửi trùng từ phía client. Sao lưu file DB cùng WAL đúng cách bằng công cụ SQLite backup; không công khai thư mục dữ liệu.

## Kiểm tra

```sh
go test -race ./...
go vet ./...
go build -o bin/server ./cmd/server
```

Test dùng SQLite thật trong thư mục tạm và HTTP server mô phỏng Telegram, không cần token thật. Gửi Telegram thật cần cấu hình `.env` hợp lệ.

## Deploy bằng Docker

```sh
# Lần đầu: bỏ qua lệnh copy nếu đã có .env
cp .env.example .env
chmod 600 .env
# Điền API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID trong .env

docker compose up -d --build
curl -f http://localhost:8080/health
docker compose logs -f app
```

Dockerfile build nhiều giai đoạn, bật CGO cho SQLite, chạy bằng user không phải root. `.env` và dữ liệu DB không được đưa vào image. Compose truyền các biến app cần từ `.env`, cố định DB trong `/data/messages.db` và lưu vào named volume `messages`. Chỉ chạy một replica. Đổi port ngoài bằng `APP_PORT=9000 docker compose up -d --build`.

Cập nhật app: `docker compose up -d --build`. Dừng: `docker compose down`. Named volume vẫn giữ dữ liệu; `docker compose down -v` sẽ xóa dữ liệu SQLite. Dùng Cloudflare Tunnel bên dưới hoặc đặt HTTPS reverse proxy trước API khi deploy Internet.

Nếu chỉ dùng Docker:

```sh
docker build -t content-telegram:local .
docker volume create content-telegram-data
docker run -d --name content-telegram --restart unless-stopped \
  --env-file .env -e HTTP_ADDR=:8080 -e DATABASE_PATH=/data/messages.db \
  -p 8080:8080 -v content-telegram-data:/data content-telegram:local
```

Với môi trường build qua HTTPS proxy có CA riêng, dùng BuildKit secret chứa CA bundle tin cậy: `docker build --secret id=proxy_ca,src=/path/to/ca-bundle.pem -t content-telegram:local .`. Không tắt kiểm tra TLS và không đưa token vào build arguments.


## Deploy qua Cloudflare Tunnel

1. Trong Cloudflare Zero Trust, vào **Networks → Tunnels**, tạo tunnel loại **Cloudflared** (remotely managed). Sao chép tunnel token vào `CLOUDFLARE_TUNNEL_TOKEN` trong `.env`. Giữ nguyên các key API/Telegram đã cấu hình.
2. Trong phần **Public Hostname / Published application route** của tunnel, chọn hostname của bạn, ví dụ `notify.example.com`, rồi cấu hình service **HTTP** với URL **`app:8080`** (đầy đủ là `http://app:8080`). Đây là tên service Docker; không dùng `localhost` vì connector chạy trong container riêng.
3. Khởi chạy:

```sh
docker compose --profile tunnel up -d --build
docker compose logs -f cloudflared
```

Gọi API qua hostname đã cấu hình:

```sh
curl https://notify.example.com/api/v1/messages \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $API_KEY" \
  -d '{"content":"Gửi qua Cloudflare Tunnel"}'
```

Tunnel cung cấp HTTPS công khai; không cần mở inbound port trên máy deploy. App vẫn kiểm tra `X-API-Key`. Port debug trên host chỉ bind `127.0.0.1` để kiểm tra local; truy cập từ xa đi qua tunnel. Chỉ container cloudflared nhận tunnel token; container app chỉ nhận key API/Telegram. `.env` không được nhúng vào image.

Cập nhật: `docker compose --profile tunnel up -d --build`. Dừng: `docker compose --profile tunnel down`. Tunnel cần outbound DNS và kết nối Cloudflare trên port 7844 (UDP/QUIC hoặc TCP/HTTP2); nếu firewall chặn UDP, đặt command cloudflared thành `["tunnel", "--no-autoupdate", "--protocol", "http2", "run"]` và cho phép TCP 7844. Chưa có tunnel token/hostname thật thì chỉ có thể kiểm tra cấu hình, chưa xác nhận được truy cập công khai.
