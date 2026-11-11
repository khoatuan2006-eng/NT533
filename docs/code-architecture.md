# Cấu trúc mã và quy tắc mở rộng

Ứng dụng và control plane đều dùng Python với class, `Protocol` và composition. Các hàm chuyển đổi dữ liệu thuần vẫn là hàm. Deadline, admission, protocol gRPC/xDS và mô hình external Envoy → A → B/C giữ nguyên. Không còn mã hoặc toolchain Go trong dự án.

## Python data plane

| Module | Trách nhiệm |
|---|---|
| `topology.py` | Đọc và kiểm tra DAG, ID, endpoint, workload và giới hạn hop |
| `endpoints.py` | Địa chỉ `host:port` chuẩn hóa và `EndpointResolver`, dùng chung cho control plane và caller |
| `service_identity.py` | Tên RPC/health dùng chung cho handler, registry và probe |
| `dependencies.py` | Hợp đồng `DependencyClient`, static/xDS client và factory chọn transport |
| `processing.py` | Handler `Service`: validation request, admission, budget và orchestration |
| `workload.py` | Hợp đồng công việc cục bộ và implementation mô phỏng mặc định |
| `service_runtime.py` | Sở hữu gRPC server, health service và các dependency client |
| `registry.py` | Đăng ký/khôi phục/deregister Consul; deployment drain marker |
| `application.py` | `ServiceSettings` và `ServiceApplication`: cấu hình, task nền, signal, startup/shutdown |
| `microservices.py` | CLI nhỏ và facade giữ các import/lệnh chạy cũ tương thích |

Handler chỉ cần hợp đồng `start/call/close` của dependency client; không đọc environment hay sở hữu connection. Thêm transport mới bằng implementation của `DependencyClient` và một nhánh tại `DependencyPoolFactory`, giữ handler xử lý độc lập.

Để thay workload mô phỏng, triển khai `Workload.execute(request)` và truyền instance qua tham số `workload` của `start_service` tại composition root. Công việc chạy trong semaphore và budget chung, phải hợp tác với cancellation; lỗi tạm thời có thể báo bằng `WorkloadUnavailable`. Hợp đồng RPC hiện vẫn giới hạn `light/heavy` và kết quả chưa chứa payload nghiệp vụ: nghiệp vụ thật có dữ liệu riêng cần mở rộng proto và validation tương ứng. Runtime mặc định vẫn dùng `SimulatedWorkload`.

`ServiceSettings` kiểm tra deployment trước khi mở socket. `ServiceApplication` giám sát task nền: lỗi không mong đợi của readiness/registration được đưa ra ngoài và đóng ứng dụng. Expected network errors của registration vẫn retry trong lớp registration. Shutdown chờ thao tác đăng ký HTTP đang chạy xong, dừng server/connection rồi deregister. Exit stack bảo đảm một lỗi cleanup không làm bỏ qua các tài nguyên còn lại.

Phần M1 ở `server.py`, `client.py`, `balancer.py`, `loadgen.py`, `telemetry.py`, `summarize.py` giữ API thí nghiệm cũ. Các module mới dùng chung generated protobuf, không sửa tay mã sinh.

## Python control plane — `src/grpc_lb/controlplane/`

| Thành phần | Trách nhiệm |
|---|---|
| `RuntimeSettings`, `config.py` | Kiểm tra environment và schema discovery |
| `EndpointResolver` | Resolve/chuẩn hóa endpoint, kiểm tra port và trùng IP:port |
| `ResourceBuilder` | Chuyển membership đã chuẩn hóa thành LDS/CDS/EDS, version theo nội dung |
| `ConsulRegistry`, `MembershipReader` | Adapter HTTP bất đồng bộ của Consul và hợp đồng registry |
| `HealthController` | Vòng registry và probe gRPC độc lập; snapshot membership đủ health/freshness |
| `SnapshotSource` | Protocol discovery: `read`, `changed`, `close` |
| `FileSource`, `CatalogSource`, `HealthController` | File hot reload, passing-only và membership + active health |
| `SnapshotPublisher` | Validate và commit snapshot; giữ snapshot cũ nếu candidate sai |
| `ControlPlane` | Sở hữu ADS server, source, loop publish và cleanup |
| `SnapshotStore`, `AggregatedDiscoveryService` | Snapshot nguyên tử; SotW ADS v3, subscription và ACK/NACK theo resource type |
| `__main__.py` | Composition root: settings → source → application; CLI và signal |

Health controller nhận endpoint chuẩn hóa trực tiếp từ resolver và trả snapshot có kiểu dữ liệu. Nó không tạo xDS resource rồi đọc ngược EDS để lấy địa chỉ. Muốn thêm registry khác, triển khai adapter/source tương ứng và nối tại factory; mã ADS và handler Python không phải thay theo HTTP API của registry mới.

Source được đóng cả khi startup lỗi, cổng bị chiếm hoặc application dừng. Task registry/probe không mong đợi bị lỗi được báo ra ngoài để dừng application; expected transport errors giữ snapshot trong lease. `ControlPlane` chỉ chạy một lần; muốn restart tạo instance mới. Fetch timeout, freshness lease và publish interval đặt tập trung trong `controlplane/config.py`.

## Các bất biến cần giữ

- Request chỉ có một budget cho queue, local work và dependency; truyền phần còn lại xuống dưới. Guard monotonic trước OK và hủy sibling khi nhánh parallel lỗi vẫn giữ nguyên.
- Health và membership là hai nguồn thông tin riêng. Health sống không vượt qua lease membership; registry RPC chậm không chặn đổi health của endpoint đã biết.
- Cấu hình lỗi không thay snapshot hợp lệ. Membership rỗng phải xuất hiện trong EDS để caller fail closed.
- Endpoint health phải dùng đúng tên logical service và đúng IP:port sẽ đưa vào EDS.
- Application sở hữu lifecycle; handler không tự tạo channel theo request hoặc tự đọc environment.
- Khi thêm policy/transport/source, bổ sung kiểm thử hành vi và chạy nghiệm thu tương ứng. Một bảng lint xanh không chứng minh HA hoặc deadline đã đúng.

## Cài đặt và kiểm tra

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe scripts/check_quality.py
.\.venv\Scripts\grpc-lb-service.exe --check-config
.\.venv\Scripts\python.exe scripts/run_microservices.py --check
```

`check_quality.py` chạy Ruff lint, kiểm tra format và pytest, dừng ở gate đầu tiên lỗi. Dùng `--skip-tests` cho kiểm tra nhanh. Ruff được khóa version trong dev dependencies; generated protobuf và kết quả thí nghiệm được loại khỏi formatter/linter.

`--check-config` đọc các environment deployment hiện có và in service/instance/mode/listen/advertise, không mở server hay in token. Lệnh `python -m grpc_lb.microservices` vẫn tương thích với Compose hiện tại. Các script M1 dùng package đã cài như các script còn lại; cài editable trước khi chạy.

Control plane chạy native Python hoặc qua Compose:

```powershell
$env:DISCOVERY_CONFIG = "configs/discovery.registry.json"
.\.venv\Scripts\grpc-lb-controlplane.exe --check-config
.\.venv\Scripts\python.exe -m pytest -q tests/test_controlplane.py tests/test_controlplane_ads.py tests/test_controlplane_sources.py
```

Dockerfile control plane chạy bộ test Python trước khi hoàn tất image runtime không có dev dependencies. Hai stack triển khai giữ lệnh Compose trong [lookaside](lookaside.md) và [lookaside HA](lookaside-ha.md). Bản Python phục vụ SotW ADS v3 LDS/CDS/EDS cho các caller hiện tại; Delta ADS chưa hỗ trợ.

## Những lỗi logic đã sửa trong đợt rà soát

| Phát hiện | Sửa và kiểm chứng |
|---|---|
| Deployment advertise/Consul URL được đọc sau khi server đã mở cổng | Validate trong settings trước startup; test cấu hình sai và startup failure giải phóng cổng |
| Task readiness chết có thể bị bỏ qua đến shutdown | Application giám sát task; test lỗi readiness được trả ra ngoài và server đóng |
| Lỗi health/cleanup một pool có thể bỏ qua các pool khác | Exit stack đóng server và mọi pool; fault-injection test xác nhận tất cả cleanup được gọi |
| Control plane tạo discovery loop trước bind nhưng lỗi bind có thể bỏ qua cleanup | Source thuộc ownership của application; test lỗi source và lỗi bind đều đóng source |
| Health controller phụ thuộc vào cấu trúc EDS để lấy lại IP | Dùng resolver riêng, snapshot typed; giữ kiểm thử health/lease/blocked-registry |
| Backend ID M1 trùng có thể ghi đè channel pool | Reject ID trùng trước khi tạo channel; test xác nhận chưa gọi constructor channel |
| M1 server lỗi bind có thể để log file mở | Factory cleanup server/log cả khi khởi tạo lỗi; test bind thật vào port đang chiếm |
| Client M1 cancel health task nhưng chưa chờ cleanup hoàn tất | Join task trước khi đóng channel; test cleanup bất đồng bộ đã hoàn thành khi close trả về |

Các kiểm tra này tập trung vào ranh giới và lỗi thực tế. Chưa phải bằng chứng không còn mọi lỗi hoặc đủ production; phạm vi HA vẫn là lab một host như các tài liệu nghiệm thu.

## Kết quả refactor trước migration Python — 26/10/2026

- Ruff lint và format đạt trên 43 file Python được quản lý; 105/105 kiểm thử Python đạt.
- `go vet` và bộ kiểm thử Go đạt trong Docker build; kiểm thử ADS, health khi registry treo, freshness, snapshot rollback và startup cleanup được giữ/bổ sung.
- Launcher 6 process đạt cho cả hai entry instance. Benchmark M1 smoke đạt 30/30 request với mỗi policy Round Robin và Least Request; đây là smoke test, không dùng để so sánh hiệu năng hai policy.
- Nghiệm thu HA sau refactor đạt, cleanup xác nhận project trống. 6.300 request tải đều OK, không OK nào nhận sau deadline; phép thử health update khi mất leader hội tụ sau 3,109 giây, nằm trong gate 10 giây.

Bằng chứng tổng hợp lịch sử: `results/code-quality-review.json`, `results/code-quality-python.txt`, `results/before-python-controlplane-ha.json`. Kết quả HA ngay trước refactor giữ ở `results/before-code-cleanup-ha-acceptance.json`. Migration Python và nghiệm thu mới được mô tả trong [chuyển control plane sang Python](python-controlplane.md).
