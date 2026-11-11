# Lớp xử lý — service workload tổng quát

Hệ thống nghiên cứu cân bằng tải phân tán và AIOps, chưa gắn với nghiệp vụ cụ thể. Mọi instance chạy cùng một implementation `ProcessingService.Execute`; service ID, topology và đặc tính xử lý lấy từ JSON. Các RPC Order/Payment/Inventory trước đây đã được thay bằng API tổng quát; client thử nghiệm cần dùng proto mới `api/processing.proto`.

Tài liệu này mô tả stack cơ sở với static dependency pools. Đã bổ sung chế độ `DISCOVERY_MODE=xds` và overlay riêng cho [lookaside động](lookaside.md); ở chế độ đó endpoint nội bộ do control plane cung cấp, không lấy từ static pool.

## Topology mẫu

```text
Client → Envoy :50050 → Service A (a-1 / a-2)
                              ├──→ Service B (b-1 / b-2)
                              └──→ Service C (c-1 / c-2)
```

A/B/C chỉ là tên ví dụ. Có thể đổi tên, thêm service D, tạo chuỗi nhiều tầng hoặc cấu hình A gọi B/C song song mà không sửa implementation xử lý. Mỗi instance là một process/container riêng, giao tiếp bằng gRPC thật.

## Cấu hình

- `configs/topology.local.json`: endpoint loopback, phục vụ launcher nhiều process.
- `configs/topology.compose.json`: endpoint DNS của container trong stack Compose.

Ví dụ cấu hình một service trong trường `services`:

```json
"service-a": {
  "instances": {"a-1": "127.0.0.1:50105", "a-2": "127.0.0.1:50106"},
  "dependencies": ["service-b", "service-c"],
  "mode": "parallel",
  "delay_s": 0.005,
  "concurrency": 50,
  "timeout_s": 5,
  "heavy_multiplier": 5,
  "failure_rate": 0
}
```

| Trường | Ý nghĩa |
|---|---|
| `entry_service` | Service đầu vào, khai báo ở cấp gốc cùng `schema_version: 1` |
| `instances` | Instance ID → địa chỉ truy cập từ caller |
| `dependencies` | Các service được gọi, mặc định không có |
| `mode` | `sequential` gọi lần lượt theo thứ tự cấu hình; `parallel` gọi đồng thời |
| `delay_s` | Thời gian xử lý mô phỏng của workload nhẹ, mặc định 0.005 giây |
| `heavy_multiplier` | Hệ số nhân độ trễ cho workload nặng, mặc định 5 |
| `concurrency` | Số tác vụ xử lý đồng thời, mặc định 50 |
| `timeout_s` | Ngân sách xử lý tối đa của service, mặc định 5 giây |
| `failure_rate` | Xác suất lỗi mô phỏng mỗi lần xử lý, từ 0 đến 1; mặc định 0 |

Mô phỏng nhẹ/nặng dùng thời gian chờ bất đồng bộ, chưa tạo CPU load thật. Tải đến được tạo bởi client/load generator. `failure_rate: 1` gây lỗi có chủ đích để thí nghiệm; không có retry tự động.

Topology phải là DAG: kiểm tra dependency tồn tại, không có chu trình, instance ID/endpoint không trùng và các tham số hợp lệ trước khi mở server. Một request gọi tất cả dependency đã cấu hình; chưa hỗ trợ rẽ nhánh có điều kiện.

## Chạy không cần Docker

Từ thư mục gốc, cài môi trường theo README rồi chạy:

```powershell
.\.venv\Scripts\python.exe scripts/run_microservices.py --check
```

Launcher đọc topology, khởi động các dependency trước caller, chờ health theo service ID, kiểm tra từng instance đầu vào rồi dừng process con. Mẫu mặc định chạy 6 process, gửi 20 request tới từng instance A. Mỗi instance B/C phải nhận 10 request từ mỗi A.

Giữ service chạy để thử từ terminal khác:

```powershell
.\.venv\Scripts\python.exe scripts/run_microservices.py
.\.venv\Scripts\python.exe scripts/check_microservices.py --address localhost:50105 --expected service-a=a-1
```

Lệnh đầu giữ terminal đến khi Ctrl+C; lệnh sau chạy trong terminal khác. Thêm `--workload heavy` để thử workload nặng. Cả hai script hỗ trợ `--topology PATH` để chọn topology khác. Thay cổng trong JSON khi có xung đột; cờ `--base-port` cũ đã được thay bằng cấu hình này.

Logs ở `results/microservices/process-N.log`. Launcher Windows terminate các process con khi dừng; container nhận SIGTERM hỗ trợ drain.

## Chạy qua Envoy

Stack này dùng cổng 50050/9901 như stack ingress cũ; dừng stack cũ nếu đang giữ cổng.

```powershell
docker compose -f deploy/compose.microservices.yaml config --quiet
docker compose -f deploy/compose.microservices.yaml run --rm --no-deps ingress --mode validate -c /etc/envoy/envoy.yaml
docker compose -f deploy/compose.microservices.yaml up --build -d
Invoke-RestMethod http://localhost:9901/clusters
.\.venv\Scripts\python.exe scripts/check_microservices.py
```

Chờ hai A endpoint healthy trước khi chạy checker. Checker dùng một channel đến Envoy, kiểm tra cây kết quả đúng topology và các instance mong đợi đều có tham gia. Backend không publish port ra host.

Kiểm tra B mất một instance:

```powershell
docker compose -f deploy/compose.microservices.yaml stop b-2
```

Chờ vài chu kỳ health rồi chạy:

```powershell
.\.venv\Scripts\python.exe scripts/check_microservices.py --expected service-b=b-1
docker compose -f deploy/compose.microservices.yaml start b-2
```

Chờ phục hồi, chạy checker mặc định. Khi xong:

```powershell
docker compose -f deploy/compose.microservices.yaml down
```

Cấu hình service là tổng quát, nhưng manifest triển khai vẫn phải khớp topology. Khi đổi service đầu vào: cập nhật endpoint và health name trong `deploy/envoy.microservices.yaml`. Khi thêm instance/container: cập nhật Compose và topology Compose. Launcher local tự đọc số service/instance từ JSON.

## Cấu hình tiến trình và nhiều node

Entrypoint: `python -m grpc_lb.microservices`.

| Biến môi trường | Ý nghĩa |
|---|---|
| `TOPOLOGY_PATH` | File topology, mặc định `configs/topology.local.json` |
| `SERVICE_ID` | Service chạy trong process; mặc định entry service trong JSON |
| `INSTANCE_ID` | Instance được khai báo trong service; mặc định instance đầu tiên |
| `LISTEN_ADDRESS` | Ghi đè địa chỉ bind; mặc định endpoint của instance trong JSON |

Đặc tính workload lấy từ JSON; các biến môi trường `SERVICE_ROLE`, `INVENTORY_ENDPOINTS`, `PAYMENT_ENDPOINTS`, `SERVICE_DELAY_S` của implementation trước không còn điều khiển module này. WorkloadService của M1 giữ nguyên giao diện cũ.

Để triển khai nhiều máy, dùng endpoint IP/DNS có thể truy cập từ caller và bind đúng network interface. `localhost` chỉ dùng cho instance cùng máy. Bản phát triển dùng plaintext gRPC; chưa có TLS/mTLS, HA hoặc nghiệm thu đa host.

## Chịu lỗi và giới hạn

- Mỗi caller có pool Round Robin độc lập theo service ID và nhận health Watch đúng tên `processing.v1.ProcessingService/<service-id>`. Instance lỗi bị tránh và được nhận lại khi probe thành công.
- Chưa có lookaside/xDS: endpoint tĩnh được nạp khi khởi động. Sửa JSON cần khởi động lại process; bước lookaside sẽ thay cơ chế này bằng cập nhật động.
- Deadline chung bao gồm chờ slot, xử lý tại chỗ và các dependency. Mỗi chặng chỉ nhận ngân sách còn lại; timeout cấu hình áp dụng cả leaf service.
- Lỗi dependency làm request cha thất bại. Với mode parallel, hủy và chờ kết thúc các nhánh còn lại khi một nhánh lỗi hoặc caller hủy.
- Request mang visited service IDs và hop budget (1–64) để tránh gọi vòng/vô hạn. Checker mặc định gửi 64; client tự viết phải cấp giá trị này.
- Admission của riêng Execute giới hạn tổng tác vụ đang xử lý/chờ ở mức hai lần concurrency; health check không dùng chung hạn mức này. Giải phóng slot khi hủy/lỗi. Health của process không đại diện toàn bộ dependency.
- Log JSON chứa service ID, instance ID, request ID, workload, status và duration. Chưa có distributed tracing/OpenTelemetry đầy đủ.

## Kiểm chứng

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_microservices.py -q
.\.venv\Scripts\python.exe -m pytest -q
```

Integration tests dùng tên service tùy ý, gRPC thật và topology nhiều tầng; kiểm tra tuần tự/song song, phân phối replica, health phục hồi, dependency chết, deadline, hủy nhánh song song, hop/cycle, profile nặng và lỗi mô phỏng.

Đã chạy launcher 6 process, toàn bộ **81 test đạt** và đã kiểm thử toàn tuyến với Envoy/Docker trong WSL. Đã thử load shedding, dừng/khôi phục instance, mất toàn bộ dependency hoặc instance đầu vào và deadline. Xem [báo cáo nghiệm thu](acceptance.md) để biết số liệu và giới hạn; sau nâng runtime, bài deadline đã đạt trong phạm vi đã kiểm chứng; xem [điều tra deadline](deadline-investigation.md). Chưa nghiệm thu nhiều máy hoặc production.

Cập nhật theo tham khảo: xem [reference-design.md](reference-design.md) cho cơ chế timeout gRPC tại Envoy, circuit breaker, health Watch và bằng chứng đo đạc; nguồn chính thức và các lựa chọn riêng của sample được phân biệt trong tài liệu đó.
