# Kiểm chứng sau khi đối chiếu gRPC và Envoy

Ngày: 25/10/2026.

Đây là snapshot trước khi nâng runtime. Kết quả mới ngày 26/10/2026 đã đạt bài deadline sau khi đối chiếu/nâng gRPC; xem [deadline-investigation.md](deadline-investigation.md). Raw data của snapshot này được giữ ở `results/before-runtime-upgrade-acceptance.json` và verdict tương ứng. Nguồn và phạm vi áp dụng: [reference-design.md](reference-design.md).

## Những gì đã thay đổi

- Envoy thực thi timeout gRPC bằng `max_stream_duration`, header cap 5 giây và offset 10 ms; trước đây chỉ tắt route timeout mặc định.
- Có circuit breaker cho connection, pending request và request outstanding. Giá trị sample gắn với capacity mẫu; không lấy các con số làm hằng số khuyến nghị chung.
- Endpoint eligibility dựa trên health, không thay đổi chỉ vì một RPC ứng dụng trả UNAVAILABLE.
- Health nội bộ dùng protocol Watch, nhận push thay vì polling liên tục. Bootstrap dùng Check; mất stream thì reconnect với exponential backoff và jitter.
- Đo thời gian nhận response trước bước validation/aggregation. Ghi server queue/duration/budget qua trailer, event-loop lag ở client và mẫu lỗi gồm cả initial/trailing metadata.
- Circuit breaker của Envoy trả UNAVAILABLE có `x-envoy-overloaded`; harness kiểm tra marker ở cả hai loại metadata, không coi mọi UNAVAILABLE là load shedding hợp lệ.

## Kết quả lần chạy cuối

**80 test đạt**, trong đó có test health Watch push, lỗi ứng dụng không loại nhầm endpoint và diagnostic trailer. Output pytest ở `results/reference-pytest.txt`.

Envoy binary validation đạt. Đã chạy client Windows → Envoy → 6 instance trong Docker/WSL và đối chiếu client Linux:

| Bài kiểm tra | Kết quả |
|---|---|
| 1.000 request nhẹ, concurrency 32 | 1.000 OK; không có OK nhận sau deadline |
| 300 request nặng, concurrency 16 | 300 OK |
| 5.000 request nhẹ, concurrency 64 | 5.000 OK; không có OK nhận sau deadline |
| Overload Windows: 1.000 request, concurrency 128, capacity A 2/instance | 429 OK, 294 RESOURCE_EXHAUSTED, 181 DEADLINE_EXCEEDED, 96 UNAVAILABLE |
| Phân loại 96 UNAVAILABLE phía Windows | Cả 96 có marker circuit breaker của Envoy |
| Overload Linux cùng sample capacity | 36 OK, 710 RESOURCE_EXHAUSTED, 254 UNAVAILABLE; cả 254 có marker circuit breaker |
| Dừng/khôi phục b-2 và a-2 | Đạt sau health hội tụ |
| Dừng toàn bộ B hoặc toàn bộ A | Trả UNAVAILABLE, khôi phục xong phục vụ lại |
| Deadline 1 ms và kiểm tra sau đó | Request báo quá hạn; graph phục vụ lại |
| Cleanup | Đạt; harness dọn project kiểm thử |

Một lần thử trước đó còn có UNAVAILABLE không được xác nhận bằng marker; harness đã báo không đạt và giữ output trong log. Lần chạy cuối không tái hiện nhóm lỗi đó. Không dùng một lần không tái hiện làm bằng chứng lỗi chập chờn đã được loại bỏ hoàn toàn.

## Vấn đề chưa giải quyết

**Không chốt nghiệm thu toàn bộ:** `results/before-runtime-upgrade-acceptance-verdict.json` có `overall_accepted: false`.

Trong bài overload Windows, 180/429 response OK được nhận sau ngân sách 3 giây. p99 phía client khoảng 4.589 ms; server duration lớn nhất trong các response OK khoảng 195 ms, event-loop lag phía client lớn nhất khoảng 543 ms.

Một mẫu cụ thể:

```text
request_id: acceptance-128
client elapsed: 3138.402 ms
server handler duration: 79.687 ms
server budget lúc vào handler: 2978.977 ms
server queue: 0.042 ms
```

Với client Linux, không có OK nhận sau deadline; p99 khoảng 322 ms và event-loop lag lớn nhất khoảng 32 ms. Các số liệu giúp khoanh vùng: thời gian nằm trong handler không giải thích phần lớn độ trễ quan sát tại client. Tuy nhiên, chưa phân tách hết thời gian trước khi gửi, transport queue, network/WSL forwarding và callback; không quy kết nguyên nhân gốc cho một thành phần chỉ từ mẫu này.

Các cơ chế theo tham khảo đã có tác dụng bảo vệ và phân loại lỗi, nhưng không tự chứng minh vấn đề client/runtime đã được sửa. Không tăng timeout, đổi nhãn late OK, hoặc nới verdict để làm báo cáo xanh. Chưa triển khai workaround mới cho phần này khi chưa có thí nghiệm đối chiếu và nguồn đủ rõ.

## Artifacts và chạy lại

- `results/before-runtime-upgrade-acceptance.json`: số liệu mới, timing/failure samples và kết quả từng phase.
- `results/before-runtime-upgrade-acceptance-verdict.json`: verdict tổng thể; cờ passed của report chức năng không thay thế verdict này.
- `results/acceptance-envoy.prom`: metrics Envoy.
- `results/acceptance-compose.log`: cả các lần thử đạt và chưa đạt.
- `results/before-reference-acceptance.json`: kết quả trước khi chuẩn hóa để giữ lịch sử.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/acceptance_microservices.py --wsl Ubuntu-22.04 --compose-binary /mnt/d/LoadBalance/.venv/acceptance-tools/docker-compose-linux-x86_64
```

Lệnh acceptance hiện trả exit code khác 0 vì deadline wall clock chưa đạt. Trên máy có Docker/Compose trong PATH, bỏ các tham số WSL. Các đường dẫn binary local không phải dependency đã commit.
