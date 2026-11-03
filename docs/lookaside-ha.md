# Registry, health controller và HA cho lookaside

Đã bổ sung registry Consul, health gRPC theo service và hai replica xDS. Envoy ở đầu vào và deadline request giữ nguyên. Stack này vẫn chạy trên một host; HA ở đây là khả năng chịu lỗi process/container được kiểm chứng, chưa phải khả năng chịu lỗi cả máy hoặc zone.

Cập nhật cải thiện HA: stack mặc định bật `CONSUL_ACTIVE_HEALTH=1`. Hai vòng đọc membership và kiểm tra health trực tiếp chạy độc lập; không chờ catalog Consul đồng bộ trạng thái health rồi mới cập nhật EDS. Phần kết quả 34 giây bên dưới được giữ làm mốc trước cải thiện; xem [điều tra và cải thiện HA](ha-improvement.md) cho thiết kế và nghiệm thu mới.

```text
Client → Envoy ingress → A-1 / A-2 → B/C instances
                            │          │
                   native xDS          └─ tự đăng ký với Consul agent của host
                            │                    │
                 DNS xds-controlplane       gRPC Health Check
                    ┌───────┴───────┐            │
                 xDS-1           xDS-2 ← consistent membership queries
                    └───────┬───────┘            │
                            └──────── Consul agent
                                         │
                                  3 Consul servers
                                    Raft / quorum
```

## Registry và health

Instance đăng ký `ID`, tên service, địa chỉ phục vụ và health check bằng API chuẩn của Consul client agent. Việc đăng ký không nằm trong đường xử lý RPC. Instance kiểm tra lại registration mỗi 4–6 giây; chỉ đăng ký lại khi registration hoặc check gRPC bị mất, tránh reset trạng thái health liên tục. Khi shutdown, chờ thao tác đăng ký HTTP đang chạy xong rồi mới deregister để không có một PUT đến muộn đăng ký lại instance đã dừng.

Consul agent kiểm tra tên `processing.v1.ProcessingService/<service-id>` bằng gRPC health protocol. Interval 1 giây, timeout 500 ms, yêu cầu hai lần thành công để passing và hai lần thất bại để critical. Instance critical kéo dài được Consul deregister sau ngưỡng cấu hình `1m`; việc rút khỏi routing diễn ra ngay khi không còn passing, không chờ deregister.

Control plane đọc membership bằng `/v1/health/service/<name>?consistent`, yêu cầu có check loại `grpc` gắn đúng `ServiceID`, resolve địa chỉ rồi kiểm tra trực tiếp chính IP:port sẽ publish trong EDS. Chỉ instance trả SERVING đủ hai lần mới được chọn; NOT_SERVING, lỗi hoặc timeout hai lần sẽ bị rút. Check dùng channel tái sử dụng, interval 1 giây, timeout 500 ms. Node khỏe nhưng không có check của ứng dụng sẽ không được đưa vào EDS. Danh sách rỗng là trạng thái hợp lệ; lỗi HTTP, lỗi DNS hoặc dữ liệu registry sai không được coi là một lần đọc membership thành công. Chế độ cũ `CONSUL_ACTIVE_HEALTH=0` vẫn dùng `passing=true&consistent` để tái lập phép đối chứng.

`DRAIN_FILE=/tmp/drain` cho phép deployment chuyển health sang NOT_SERVING mà vẫn giữ process và transport hoạt động. Tạo file để drain, xóa file để trở lại SERVING. Ví dụ với stack đang chạy:

```sh
docker compose -f deploy/compose.microservices.yaml -f deploy/compose.lookaside-ha.yaml exec b-2 python -c "from pathlib import Path; Path('/tmp/drain').touch()"
docker compose -f deploy/compose.microservices.yaml -f deploy/compose.lookaside-ha.yaml exec b-2 python -c "from pathlib import Path; Path('/tmp/drain').unlink(missing_ok=True)"
```

Drain thay đổi eligibility theo health; lời gọi đang chạy hoặc caller chưa nhận cập nhật vẫn có thể hoàn thành. Đây không phải thao tác đóng cổng ngay lập tức. Hệ thống không tự retry request nghiệp vụ.

## HA và dữ liệu cũ

Ba Consul server lưu registry bằng Raft, không dùng ba registry độc lập. Consul agent đặt trên host workload và chuyển tiếp đến cụm server. Khi triển khai nhiều máy cần agent cho từng host; agent trong stack một host này là một thành phần cục bộ của host đó, không phải một cụm agent HA dùng chung registration.

Hai replica xDS cùng đọc một nguồn registry, cùng cấu hình service scope và cùng tạo version từ nội dung resource. Chúng không tự sao chép trạng thái hoặc chạy thêm một thuật toán consensus riêng. `dns:///xds-controlplane:18000` trả địa chỉ của cả hai replica; channel của gRPC tới control plane xử lý mất kết nối và chọn địa chỉ còn hoạt động. Cách này phải được kiểm chứng bằng việc nhận **cập nhật mới** và khởi tạo **caller chưa có cache** khi từng replica dừng; chỉ thấy traffic cũ vẫn chạy chưa chứng minh được failover discovery.

Khi mất registry/quorum, control plane giữ membership hợp lệ trong cửa sổ freshness 15 giây kể từ lần đọc/resolve hợp lệ gần nhất; health của các endpoint đã biết vẫn được kiểm tra độc lập trong cửa sổ này. Hết cửa sổ, replica còn hoạt động publish EDS rỗng. Kiểm tra expiry không phải đợi truy vấn registry đang treo kết thúc; nhịp publish dự phòng là 1 giây. Khi registry phục hồi, endpoint tự xuất hiện lại nếu health trực tiếp đạt.

Nếu **cả hai xDS replica cùng mất kết nối**, caller vẫn giữ cấu hình trong bộ nhớ theo hành vi native gRPC. Control plane không thể đẩy lệnh hết hạn trong tình huống đó; freshness lease trên không phải TTL cục bộ của SDK. Chưa triển khai cơ chế loại bỏ cache ở caller khi toàn bộ control plane mất liên lạc.

`configs/discovery.registry.json` là allowlist tên service, được kiểm tra và đọc một lần khi khởi động mỗi xDS replica. Membership lấy hoàn toàn từ Consul, không lấy endpoint tĩnh trong file hoặc topology của caller. Khi thay scope, cập nhật cấu hình rồi rolling restart các replica. Việc sửa một file local lúc process đang chạy không được phép làm đóng băng vòng cập nhật health. Chế độ file của prototype cũ vẫn hỗ trợ hot reload như trước.

## Nguồn và quyết định của dự án

- [Consul service registration API](https://developer.hashicorp.com/consul/api-docs/agent/service), [gRPC health checks](https://developer.hashicorp.com/consul/docs/reference/service/health-check), [mã gRPC probe 2.1.0](https://github.com/hashicorp/consul/blob/v2.1.0/agent/checks/grpc.go): dùng cơ chế có sẵn để đăng ký và thực thi health.
- [Consul Health API](https://developer.hashicorp.com/consul/api-docs/health): dùng kết quả passing và truy vấn consistent. Không dùng dữ liệu stale-read khi mất quorum để chứng minh health vẫn mới.
- [Consul bootstrap](https://developer.hashicorp.com/consul/docs/deploy/server/vm/bootstrap), [reliability](https://developer.hashicorp.com/consul/docs/concept/reliability): ba server tham gia quorum; không tự viết registry/consensus.
- [gRPC load balancing](https://github.com/grpc/grpc/blob/v1.84.0/doc/load-balancing.md), [bootstrap format](https://github.com/grpc/grpc/blob/v1.84.0/doc/grpc_xds_bootstrap_format.md): dùng resolver/channel native để kết nối control plane. Không đồng nhất cách dùng một tên DNS nhiều địa chỉ với danh sách server ưu tiên của [A71](https://github.com/grpc/proposal/blob/master/A71-xds-fallback.md).

Consul được khóa ở `hashicorp/consul:2.1.0`. Interval, ngưỡng hai lần, poll control plane 1 giây, timeout HTTP 2 giây, budget fetch/resolve 4 giây và freshness 15 giây là policy lab. Control plane hiện dùng Python `grpc.aio`, protobuf xDS chính thức và HTTP client bất đồng bộ; membership, health và publish chạy độc lập. SDK gRPC vẫn tự route request nghiệp vụ như [thiết kế lookaside](lookaside.md).

## Chạy và nghiệm thu

```sh
docker compose -f deploy/compose.microservices.yaml -f deploy/compose.lookaside-ha.yaml up --build -d
python scripts/check_microservices.py
docker compose -f deploy/compose.microservices.yaml -f deploy/compose.lookaside-ha.yaml down --remove-orphans
```

Lệnh `down` thông thường giữ các volume registry. Bài nghiệm thu dùng project riêng, tạo và xóa volume của chính bài thử:

```powershell
.\.venv\Scripts\python.exe scripts/acceptance_lookaside_ha.py --wsl Ubuntu-22.04 --compose-binary /mnt/d/LoadBalance/.venv/acceptance-tools/docker-compose-linux-x86_64
```

Bài thử gồm tải nhẹ/nặng/stress, NOT_SERVING khi transport vẫn sống, tự đăng ký instance mới B-3 mà caller không biết trước, deregister, mất từng replica xDS với cập nhật mới và caller mới, mất Consul leader, khôi phục đủ ba voter, mất quorum, hết hạn membership, restart agent, backend dừng/phục hồi và deadline ngắn. Consul Autopilot có thể thay đổi tập voter khi node mất liên lạc, nên phải đợi đủ ba voter phục hồi trước bài mất hai trong ba server.

Kết quả: `results/lookaside-ha-acceptance.json`; log protocol: `results/lookaside-ha-controlplane.log`; kiểm thử Python: `results/python-controlplane-quality.txt`. Kiểm thử control plane Python còn chạy trong Docker build và ghi vào `results/lookaside-ha-compose.log`. Các kết quả Go ở mục lịch sử bên dưới thuộc phiên bản trước migration.

Lần đầu các phép thử chức năng đạt nhưng audit phát hiện còn container B-3 của profile scale. Kết quả đó được giữ và đánh dấu cleanup không đạt trong `results/lookaside-ha-initial-acceptance.json`. Runner hiện dọn cả profile và xác nhận project không còn container, không chỉ dựa vào exit code của `down`.

### Kết quả trước cải thiện HA — 26/10/2026

`results/lookaside-ha-before-fast-failover.json` giữ mốc `passed: true`, `cleanup: passed` trước bước cải thiện. Có 91/91 kiểm thử Python đạt; bộ kiểm thử Go đạt trong Docker build. Kết quả mới nằm ở `results/lookaside-ha-acceptance.json` và [tài liệu cải thiện HA](ha-improvement.md).

| Kịch bản | Kết quả |
|---|---|
| Tải nhẹ / nặng / stress | 1.000 / 300 / 5.000 request đều OK; không có OK nhận sau timeout 3 giây |
| NOT_SERVING khi transport vẫn sống | Direct business RPC vẫn trả B-2, health trả NOT_SERVING; graph chỉ dùng B-1 sau 3,844 giây |
| SERVING trở lại | Graph dùng lại cả B-1/B-2 sau 2,812 giây |
| B-3 mới, caller không biết trước | Tự đăng ký và xuất hiện trong graph sau 6,234 giây; dừng thì deregister |
| Chỉ còn xDS-2 / chỉ còn xDS-1 | Health update mới được nhận sau khoảng 2,94 / 2,92 giây; mỗi cold caller thực hiện 40 RPC, phân phối 20/20 cho B-1/B-2 |
| Đóng băng Consul leader | Bầu leader khác và tiếp tục cập nhật health; graph hội tụ sau **34,266 giây** |
| Server Consul phục hồi | Đã xác nhận đủ ba voter trước phép thử mất quorum tiếp theo |
| Mất hai trong ba server | Sau cửa sổ freshness, 40/40 request UNAVAILABLE; phục hồi quorum thì graph phục hồi sau 7,390 giây |
| Restart agent và backend | Registration/health tự phục hồi; thời điểm khởi động hai caller A không đổi |
| Timeout 1 ms | 100/100 DEADLINE_EXCEEDED; graph phục hồi sau đó |
| Cleanup | Dọn cả profile scale và xác nhận project không còn container |

Các thời gian trên là thời gian bài `wait_graph` chờ hội tụ rồi hoàn thành 24 RPC kiểm chứng, không phải phép đo tách riêng thời gian bầu leader hay phân phối EDS. Ngưỡng chờ 45 giây là gate của lab. **34 giây khi mất leader là giới hạn đáng chú ý, chưa đủ để chốt HA có độ trễ thấp cho production**; cần tách thêm thời gian phát hiện lỗi, RPC/đồng bộ catalog của Consul và cập nhật xDS trước khi đặt SLO hoặc tuning. Không tăng deadline RPC để che khoảng hội tụ này, và chưa đo đầy đủ error rate của mọi request trong giai đoạn chuyển tiếp.

## Giới hạn còn lại

Chưa nghiệm thu lỗi cả máy, partition nhiều host/zone, tải kéo dài, độ trễ mạng lớn hoặc storage hỏng. Agent của host và ba volume server vẫn cùng nằm trên một máy trong lab. Chưa có TLS/mTLS, ACL/token provisioning, telemetry/tracing tập trung hoặc AIOps. Các API và traffic control plane hiện dùng mạng Docker nội bộ; không publish API Consul hay xDS ra host. Kết quả hội tụ không đồng nghĩa không có lỗi RPC trong khoảng chuyển tiếp.
