# Triển khai lab trên ba VM

**Cập nhật 28/10/2026:** ba VM bên dưới đã bị người dùng xóa. Cấu hình và kết quả hiện tại nằm trong [bản triển khai lại trên ba VM k3s](cloud-rebuild-2026-10-28.md). Phần dưới lưu cấu hình lịch sử ngày 27/10.

Các service, Consul và xDS chạy với Linux host networking; Envoy dùng bridge với port mapping. Đây là ba hệ điều hành độc lập, không phải ba container cùng host. Cloud hiện xếp cả ba VM vào az-02; chưa có bằng chứng chúng nằm trên ba hypervisor khác nhau.

| VM | IP nội bộ | Thành phần |
|---|---|---|
| lb-test-node-1 | 192.168.1.45 | Envoy, A-1, B-1, control plane 1, Consul 1 |
| lb-test-node-2 | 192.168.1.158 | Envoy, A-2, C-1, control plane 2, Consul 2 |
| lb-test-node-3 | 192.168.1.140 | B-2, C-2, Consul 3 |

Ba VM mới dùng gp.small-2c (2 vCPU, 2 GB RAM), Ubuntu 22.04 và volume 20 GB mỗi VM. VM cũ nhom15_vm_1 giữ nguyên. Các IP trong tài liệu là lần cấp phát ngày 27/10/2026, phải sinh lại cấu hình nếu thay VM.

## Đưa code lên

```powershell
.\.venv\Scripts\python.exe scripts/multihost.py render --nodes 192.168.1.45 192.168.1.158 192.168.1.140 --output results/cloud-cluster
.\.venv\Scripts\python.exe scripts/multihost.py bundle --cluster results/cloud-cluster --output results/loadbalance-release-v5.tar.gz
.\.venv\Scripts\python.exe scripts/multihost.py deploy --inventory results/cloud-ssh.json --ssh-config results/cloud-ssh-config --key C:/Users/Khoa/Downloads/nhom15_monitor.pem --archive results/loadbalance-release-v5.tar.gz --prebuilt
```

`cloud-ssh.json` hiện ánh xạ node1 tới Floating IP `192.168.120.109`, node2/node3 tới IP nội bộ. `cloud-ssh-config` cấu hình key trên Windows và ProxyJump qua node1 cho hai IP nội bộ. Chỉ node1 được gắn Floating IP mới, sau khi người dùng đồng ý; hai Floating IP cũ và VM cũ giữ nguyên. VPN và Floating IP là hai phần của đường truy cập, không phải hai lựa chọn thay thế. Không copy private key vào VM, không bật agent forwarding.

Bundle lấy code/config theo allowlist, bỏ `.venv`, `.git`, cache, log, kết quả cũ và private key. Script SCP gói lên `/tmp`, đối chiếu SHA-256 qua SSH trước khi giải nén vào `/opt/loadbalance`, cài Docker/Compose v2/Buildx từ Ubuntu packages nếu thiếu và chạy Compose riêng cho từng node. Dùng SSH key hiện có, BatchMode và known_hosts riêng; không bỏ kiểm tra host key, không thêm user vào nhóm docker. Lệnh gửi Linux qua SSH là UTF-8 bytes với LF, tránh Windows tự đổi stdin thành CRLF.

Node3 không phân giải Docker Hub ổn định, nên image được build trên node1 rồi xuất bằng `docker save | gzip`. Archive image SHA-256 `86852a4833a6b5a7a90a3d11d5771651e008bb6e61da9e36583990730a0b53c0` được chuyển qua mạng nội bộ tới node2/node3, kiểm tra checksum rồi `docker load`. HTTP server tạm chỉ bind `192.168.1.45:8020`, đã dừng sau khi chuyển xong. `--prebuilt` chỉ dùng khi đã nạp image được xác minh; Compose chạy `--no-build --pull never`, không âm thầm tải image khác. Nghiệm thu còn so sánh image ID của các container workload đang chạy.

Image được build từ release v4, sau đó driver triển khai/kiểm thử được hoàn thiện trong release v5. Đã đối chiếu SHA-256 từng file `src/`, `api/`, `pyproject.toml` trong hai archive: mã ứng dụng giống nhau. Gói v5 triển khai cuối trên cả ba VM có SHA-256 `bb4f80137d962cbe2dc31178b46a76c71c28596d6c08d0c81b6fe71f5e0fb9e8`. Manifest `results/cloud-deployment-status.json` lưu cả provenance này và image ID thực tế.

Gói giải nén có `cluster/topology.json`, `cluster/bootstrap.json`, `cluster/discovery.json` và `cluster/nodeN/compose.json`. Build context là gốc release, không phải workspace `results` khi sinh file. Runtime workload dùng Python 3.12 trong container; không phụ thuộc Python 3.10 của Ubuntu 22.04.

Hai control plane được bootstrap bằng resolver `ipv4:IP1:18000,IP2:18000` của [gRPC C-core](https://github.com/grpc/grpc/blob/v1.84.0/doc/naming.md). Backend và xDS bind IP nội bộ; Consul HTTP bind localhost. Envoy container giữ cổng không đặc quyền 50050; Docker publish `80:50050` và chỉ publish admin qua `127.0.0.1:9901:9901`. Không mở Consul HTTP/admin ra Floating IP.

Đã sửa hai lỗi triển khai phát hiện trên VM: Envoy không đặc quyền không bind được cổng 80 khi dùng host networking, và Compose v1 lỗi `ContainerConfig` với Docker Engine 29. Chuyển sang port mapping cùng Compose v2; không chạy Envoy bằng root hoặc vá thư viện Compose. Việc bỏ trường ContainerConfig được ghi trong [Docker Engine 26 release notes](https://docs.docker.com/engine/release-notes/26.0/).

## Nghiệm thu cần thực hiện sau khi SSH được

1. Xác nhận ba Consul voter và registration có IP/port đúng VM; lưu hostname, boot ID, container IDs và image/release checksum.
2. Gửi light/heavy/stress qua Envoy; graph phải thấy cả hai replica A/B/C và không có OK nhận sau deadline.
3. Drain B-2 khi transport còn mở; graph chỉ dùng B-1 rồi phục hồi.
4. Dừng từng control plane, xác nhận cập nhật mới và cold caller ở replica còn lại.
5. Dừng dịch vụ Consul leader; kiểm tra health vẫn cập nhật và cụm bầu leader mới. Mất quorum phải rút membership sau lease.
6. Tắt VM node3: B-2/C-2 và một Consul server cùng mất; ingress ở node1 phải phục vụ qua B-1/C-1. Bật VM trở lại và xác nhận tự đăng ký.

Hai ingress không tạo một VIP tự failover. Nếu tắt VM đang chứa ingress mà client đang gọi, kết nối đó sẽ mất; gọi ingress thứ hai là chuyển endpoint có chủ đích, không được báo thành HA tự động của lớp đầu vào.

Chạy nghiệm thu từ Windows qua VPN, với client gọi Floating IP ingress:

```powershell
.\.venv\Scripts\python.exe scripts/acceptance_cloud.py --inventory results/cloud-ssh.json --ssh-config results/cloud-ssh-config --key C:/Users/Khoa/Downloads/nhom15_monitor.pem --archive results/loadbalance-release-v5.tar.gz --address 192.168.120.109:80 --poweroff-node3
```

`--poweroff-node3` lên lịch shutdown node3, xác nhận graph còn B-1/C-1 khi VM mất, rồi ghi `operator_state=node3_off_verified_start_it_in_dashboard` vào report. Người vận hành bật VM lại bằng Dashboard trong vòng 240 giây. Runner kiểm tra boot ID đổi và graph phục hồi; không tuyên bố tự động bật lại VM. Tránh chạy tùy chọn này nếu không có người/agent mở Dashboard để phục hồi.

Báo cáo: `results/cloud-acceptance.json`, log `results/cloud-acceptance.log`. Lab được giữ lại sau thử nghiệm, không tự xóa VM/volume.

## Kết quả nghiệm thu cloud — 27/10/2026

**Đạt `passed=true`, `operator_state=completed`.** Đã xác nhận lại cả ba VM Active/Running, 13 container running, ba Consul voter và không còn lỗi được tiêm. VM cũ vẫn Shutoff, không bị dùng hoặc sửa trong bài thử. Server chuyển image tạm thời đã dừng.

| Phép thử | Bằng chứng |
|---|---|
| Ba máy thực sự độc lập ở mức OS | Hostname và boot ID riêng; cùng checksum release trên ba máy |
| Cùng binary/container ứng dụng | Sáu workload có cùng image ID; hai control plane cũng cùng image ID |
| Graph qua mạng VM | Đủ A-1/A-2, B-1/B-2, C-1/C-2 với placement trong bảng trên |
| Tải nhẹ/nặng/stress | 1.000 + 300 + 5.000 RPC đều OK; không có OK nhận sau timeout client |
| NOT_SERVING rồi SERVING lại | Rút B-2 khỏi EDS và đưa lại vào graph |
| Dừng từng control plane | Cập nhật health mới và cold caller 40 RPC đều đạt ở replica còn lại |
| Dừng Consul leader | Graph phản ánh health mới; runner xác nhận sau 3,719 giây; cụm phục hồi |
| Mất quorum 22 giây | 40/40 UNAVAILABLE do lease hết hạn; phục hồi graph sau unpause |
| Deadline 1 ms | 100/100 DEADLINE_EXCEEDED, rồi graph phục hồi |
| Shutdown VM node3 | Dashboard xác nhận Shutoff; A-1/A-2 vẫn trả graph qua B-1/C-1 |
| Bật lại VM node3 | Boot ID thay đổi; Docker tự khởi động các container; B-2/C-2 trở lại graph |
| Chất lượng code local | 162 pytest đạt; Ruff lint/format đạt, 56 file Python được quản lý |

Các số `converged_in_s` bao gồm thời gian runner xác nhận bằng 24 RPC nối tiếp, không phải số đo chính xác thời điểm mọi caller nhận EDS. Sau khi xác nhận VM mất liên lạc, kiểm tra graph thiếu node3 đạt sau 5,125 giây; sau khi SSH thấy boot ID mới, graph đầy đủ đạt sau 4,438 giây. Không suy ra không có lỗi chuyển tiếp trong lúc mất node. Đây là bằng chứng ba VM cùng availability zone; chưa kiểm chứng lỗi hypervisor/zone và chưa có VIP failover tự động cho ingress.

Endpoint gRPC còn chạy qua VPN: `192.168.120.109:80`. Đối chiếu toàn bộ trạng thái cuối và checksum trong `results/cloud-deployment-status.json`; kiểm thử cloud không được gộp với các con số local/WSL trước đó.
