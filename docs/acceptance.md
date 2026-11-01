# Nghiệm thu lớp tiếp nhận và lớp xử lý

Ngày kiểm chứng: 25/10/2026.

Tài liệu này giữ kết quả của đợt trước khi chuẩn hóa theo tham khảo; số liệu gốc được giữ trong `results/before-reference-acceptance.json`. Cơ chế triển khai mới và nguồn đối chiếu nằm ở [reference-design.md](reference-design.md); kết quả mới xem [reference-verification.md](reference-verification.md).

**Kết luận: các bài chức năng trên một host đạt sau khi vá lỗi, nhưng chưa nghiệm thu toàn bộ. Deadline theo thời gian thực tại client Windows khi quá tải chưa đạt; production và đa host chưa được xác nhận.**

Phạm vi chạy thực: client Python trên Windows → Envoy → hai instance A → hai B và hai C, qua mạng Docker trong WSL Ubuntu-22.04. Workload là asynchronous delay mô phỏng; service IDs và topology đọc từ JSON.

Môi trường: Python 3.12, gRPC 1.71.2, Docker Engine 29.1.3, Compose standalone 5.6.1 và Envoy 1.35.5 (hai worker). Ban đầu Windows không có Docker CLI trong PATH; khi nghiệm thu đã phát hiện Engine hoạt động trong WSL, nên có thể kiểm chứng toàn tuyến.

## Các lỗi và điểm yếu đã xử lý

| Phát hiện | Bằng chứng / ảnh hưởng | Bản vá |
|---|---|---|
| Hạn mức RPC chung chặn cả health check | Tái hiện `RESOURCE_EXHAUSTED` trên Health/Check khi workload chiếm đủ slot; caller có thể loại nhầm instance còn sống | Chuyển admission vào Execute; health độc lập, vẫn giới hạn tổng workload đang chạy/chờ |
| Request ID Unicode làm metadata không hợp lệ | Request ID có dấu gây `GRPC_CALL_ERROR_INVALID_METADATA`, client cuối cùng timeout | Percent-encode header ASCII, giữ nguyên request ID Unicode trong protobuf; test qua RPC nội bộ |
| Timer timeout trễ khi event loop bị khựng | Test cố ý chặn loop 50 ms cho budget 10 ms tái hiện trả OK quá ngân sách trước khi callback timeout chạy | Kiểm tra monotonic ngay trước khi trả response, không chỉ dựa vào `asyncio.timeout`; test hồi quy nhận DEADLINE_EXCEEDED |
| Khởi động Envoy lâu không có upstream khỏe | Probe đầu lỗi trước khi backend sẵn sàng; counter chỉ có hai probe, backend đã SERVING nhưng client vẫn nhận `no healthy upstream` trong cửa sổ chờ 45 giây | Đặt `no_traffic_interval: 1s` cho cả hai cấu hình Envoy; chạy lại startup và recovery thành công |
| Validation cấu hình không đầy đủ | Kiểu dữ liệu sai gây ngoại lệ không nhất quán; chuỗi dependency có thể bị hiểu nhầm; host/port sai và duplicate JSON key chưa bị chặn | Validate schema, kiểu số, endpoint, trùng ID/endpoint/key, dependency, chu trình và độ sâu tối đa 64 |
| Launcher chưa kiểm tra cổng trước khi khởi động | Có thể khởi động dở cụm hoặc kiểm tra nhầm endpoint đang tồn tại | Preflight toàn bộ cổng local trước khi tạo child/ghi log; tắt socket reuseport tại service |
| Build context chứa môi trường và kết quả chạy | Không có `.dockerignore`, phải gửi cả `.venv` và kết quả thí nghiệm | Thêm `.dockerignore`; các replica dùng chung một image, harness build một lần |
| Lỗi giao thức trên đường client Windows ↔ WSL khi quá tải | Tái hiện GOAWAY `kWrongFrameSequence` và UNAVAILABLE ngoài các mã load shedding mong đợi; client Linux không tái hiện trong bài đối chiếu | Chọn nghttp2 cho codec nhận phía Envoy bằng `use_oghttp2_codec: false`; validator và bài tải sau thay đổi đạt. Đây là mitigation theo bộ phiên bản đã pin, chưa chứng minh nguyên nhân gốc |

Đã bỏ trường admin access log cũ bị Envoy cảnh báo deprecated. Hai cấu hình Envoy đều được validator của chính Envoy xác nhận hợp lệ.

Trường chọn codec có trong [API Envoy 1.35.5](https://github.com/envoyproxy/envoy/blob/v1.35.5/api/envoy/config/core/v3/protocol.proto), được đánh dấu work-in-progress. Cấu hình tương thích này cần được đánh giá lại khi nâng Envoy/gRPC; không coi đó là khuyến nghị production chung.

## Kết quả kiểm thử

Toàn bộ pytest: **78 test đạt**, gồm 20 test hồi quy bổ sung trong `tests/test_acceptance.py`. Launcher native 6 process vẫn chạy thành công sau các bản vá.

Kết quả xuyên proxy ở [results/before-reference-acceptance.json](../results/before-reference-acceptance.json) có `passed: true` cho các bài chức năng và `cleanup: passed`. Verdict bổ sung ở [results/before-reference-acceptance-verdict.json](../results/before-reference-acceptance-verdict.json) có `functional_checks_passed: true`, nhưng `client_wall_deadline_accepted: false` và **`overall_accepted: false`**. Không dùng riêng cờ `passed` trong báo cáo chức năng để kết luận nghiệm thu toàn bộ.

| Kịch bản | Kết quả quan sát |
|---|---|
| Startup và phân phối từng RPC trên một channel | A/B/C đều có đủ hai instance tham gia; initial check chia 12–12 |
| 1.000 request nhẹ, concurrency 32 | 1.000 OK; mỗi replica tại mỗi service nhận 500 request |
| 300 request nặng, concurrency 16 | 300 OK |
| 5.000 request nhẹ, concurrency 64 | 5.000 OK; mỗi replica tại mỗi service nhận 2.500 request |
| Quá tải có chủ đích | Capacity A giảm riêng cho test xuống 2/instance; 1.000 request, concurrency 128: 349 OK, 443 RESOURCE_EXHAUSTED, 208 DEADLINE_EXCEEDED; trong số OK có 148 response được client nhận sau 3 giây |
| Đối chiếu client Linux cùng capacity thấp | 54 OK, 946 RESOURCE_EXHAUSTED; không có OK nhận sau deadline; p99 khoảng 454 ms |
| Sau quá tải | Khôi phục cấu hình capacity, toàn bộ graph phục vụ lại |
| Dừng b-2 | Chỉ b-1 xử lý sau health hội tụ; start b-2 thì cả hai tham gia lại |
| Dừng a-2 | Envoy chỉ gửi đến a-1 sau health hội tụ; start a-2 thì cả hai tham gia lại |
| Dừng toàn bộ B | 10/10 UNAVAILABLE; start lại B thì graph phục hồi |
| Dừng toàn bộ A | 10/10 UNAVAILABLE; start lại A thì graph phục hồi |
| 100 request với deadline 1 ms | 100 DEADLINE_EXCEEDED; các request kiểm tra sau đó thành công |
| Telemetry Envoy | Đọc được endpoint Prometheus và lưu snapshot |
| Dọn môi trường | Đã down stack; xác nhận không còn container/network của project kiểm thử |

Trong lần thử quá tải ban đầu, concurrency 512 vẫn cho 1.500/1.500 OK do còn hàng đợi/giới hạn tại client, proxy và HTTP/2. Bài test đó chưa chứng minh admission đã đầy. Harness sau đó dùng override topology riêng để hạ capacity có chủ đích, không thay đổi cấu hình mẫu của dự án.

## Ý nghĩa của số liệu hiệu năng

Lần chạy cuối ghi nhận khoảng 332 RPC/s ở baseline và 339 RPC/s ở bài 5.000 request. p99 lần lượt khoảng 411 ms và 403 ms. Đây là số liệu mô tả một lần chạy, không phải công suất tối đa hoặc cam kết latency.

Bài quá tải trên client Windows có p99 của response OK khoảng 5,55 giây khi đặt deadline 3 giây; 148/349 response OK được nhận sau ngân sách. Các số đếm này mô tả mã gRPC quan sát được, không đồng nghĩa mọi OK đều đáp ứng deadline. Với client Linux qua cùng Envoy/capacity, p99 khoảng 454 ms và không có OK nhận sau deadline. Hai đường client khác môi trường chạy và network path, nên chưa thể quy hết nguyên nhân cho Windows, gRPC hoặc scheduler khi chưa có trace. Kiểm tra ngân sách tại server đã được vá và có regression test, nhưng điều đó chưa giải quyết độ trễ nhận response tại client Windows khi quá tải. Đây là điều kiện chưa đạt, không bị bỏ qua để cho bài nghiệm thu xanh.

Chưa chạy nhiều lần để có khoảng biến thiên, chưa chạy soak hàng giờ, chưa đo memory/FD theo thời gian, chưa tạo workload CPU/I/O thực và chưa chốt SLO. Vì vậy chỉ nghiệm thu các hành vi chức năng đã quan sát, không nghiệm thu hiệu năng production.

## Chạy lại

Máy có Docker/Compose trong PATH:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/acceptance_microservices.py
```

Trên môi trường đã kiểm chứng, Compose standalone được tải vào `.venv/acceptance-tools`, xác minh SHA-256 với checksum release chính thức của Docker Compose:

```powershell
.\.venv\Scripts\python.exe scripts/acceptance_microservices.py --wsl Ubuntu-22.04 --compose-binary /mnt/d/LoadBalance/.venv/acceptance-tools/docker-compose-linux-x86_64
```

Đường dẫn binary trên là công cụ local, không được commit; máy khác cần Compose riêng. Harness giữ một WSL foreground session trong lúc client Windows phát tải, tạo project `loadbalance-acceptance`, từ chối chạy nếu project đã có container và dọn stack trong `finally`.

Harness ghi thêm verdict và trả exit code khác 0 nếu còn response OK nhận sau ngân sách, kể cả khi các bài chức năng đều đạt. Đây là gate mới được áp lên dữ liệu lần chạy cuối; verdict được tính trực tiếp từ các counter trong JSON, không sửa số liệu gốc.

Các file runtime override nằm dưới `results/`; cấu hình topology gốc không bị sửa khi giảm capacity cho bài overload. Các cổng 50050/9901 phải trống trước khi chạy. Quá trình này build image và tạo/dừng các container kiểm thử.

Artifacts:

- `results/acceptance.json`: kết quả và số liệu từng giai đoạn.
- `results/acceptance-verdict.json`: verdict chức năng, deadline và tổng thể.
- `results/acceptance-before-codec.json`: đối chiếu lỗi quá tải trước mitigation codec.
- `results/acceptance-codec-before-budget-guard.json`: snapshot sau codec, trước vá kiểm tra budget cuối handler.
- `results/acceptance-pytest.txt`: output lần chạy pytest cuối.
- `results/acceptance-envoy.prom`: snapshot metrics cuối bài test.
- `results/acceptance-compose.log`: lệnh Compose và output; có cả các lần thử trước khi vá.

## Chưa được xác nhận

- Đa host, mất máy, network partition, packet loss và DNS outage.
- HA cho ingress/control plane; TLS/mTLS, xác thực và chính sách truy cập public.
- Lookaside/xDS động và AIOps: chưa triển khai.
- Deadline theo wall clock của client Windows khi quá tải: đã phát hiện vi phạm, chưa giải quyết dứt điểm. Cần trace send/start/receive và callback, rồi so sánh runtime gRPC trước khi tăng phạm vi triển khai.
- Benchmark có kiểm soát, soak test và SLO cho cả thành công lẫn lỗi.
- Stack ingress WorkloadService M1 riêng: đã validate Envoy config, nhưng chưa chạy toàn bộ bộ fault/load qua stack đó; các kết quả end-to-end phía trên thuộc stack microservices tổng quát.
