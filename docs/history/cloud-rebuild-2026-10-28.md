# Cụm triển khai lại ngày 28/10/2026

Người dùng đã xóa ba VM thử nghiệm ngày 27/10 và cho phép dùng ba VM k3s hiện có. Các kết quả cũ trong `results/cloud-*` là bằng chứng lịch sử, không mô tả cụm hiện tại.

| Node | VM | IP nội bộ | Thành phần LoadBalance |
|---|---|---|---|
| node1 | k3s-server | 192.168.1.238 | Envoy, A1, B1, control plane, Consul |
| node2 | k3s-agent1 | 192.168.1.172 | Envoy, A2, C1, control plane, Consul |
| node3 | k3s-agent2 | 192.168.1.217 | B2, C2, Consul |

VM1 có 2 vCPU/4 GB RAM, VM2 và VM3 mỗi máy 1 vCPU/2 GB RAM; Ubuntu 20.04, cùng az-03. Chưa xác minh tách hypervisor. Cụm dùng chung tài nguyên với k3s nên số đo không đại diện cho máy chuyên dụng. Trước triển khai, ba Kubernetes node đều Ready; metrics-server có pod Running nhưng readiness 0/1 từ trước.

Ingress Envoy nghe cổng **18080** để tránh ingress k3s. Port admin chỉ bind loopback 9901. Service dùng các cổng 50101/50102, 50201/50202, 50301/50302; xDS 18000. Floating IP hiện có của VM1 là 192.168.121.214. SSH node2/3 đi qua ProxyJump trên VM1, private key ở Windows.

## Tái lập triển khai

```powershell
.venv\Scripts\python.exe scripts/multihost.py render --nodes 192.168.1.238 192.168.1.172 192.168.1.217 --ingress-port 18080 --output results/k3s-cluster
.venv\Scripts\python.exe scripts/multihost.py bundle --cluster results/k3s-cluster --output results/loadbalance-k3s-release.tar.gz
.venv\Scripts\python.exe scripts/multihost.py deploy --inventory results/k3s-ssh.json --ssh-config results/k3s-ssh-config --key C:/Users/Khoa/Downloads/nhom15_monitor.pem --archive results/loadbalance-k3s-release.tar.gz --prebuilt
```

`--prebuilt` yêu cầu nạp image trước. Lần này build trên node1 rồi chuyển archive image nội bộ, SHA-256 `0f9df1585053b9936f0565bddca4cc1ecdd7546f169db4cb9d69de21aa738c14`. Image archive được kiểm checksum trên cả hai node nhận. HTTP chuyển image bind IP nội bộ cổng 8020, đã dừng sau khi chuyển. Release triển khai SHA-256 `80857261bfb6b4d962650bdf767bf140320aff6fe17248aa697732ef652dc963`.

DNS DHCP 8.8.8.8 không trả bản ghi A ổn định trong lần triển khai này. Đã kiểm tra truy vấn trực tiếp 1.1.1.1 hoạt động rồi đặt DNS runtime bằng `sudo resolvectl dns ens3 1.1.1.1`; thay đổi này không tồn tại qua reboot. Không tắt kiểm tra TLS hay thay địa chỉ registry trong `/etc/hosts`.

## Kiểm thử và truy cập hiện tại

Để truy cập thử nghiệm mà chưa mở thêm cổng security group:

```powershell
ssh -F results/k3s-ssh-config -N -L 127.0.0.1:17080:192.168.1.238:18080 ubuntu@192.168.121.214
```

Giữ terminal trên, ở terminal khác:

```powershell
.venv\Scripts\python.exe scripts/check_microservices.py --address 127.0.0.1:17080 --topology results/k3s-cluster/topology.json --requests 60
.venv\Scripts\python.exe scripts/acceptance_cloud.py --inventory results/k3s-ssh.json --ssh-config results/k3s-ssh-config --key C:/Users/Khoa/Downloads/nhom15_monitor.pem --cluster results/k3s-cluster --archive results/loadbalance-k3s-release.tar.gz --address 127.0.0.1:17080 --report results/k3s-acceptance.json
```

Tunnel này phụ thuộc VM1, **không phải bằng chứng HA đầu vào**. Không dùng nó để kết luận client vẫn truy cập được khi mất VM1. Test mặc định chỉ gây lỗi tiến trình LoadBalance; không shutdown VM dùng chung với k3s.

## HA đầu vào

Load balancer `lb-ha-entry`, UUID `62877bb9-c24d-4f24-8f57-cb6f4491ece7`, VIP `192.168.1.157`, provider OVN, đã gắn Floating IP đang rảnh `192.168.120.109`. Theo [tài liệu OVN chính thức](https://docs.openstack.org/ovn-octavia-provider/2024.1/admin/driver.html), provider chỉ hỗ trợ thuật toán `SOURCE_IP_PORT`; dashboard không có lựa chọn này nên phải dùng API Octavia v2 với Application Credential tạm đã được người dùng cho phép. `scripts/cloud_api.py` lấy endpoint từ service catalog, giữ kiểm tra TLS và không log secret/token.

Đã cấu hình: client → `192.168.120.109:80` → OVN TCP listener → pool `192.168.1.238:18080` và `192.168.1.172:18080` → Envoy → A → B/C. Health TCP mỗi 2 giây, timeout 1 giây, ngưỡng lỗi/thành công 2 lần. API xác nhận listener/pool/monitor và hai member ONLINE, ACTIVE; snapshot ở `results/k3s-ingress-status.json`. Kiểm tra 40 request trực tiếp qua Floating IP đã đi qua cả A1/A2, B1/B2, C1/C2. Bằng chứng VM1 SHUTOFF và phục hồi phải xem báo cáo fault riêng, không suy ra từ ONLINE.

VPN hiện được cloud nhìn thấy qua NAT source `192.168.120.11` (đọc từ SSH_CONNECTION), khác IP adapter VPN trên Windows. Đã thêm rule ingress TCP 18080 chỉ từ `192.168.120.11/32` vào security group default để gói qua LB tới Envoy được nhận; rule UUID `ff7dbbaf-4764-4a21-a2b4-e8cbe6b9830f`. Các rule sẵn có giữ nguyên. Nếu gateway VPN thay đổi, phải kiểm tra source thực tế và điều chỉnh rule. Không cần mở các port dịch vụ nội bộ ra mạng client.

Protocol thí nghiệm, tiêu chí và lệnh demo nằm trong [thực nghiệm đối chứng](controlled-experiments.md).

Để tạo lại listener/pool trên LB đã có, cài operator dependencies bằng `python -m pip install -e ".[cloud]"`, cung cấp `clouds.yaml` ngoài repository rồi chạy:

```powershell
.venv\Scripts\python.exe scripts/cloud_api.py --credentials C:/Users/Khoa/Downloads/clouds.yaml --loadbalancer 62877bb9-c24d-4f24-8f57-cb6f4491ece7 --members 192.168.1.238 192.168.1.172 --output results/my-ingress-status.json
```

Script dùng lại tài nguyên cùng tên, từ chối member/thuật toán không khớp và không xóa tài nguyên khác. Floating IP và rule backend là hạ tầng chuẩn bị riêng: gắn FIP `19f6816e-4ea2-40ab-a522-e43041e9c0d1` vào VIP port `fb36b0d7-6e7b-426d-b30e-bef166dc59c5`, cho TCP 18080 từ source VPN thực tế tới hai Envoy. Khi cấp LB mới, phải thay các UUID trong cấu hình/demo; không tái sử dụng UUID sau khi xóa tài nguyên.
