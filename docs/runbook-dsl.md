# Cách viết Runbook

Runbook là **dữ liệu**, không phải code. Sửa runbook = update một row trong DB,
không cần deploy lại.

Một runbook gồm hai phần:

1. `match_rules` - khi nào runbook này được dùng
2. `steps` - làm gì, theo thứ tự nào

---

## Phần 1: `match_rules`

### Cấu trúc

```json
{
  "all":  [ ... ],   // MỌI điều kiện phải đúng   (AND)
  "any":  [ ... ],   // ít nhất MỘT điều kiện đúng (OR)
  "none": [ ... ]    // KHÔNG điều kiện nào đúng   (NOT)
}
```

Cả ba khoá đều không bắt buộc. `match_rules` rỗng `{}` nghĩa là **khớp mọi
alert** - dùng để làm runbook lưới an toàn cuối cùng.

### Một điều kiện

```json
{"field": "labels.alertname", "op": "regex", "value": "(?i)memory"}
```

### Các `field` dùng được

| Field | Nội dung | Ví dụ |
|---|---|---|
| `service` | service bị ảnh hưởng | `payment-api` |
| `environment` | môi trường | `production` |
| `severity` | mức nghiêm trọng | `critical` |
| `severity_weight` | severity dạng số, để so ngưỡng | `4` (critical) ... `0` (info) |
| `source` | nguồn gửi alert | `prometheus` |
| `title` | tiêu đề alert | `PodOOMKilled` |
| `description` | mô tả chi tiết | |
| `status` | trạng thái incident | `received` |
| `attempt_count` | đã thử bao nhiêu lần | `0` |
| `labels.<tên>` | label bất kỳ từ alert gốc | `labels.pod`, `labels.namespace` |

Đường dẫn có dấu chấm đi xuyên vào dict con. Field không tồn tại trả `None`,
không gây lỗi.

### Các toán tử `op`

| `op` | Ý nghĩa | Ví dụ `value` |
|---|---|---|
| `eq` | bằng (chuỗi không phân biệt hoa/thường) | `"payment-api"` |
| `ne` | khác | `"staging"` |
| `in` | thuộc danh sách | `["critical", "high"]` |
| `not_in` | không thuộc danh sách | `["dev", "test"]` |
| `contains` | chứa chuỗi con | `"memory"` |
| `regex` | khớp biểu thức chính quy | `"(?i)^(oom\|memory)"` |
| `gt` / `gte` / `lt` / `lte` | so sánh số | `3` |
| `exists` | field có giá trị | (không cần `value`) |
| `not_exists` | field không có giá trị | (không cần `value`) |

Gõ sai `op` sẽ bị **từ chối ngay lúc tạo runbook** (HTTP 422), không phải đợi
tới lúc có sự cố mới phát hiện.

### Trọng số severity

| Severity | Weight |
|---|---|
| `critical` | 4 |
| `high` | 3 |
| `medium` | 2 |
| `low` | 1 |
| `info` | 0 |

Nhờ đó viết được `{"field": "severity_weight", "op": "gte", "value": 3}` thay vì
phải liệt kê `["critical", "high"]`.

### Ví dụ hoàn chỉnh

```json
{
  "all": [
    {"field": "severity_weight",     "op": "gte",   "value": 3},
    {"field": "environment",         "op": "eq",    "value": "production"},
    {"field": "labels.alertname",    "op": "regex", "value": "(?i)(oom|memory)"}
  ],
  "any": [
    {"field": "service", "op": "eq", "value": "payment-api"},
    {"field": "service", "op": "eq", "value": "order-api"}
  ],
  "none": [
    {"field": "labels.maintenance", "op": "exists"}
  ]
}
```

Đọc thành lời: *nghiêm trọng từ high trở lên, ở production, tên alert liên quan
tới memory, thuộc payment-api hoặc order-api, và KHÔNG đang trong cửa sổ bảo trì.*

### Khi nhiều runbook cùng khớp thì chọn cái nào?

Thứ tự xếp hạng (`app/services/runbook_matcher.py`):

1. `priority` nhỏ hơn thắng (số nhỏ = ưu tiên cao, giống `nice` của Linux)
2. Cùng `priority` -> luật **CỤ THỂ HƠN** thắng (nhiều điều kiện hơn)
3. Vẫn bằng -> theo tên, để kết quả **ổn định** (chạy lại luôn ra cùng đáp án)

Quy tắc 2 quan trọng: nó đảm bảo runbook catch-all (0 điều kiện) không giành
alert của runbook chuyên biệt.

### Thử luật trước khi bật

```bash
curl -X POST http://localhost:8000/api/v1/runbooks/test-match \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"service":"payment-api","severity":"critical","labels":{"alertname":"PodOOMKilled"}}'
```

Endpoint này **không chạy gì cả**, chỉ cho biết alert giả đó sẽ khớp runbook
nào. Luôn dùng nó trước khi bật runbook trên production - và nhớ kiểm tra cả
chiều ngược lại: runbook có **vô tình** khớp alert khác không.

---

## Phần 2: `steps`

Các step chạy **tuần tự** theo đúng thứ tự trong mảng. `order_index` được sinh
tự động, anh không phải tự đánh số.

### Thuộc tính chung của mọi step

| Thuộc tính | Mặc định | Ý nghĩa |
|---|---|---|
| `name` | (bắt buộc) | tên hiển thị trong lịch sử |
| `action_type` | (bắt buộc) | loại hành động, xem bảng dưới |
| `params` | `{}` | tham số riêng theo từng loại |
| `timeout_seconds` | 30 | quá hạn thì huỷ step |
| `retries` | 0 | số lần thử LẠI (0 = chỉ thử 1 lần) |
| `continue_on_failure` | false | true = lỗi vẫn chạy tiếp step sau |
| `rollback_params` | `{}` | cách hoàn tác nếu step SAU thất bại |

### Biến trong `params`

Mọi chuỗi trong `params` đều được thay biến trước khi chạy:

| Biến | Giá trị |
|---|---|
| `{service}` | service bị ảnh hưởng |
| `{environment}` | môi trường |
| `{severity}` | mức nghiêm trọng |
| `{incident_id}` | id incident |
| `{execution_id}` | id lượt chạy |
| `{attempt}` | lần thử thứ mấy |
| `{<tên label>}` | bất kỳ label của alert, vd `{pod}`, `{namespace}` |

Việc thay biến dùng `str.format_map`, **không** dùng `eval` - nên runbook trong
DB không thể trở thành lỗ hổng thực thi code.

### Các `action_type`

#### `grpc_command` - nhờ agent chạy lệnh trên node

```json
{
  "name": "Restart service",
  "action_type": "grpc_command",
  "params": {
    "action": "RESTART_SERVICE",
    "target": "{service}",
    "args": {"namespace": "{environment}"}
  },
  "timeout_seconds": 60,
  "retries": 1
}
```

`action` phải là một trong các giá trị đã khai trong `protos/agent.proto`:
`RESTART_SERVICE`, `SCALE_WORKLOAD`, `CLEAR_CACHE`, `ROTATE_LOG`, `RUN_SCRIPT`,
`KILL_PROCESS`. Sai tên -> lỗi cấu hình, agent từ chối.

#### `http_call` - gọi REST API

```json
{
  "name": "Scale deployment",
  "action_type": "http_call",
  "params": {
    "method": "PATCH",
    "url": "http://k8s-api/apis/apps/v1/namespaces/{environment}/deployments/{service}/scale",
    "json": {"spec": {"replicas": 6}},
    "headers": {"Authorization": "Bearer ..."},
    "expect_status": [200, 201],
    "service_name": "k8s-api"
  }
}
```

`service_name` dùng để đặt tên circuit breaker - mỗi dependency một cầu dao
riêng, K8s API sập không làm chặn luôn Slack.

#### `wait` - chờ warm-up

```json
{"name": "Chờ pod sẵn sàng", "action_type": "wait", "params": {"seconds": 30}}
```

Chặn trên 300 giây để runbook viết sai không treo cả worker.

#### `health_check` - XÁC MINH đã hồi phục

```json
{
  "name": "Xác minh health",
  "action_type": "health_check",
  "params": {
    "url": "http://{service}.{environment}.svc/healthz",
    "retries": 5,
    "interval": 5,
    "expect_status": 200
  },
  "timeout_seconds": 60
}
```

**Đây là step quan trọng nhất của một runbook.** Thiếu nó, hệ thống chỉ "làm gì
đó" rồi tự tuyên bố thành công - tệ hơn cả không làm gì, vì nó khiến người vận
hành mất niềm tin và bắt đầu bỏ qua alert thật.

#### `notify` - thông báo

```json
{
  "name": "Báo on-call",
  "action_type": "notify",
  "params": {
    "channel": "sre-oncall",
    "level": "warning",
    "message": "Đã tự restart {service} do OOM (lần {attempt})"
  },
  "continue_on_failure": true
}
```

Luôn đặt `continue_on_failure: true` cho step thông báo. Slack sập không được
làm hệ thống mất khả năng tự phục hồi.

#### `noop` - không làm gì

Dùng để test luồng, làm placeholder, hoặc làm runbook chỉ-ghi-nhận-rồi-escalate.

### Rollback

`rollback_params` mô tả cách hoàn tác. Khi một step SAU thất bại, engine chạy
rollback cho các step đã hoàn thành theo **thứ tự ngược**.

```json
{
  "name": "Scale up",
  "action_type": "grpc_command",
  "params":          {"action": "SCALE_WORKLOAD", "target": "{service}", "args": {"replicas": "6"}},
  "rollback_params": {"action": "SCALE_WORKLOAD", "target": "{service}", "args": {"replicas": "3"}}
}
```

Vì sao phải ngược thứ tự? Runbook làm `[scale up]` -> `[drain node]` ->
`[health check]`. Health check fail thì rollback phải `[undrain]` rồi mới
`[scale về cũ]`. Làm đúng thứ tự thuận sẽ để hệ thống ở trạng thái còn tệ hơn
ban đầu.

---

## Nhịp chuẩn của một runbook tốt

```
NOTIFY  ->  TÁC ĐỘNG  ->  WAIT  ->  HEALTH_CHECK  ->  NOTIFY
(báo)       (sửa)         (chờ)     (XÁC MINH)        (báo kết quả)
```

Thiếu `WAIT` thì health check chạy quá sớm, thấy service "chết" và engine tưởng
thất bại -> restart lại -> vòng lặp vô nghĩa.

Thiếu `HEALTH_CHECK` thì hệ thống nói dối về kết quả của chính nó.

## Runbook cho hành động nguy hiểm

Đặt `requires_approval: true`. Engine sẽ so khớp, chuyển incident sang
`TRIAGED`, rồi **dừng** chờ người bấm nút:

```bash
curl -X POST http://localhost:8000/api/v1/incidents/{id}/remediate \
  -H "Authorization: Bearer $OPERATOR_TOKEN" -d '{}'
```

Áp dụng cho mọi thứ chạm vào dữ liệu: kill connection DB, xoá volume, scale
down, rollback migration.

## Danh sách kiểm tra trước khi bật runbook trên production

- [ ] Đã gọi `/runbooks/test-match` và khớp ĐÚNG loại alert mong muốn
- [ ] Đã kiểm tra nó KHÔNG vô tình khớp alert khác
- [ ] Có step `health_check` để xác minh
- [ ] Step thông báo đặt `continue_on_failure: true`
- [ ] `max_attempts` hợp lý (thường 1-2)
- [ ] `cooldown_seconds` đủ dài để không lặp
- [ ] Hành động nguy hiểm thì `requires_approval: true`
- [ ] Đã chạy thử với `DRY_RUN=true`
- [ ] Có `rollback_params` cho các step thay đổi trạng thái
