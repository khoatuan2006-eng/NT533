# Lớp tiếp nhận — Server-side LB

## Phạm vi triển khai

Envoy nhận gRPC qua một endpoint và phân phối từng RPC theo Round Robin tới ba WorkloadService replica. Client dùng một channel thông thường, không cần danh sách backend hoặc WorkloadClient của M1.

```text
External gRPC client → localhost:50050 (Envoy) → replica-1/2/3:50051
```

Các WorkloadService đóng vai trò service đầu vào trong bước này. Lookaside, chuỗi gọi nhiều service và AIOps sẽ được triển khai ở các bước tiếp theo theo [định hướng dự án](project-direction.md).

Envoy dùng HTTP/2 tới backend và `grpc.health.v1.Health/Check` với tên `workload.v1.WorkloadService`. Endpoint không khỏe bị loại sau hai lần kiểm tra lỗi; endpoint phục hồi cần hai lần kiểm tra thành công. Không có backend khỏe thì trả lỗi, không bật panic routing tới backend đã bị đánh dấu lỗi. Request đang chạy vẫn có thể lỗi khi backend chết; không tự retry vì Execute chưa có hợp đồng idempotency.

## Chạy

Yêu cầu Docker Engine/Docker Desktop chạy Linux containers, Compose v2 và môi trường Python đã cài dự án theo README. Từ thư mục gốc:

```powershell
docker compose -f deploy/compose.ingress.yaml config --quiet
docker compose -f deploy/compose.ingress.yaml run --rm --no-deps ingress --mode validate -c /etc/envoy/envoy.yaml
docker compose -f deploy/compose.ingress.yaml up --build -d
```

Compose này độc lập với `deploy/compose.yaml` của M1. Backend không publish port ra host. Delay được cấu hình bằng `SERVICE_DELAY_S` để không bị giá trị mặc định trong Dockerfile ghi đè; concurrency dùng đúng `MAX_CONCURRENCY`.

Kiểm tra health trước khi gửi tải:

```powershell
Invoke-RestMethod http://localhost:9901/clusters
```

Chờ cả ba endpoint có `health_flags::healthy`, sau đó:

```powershell
.\.venv\Scripts\python.exe scripts/check_ingress.py
```

Script dùng một channel và 60 RPC tuần tự, kiểm tra cả ba backend nhận request và phân bố gần đều; lỗi RPC hoặc sai phân bố sẽ trả exit code khác 0. Điều này kiểm chứng cân bằng theo RPC, thay vì chỉ chia kết nối TCP.

## Kiểm tra backend chết và phục hồi

```powershell
docker compose -f deploy/compose.ingress.yaml stop replica-2
```

Chờ admin `/clusters` đánh dấu endpoint lỗi hoặc loại địa chỉ khỏi DNS, sau đó chạy:

```powershell
.\.venv\Scripts\python.exe scripts/check_ingress.py --expected replica-1,replica-3
docker compose -f deploy/compose.ingress.yaml start replica-2
```

Khi cả ba endpoint healthy trở lại, chạy lại script mặc định. Health check hội tụ theo thời gian; không yêu cầu request ngay sau thời điểm dừng backend đều thành công.

## Logs, metrics và dừng

```powershell
docker compose -f deploy/compose.ingress.yaml logs --tail 30 ingress
Invoke-WebRequest http://localhost:9901/stats/prometheus
docker compose -f deploy/compose.ingress.yaml down
```

Access log JSON chứa upstream, duration, HTTP/gRPC status và request ID để chuẩn bị cho observability. Caller cần đặt deadline; route không áp timeout 15 giây mặc định, stream nhàn rỗi được giới hạn 60 giây.

## Giới hạn hiện tại

- Đây là ingress chạy thử: một Envoy instance, chưa có HA/failover ở địa chỉ vào chung.
- Listener dùng plaintext gRPC; các cổng chỉ bind localhost trên host. Chưa dùng cho Internet public. Trước khi public cần cấu hình domain/chứng chỉ TLS và chính sách truy cập phù hợp.
- Admin có quyền quản trị, chỉ publish localhost để kiểm thử; không mở ra Internet.
- Chưa có API HTTP/JSON hoặc gRPC-Web cho trình duyệt.
- Danh sách ba backend được khai báo trong cấu hình; service discovery động qua lookaside chưa nằm trong bước này.

## Trạng thái kiểm chứng khi triển khai

- CLI của script và luồng gọi một backend gRPC thật đã được kiểm tra, gồm phát hiện backend trả về khác danh sách mong đợi.
- Sau nghiệm thu: toàn bộ 81 test đạt. Phép đo latency M1 dùng đồng hồ monotonic để tránh latency 0 ms trên Windows.
- Đã tìm thấy Docker Engine trong WSL Ubuntu-22.04, dùng Compose standalone để kiểm chứng. Cả hai cấu hình Envoy đều qua validation bằng Envoy 1.35.5. Luồng xuyên proxy được kiểm thử trên stack microservices tổng quát; stack WorkloadService ba replica riêng chưa chạy toàn bộ bài fault/load tương tự. Xem [báo cáo nghiệm thu](acceptance.md).
- Đã đặt `no_traffic_interval: 1s` để tránh chờ chu kỳ mặc định 60 giây nếu probe đầu chạy trước khi backend sẵn sàng. Cấu hình mẫu giới hạn Envoy ở hai worker để chạy thử; chưa phải sizing production.

## Tài liệu tham khảo

- [Google: dùng Envoy cân bằng gRPC](https://docs.cloud.google.com/kubernetes-engine/docs/tutorials/exposing-grpc-services-on-gke-using-envoy-proxy)
- [Envoy: gRPC health check](https://www.envoyproxy.io/docs/envoy/v1.35.5/api-v3/config/core/v3/health_check.proto)

Timeout và backpressure đã được chuẩn hóa theo Envoy: `max_stream_duration` thực thi grpc-timeout, circuit breaker giới hạn connection/pending/active request. Xem [nguồn và quyết định thiết kế](reference-design.md), cùng [kiểm chứng mới](reference-verification.md).

Cập nhật 26/10/2026: runtime gRPC 1.84.0, bỏ workaround ép codec; deadline xuyên proxy đã đạt trong bài chạy mới. Xem [điều tra deadline](deadline-investigation.md).
