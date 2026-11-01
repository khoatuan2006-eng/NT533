# Chuyển control plane sang Python

Mã ứng dụng của dự án hiện dùng Python cho cả workload service và lookaside control plane. Đã bỏ thư mục Go `controlplane/`, thay bằng package `src/grpc_lb/controlplane/`. Envoy ingress và Consul vẫn là hạ tầng có sẵn, không viết lại hai sản phẩm này.

## Thiết kế

| Module | Thành phần và trách nhiệm |
|---|---|
| `config.py` | `RuntimeSettings`, kiểm tra schema/duplicate key, `EndpointResolver`; policy timeout/lease tập trung |
| `resources.py` | `ResourceBuilder`: LDS ApiListener/HCM → CDS EDS/ROUND_ROBIN → EDS IP:port; hash nội dung xác định version |
| `sources.py` | `SnapshotSource`/`MembershipReader` Protocol; file hot reload, Consul catalog và `HealthController` |
| `ads.py` | `SnapshotStore` và `AggregatedDiscoveryService`; SotW ADS v3, subscription theo type, nonce, ACK/NACK |
| `application.py` | `SnapshotPublisher` commit candidate hợp lệ; `ControlPlane` sở hữu server, task và cleanup |
| `__main__.py` | CLI, signal và composition root |

HTTP của registry dùng `httpx.AsyncClient`, probe dùng channel `grpc.aio` tái sử dụng. Membership và active health chạy trong các task riêng. Vòng publish kiểm tra lease mỗi giây ngay cả khi registry đang chờ I/O. Hết 15 giây không nhận membership hợp lệ thì EDS rỗng, dù backend vẫn SERVING. Mất toàn bộ control plane vẫn có giới hạn cache native của gRPC như trước.

Catalog passing-only cũng tự quản lý timeout đọc/resolve 4 giây: lỗi tạm thời giữ snapshot cũ trong lease, sau lease trả membership rỗng. Tạo resource có budget riêng 4 giây để không bỏ qua kết quả fail closed khi truy vấn vừa hết timeout. Các giá trị là policy lab hiện có; không thay deadline nghiệp vụ.

Server ADS dùng [protobuf xDS chính thức do gRPC phát hành](https://pypi.org/project/xds-protos/1.84.0/) và triển khai tập SotW ADS v3 theo [protocol Envoy](https://www.envoyproxy.io/docs/envoy/latest/api-docs/xds_protocol.html). Không dùng discovery protocol riêng. Phạm vi hiện tại là LDS/CDS/EDS phục vụ caller native gRPC; Delta ADS trả UNIMPLEMENTED. Đây là control plane của dự án, không phải toàn bộ tính năng của thư viện Go trước đây.

Mỗi type có subscription và nonce riêng trong một stream. Subscribe B không đợi C; ACK/NACK không gửi lại vô hạn cùng candidate; cập nhật mới vẫn được push. Cấu hình sai giữ snapshot hợp lệ gần nhất. SnapshotStore chỉ thông báo khi hash đổi và gộp các cập nhật cho stream chậm. Task stream, probe và registry được hủy/chờ khi dừng; source được đóng khi startup hoặc bind lỗi.

## Cài và chạy

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
$env:DISCOVERY_CONFIG = "configs/discovery.registry.json"
.\.venv\Scripts\grpc-lb-controlplane.exe --check-config
```

`--check-config` chỉ kiểm tra settings và file policy, không mở socket. Với chế độ file, lệnh còn resolve endpoint. Chạy server native bằng `python -m grpc_lb.controlplane` hoặc `grpc-lb-controlplane`. Để chạy native, phải dùng đường dẫn discovery và endpoint mà máy đó truy cập được; hostname Compose chỉ resolve trong mạng Docker.

Giữ các biến environment cũ: `DISCOVERY_CONFIG`, `XDS_LISTEN_ADDRESS` (mặc định `[::]:18000`), `CONSUL_HTTP_ADDR`, `CONSUL_ACTIVE_HEALTH`. Compose lookaside/HA và bootstrap caller giữ nguyên. Dockerfile control plane cài package Python và chạy các kiểm thử control plane trong bước build; runtime dùng user 65534 và không mang pytest/Ruff hay source test.

## Kiểm chứng

```powershell
.\.venv\Scripts\python.exe scripts/check_quality.py
.\.venv\Scripts\python.exe scripts/acceptance_lookaside.py --wsl Ubuntu-22.04 --compose-binary /mnt/d/LoadBalance/.venv/acceptance-tools/docker-compose-linux-x86_64
.\.venv\Scripts\python.exe scripts/acceptance_lookaside_ha.py --wsl Ubuntu-22.04 --compose-binary /mnt/d/LoadBalance/.venv/acceptance-tools/docker-compose-linux-x86_64
```

Các nhóm test `test_controlplane*.py` thay các kiểm thử Go: schema, resource và hash ổn định, ADS thực, ACK/NACK, stale nonce, subscription mới, registry malformed/consistent/passing-only, health khi registry treo, health timeout dù transport mở, lease, startup/bind failure, shutdown và lỗi task nền.

Hai lỗi phát hiện khi bổ sung kiểm thử migration đã được sửa: không hủy task chờ termination trước `server.stop` để tránh làm hỏng future shutdown nội bộ gRPC; catalog timeout vẫn phải đi qua quyết định lease và publish rỗng sau expiry.

Bằng chứng mới: `results/python-controlplane-quality.txt`, `results/python-controlplane-migration.json` và hai báo cáo nghiệm thu `results/lookaside-acceptance.json`, `results/lookaside-ha-acceptance.json`. Mốc trước chuyển ngôn ngữ giữ tại `results/before-python-controlplane-lookaside.json`, `results/before-python-controlplane-ha.json`. Chỉ coi migration đạt khi kiểm thử và nghiệm thu toàn tuyến cùng cleanup đạt. Phạm vi xác nhận vẫn là một máy Windows với container Linux/WSL, chưa phải đa máy/production.

## Kết quả nghiệm thu — 26/10/2026

| Kiểm tra | Kết quả |
|---|---|
| Ruff lint/format | Đạt, 53 file Python được quản lý |
| Toàn bộ pytest trên Windows | 155/155 đạt |
| Kiểm thử control plane trong Linux Docker build | 50/50 đạt |
| CLI module và console entrypoint `--check-config` | Đạt; `pip check` không có dependency lỗi |
| Lookaside chế độ file | `passed: true`, `cleanup: passed`; hot reload, invalid config, empty membership, backend/control-plane stop/recovery và deadline đạt |
| Lookaside HA | `passed: true`, `cleanup: passed`; self-registration, health, từng xDS replica, cold caller, leader/quorum, agent/backend recovery và deadline đạt |
| Tải qua hai stack | Mỗi stack 1.000 light + 300 heavy + 5.000 stress đều OK; tổng 12.600, không có OK nhận sau timeout client |
| Mất Consul leader | Health hội tụ sau 3,641 giây; gate giữ nguyên 10 giây |
| Mất quorum quá lease | 40/40 UNAVAILABLE; registry phục hồi thì graph trở lại |
| Deadline 1 ms ở HA | 100/100 DEADLINE_EXCEEDED, sau đó graph phục hồi |
| Image thực đang chạy | Python 3.12.15, xds-protos 1.84.0; không cài pytest |

Lượt image sơ bộ đã đi qua các phép thử chức năng nhưng không đạt gate bằng chứng log lease; báo cáo thất bại được giữ tại `results/python-controlplane-initial-ha.json`. Image cuối đã bổ sung log chuyển trạng thái lease và được chạy lại toàn bộ HA, không bỏ gate hoặc tăng timeout để báo đạt. Mã cuối cũng đã sửa hai lỗi shutdown/catalog timeout nêu phía trên và vượt qua cả 50 kiểm thử Linux.
