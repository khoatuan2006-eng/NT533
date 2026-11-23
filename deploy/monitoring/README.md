# Hướng Dẫn Giám Sát Cụm Cân Bằng Tải gRPC (Prometheus + Grafana) — Đề Tài NT533

Tài liệu này hướng dẫn chi tiết từ đầu (A-Z) dành cho thành viên mới clone repository về máy: cách cấu hình lại theo địa chỉ IP/Key AWS của riêng bạn, triển khai hệ thống cân bằng tải gRPC 2 tầng, bật hệ thống giám sát Prometheus + Grafana, và thực hiện các kịch bản thử nghiệm tải, tiêm lỗi và đổi thuật toán cân bằng tải (Round Robin, Least Request, Reinforcement Learning).

---

## 1. Kiến Trúc Hệ Thống

Hệ thống được thiết kế chạy trên **4 máy ảo AWS EC2** (`c7i-flex.large`, Ubuntu 24.04, mạng VPC):

```text
                               ┌────────────────────────────────────────────────┐
                               │             MẠNG NỘI BỘ VPC AWS                │
                               │                                                │
[ Khách hàng / Tải ]           │   ┌───────────────┐        ┌───────────────┐   │
       │                       │   │  a-1 (Node 1) │        │  b-1 (Node 1) │   │
       ▼                       │   ├───────────────┤        ├───────────────┤   │
┌──────────────┐               │   │  a-2 (Node 2) │        │  b-2 (Node 2) │   │
│ Envoy-1 (N1) │──(Tầng 1)────┼──►│               │──(Tầng 2)─►             │   │
│ Envoy-2 (N2) │               │   │  a-3 (Node 3) │        │  b-3 (Node 3) │   │
└──────────────┘               │   ├───────────────┤        ├───────────────┤   │
       ▲                       │   │  a-4 (Node 4) │        │  b-4 (Node 4) │   │
       │                       │   └───────────────┘        └───────────────┘   │
       │                       └────────────────────────────────────────────────┘
       │                                                 │ (Scrape metrics 9200)
       │                                                 ▼
[ SSH Tunnel ] ◄────────────────────────────── ┌─────────────────────────────────┐
  localhost:3000 (Grafana)                     │ Node 4: Prometheus & Grafana    │
  localhost:9090 (Prometheus)                  │ Cào toàn bộ Node, Envoy, A, B  │
                                               └─────────────────────────────────┘
```

* **Tầng 1 (Ingress)**: 2 Envoy Ingress (`envoy-1` trên Node 1, `envoy-2` trên Node 2), cổng 80.
* **Tầng 2 (Workload A)**: 4 bản sao `a-1` .. `a-4` (mỗi Node 1 bản sao, cổng 50101-50104).
* **Tầng 2 (Workload B)**: 4 bản sao `b-1` .. `b-4` (mỗi Node 1 bản sao, cổng 50201-50204).
* **Control Plane & Service Discovery**: 3 Consul Servers tạo cụm Raft Quorum (Node 1, 2, 3), 1 Consul Agent (Node 4), 2 xDS Control Plane (Node 1, 2).
* **Monitoring Stack**: Prometheus và Grafana đặt tại **Node 4** (`network_mode: host`) cào số liệu toàn bộ cụm qua IP private nội bộ VPC, sau đó mở SSH tunnel đưa cổng 3000 về `localhost:3000` của bạn.

---

## 2. Chuẩn Bị & Điều Kiện Tiên Quyết

### 2.1. Yêu cầu trên máy tính của bạn (Local)
* Đã cài **Git**, **Python 3.11+**, và **PowerShell** (hoặc Bash shell).
* File khóa SSH AWS (ví dụ: `lb-aws.pem`).
* Các thư viện Python cần thiết:
  ```bash
  pip install requests pyyaml
  ```

### 2.2. Yêu cầu Security Group trên AWS EC2
Đảm bảo Security Group của 4 máy ảo EC2 cho phép:
* **Inbound từ máy tính của bạn**: Cổng `22` (SSH).
* **Inbound nội bộ VPC (`172.31.0.0/16`)**:
  * `80`: Cổng Ingress gRPC.
  * `8300, 8301, 8500`: Consul Raft, Serf LAN và HTTP API.
  * `9100`: Node Exporter (CPU, RAM, Mạng).
  * `9200`: Service A Prometheus Metrics exporter.
  * `9901`: Envoy Ingress Admin / Prometheus Stats.
  * `18000`: xDS Control Plane ADS.
  * `50101 - 50204`: Các cổng backend gRPC của Service A và Service B.

---

## 3. Các Bước Cấu Hình Cho Người Mới (Thay đổi IP & Key)

Khi bạn clone repo về hoặc khi cụm EC2 được khởi động lại (IP Public thay đổi), bạn chỉ cần kiểm tra và chỉnh sửa **4 vị trí** sau:

### Vị trí 1: File cấu hình SSH (`configs/ssh-config.aws-new`)
Mở file `configs/ssh-config.aws-new` và cập nhật:
* Đường dẫn file khóa `IdentityFile` trỏ tới file `.pem` trên máy của bạn.
* `HostName` của `node1`, `node2`, `node3`, `node4` trỏ tới **IP Public** của 4 VM tương ứng:

```sshconfig
Host node1 172.31.20.204
    HostName <IP_PUBLIC_NODE_1>
    User ubuntu
    IdentityFile "C:\duong\dan\den\khoa\lb-aws.pem"

Host node2 172.31.30.73
    HostName <IP_PUBLIC_NODE_2>
    User ubuntu
    IdentityFile "C:\duong\dan\den\khoa\lb-aws.pem"

Host node3 172.31.26.182
    HostName <IP_PUBLIC_NODE_3>
    User ubuntu
    IdentityFile "C:\duong\dan\den\khoa\lb-aws.pem"

Host node4 172.31.18.115
    HostName <IP_PUBLIC_NODE_4>
    User ubuntu
    IdentityFile "C:\duong\dan\den\khoa\lb-aws.pem"
```

### Vị trí 2: File đặc tả cụm (`configs/cluster.aws-4node-4a4b.json`)
Cập nhật trường `ssh` tương ứng với **IP Public** của từng máy:
```json
  "nodes": {
    "node1": { "ip": "172.31.20.204", "ssh": "<IP_PUBLIC_NODE_1>", "vcpus": 2, "instances": ["a-1", "b-1"] },
    "node2": { "ip": "172.31.30.73",  "ssh": "<IP_PUBLIC_NODE_2>", "vcpus": 2, "instances": ["a-2", "b-2"] },
    "node3": { "ip": "172.31.26.182", "ssh": "<IP_PUBLIC_NODE_3>", "vcpus": 2, "instances": ["a-3", "b-3"] },
    "node4": { "ip": "172.31.18.115", "ssh": "<IP_PUBLIC_NODE_4>", "vcpus": 2, "instances": ["a-4", "b-4"] }
  }
```

### Vị trí 3: Cấu hình Prometheus (`deploy/monitoring/prometheus/prometheus.node4.yml`)
Nếu dải IP Private nội bộ của bạn khác `172.31.x.x`, hãy thay đổi tương ứng tại file này (nếu vẫn dùng cụm AWS ban đầu thì giữ nguyên):
* `envoy-ingress`: targets `172.31.20.204:9901`, `172.31.30.73:9901`
* `service-a-metrics`: targets `172.31.20.204:9200`, `172.31.30.73:9200`, `172.31.26.182:9200`, `127.0.0.1:9200`
* `consul-cluster`: targets port `8500`
* `node-exporter`: targets port `9100`

### Vị trí 4: Script khởi chạy (`deploy/monitoring/run-grafana-on-node4.ps1`)
Mở file và cập nhật các giá trị mặc định ở đầu file cho tiện dùng:
```powershell
param(
    [string]$Node4 = "<IP_PUBLIC_NODE_4>",
    [string]$Node1 = "<IP_PUBLIC_NODE_1>",
    [string]$Node2 = "<IP_PUBLIC_NODE_2>",
    [string]$Node3 = "<IP_PUBLIC_NODE_3>",
    [string]$KeyPath = "C:\duong\dan\den\khoa\lb-aws.pem",
    ...
)
```

---

## 4. Các Bước Triển Khai Thực Tế

### Bước 4.1: Render cấu hình và Deploy cụm gRPC lên AWS
Từ thư mục gốc của repository trên máy bạn, chạy:

```powershell
# 1. Sinh cấu hình Docker Compose cho 4 máy
python scripts/multihost.py render --spec configs/cluster.aws-4node-4a4b.json --output results/aws-4node-4a4b

# 2. Đóng gói mã nguồn & cấu hình thành file nén
python scripts/multihost.py bundle --cluster results/aws-4node-4a4b --output results/loadbalance-release.tar.gz

# 3. Đẩy lên 4 VM EC2 và khởi chạy container (Node 1..4)
python scripts/multihost.py deploy --spec configs/cluster.aws-4node-4a4b.json --ssh-config configs/ssh-config.aws-new --key "C:\duong\dan\khoa\lb-aws.pem" --archive results/loadbalance-release.tar.gz

# 4. Xác nhận toàn bộ 4 máy đã sẵn sàng
python scripts/multihost.py verify --spec configs/cluster.aws-4node-4a4b.json --ssh-config configs/ssh-config.aws-new --key "C:\duong\dan\khoa\lb-aws.pem"
```
> Khi màn hình in ra: `Sẵn sàng: 4 node, mọi container chạy, mọi cổng cần thiết đều tới được.` là bạn đã triển khai thành công cụm gRPC!

---

### Bước 4.2: Khởi chạy Monitoring Stack (Prometheus + Grafana)
Chạy script tự động triển khai giám sát lên Node 4:

```powershell
.\deploy\monitoring\run-grafana-on-node4.ps1
```
*(Nếu đã cài Node Exporter từ trước và chỉ muốn đồng bộ lại cấu hình, thêm cờ `-SkipNodeExporter` để chạy nhanh hơn).*

Script sẽ tự động:
1. Cài đặt `node-exporter` lên cả 4 máy EC2 để đo CPU, RAM, Network.
2. Upload cấu hình Prometheus và Dashboard Grafana lên Node 4.
3. Chạy container Prometheus & Grafana bằng Docker trên Node 4.
4. Mở sẵn **SSH Tunnel** kết nối cổng 3000 về máy bạn.

Sau khi hoàn tất, mở trình duyệt truy cập:
👉 **[http://localhost:3000](http://localhost:3000)** (Tài khoản mặc định: `admin` / `admin`).

---

## 5. Hướng Dẫn Đọc Dashboard Grafana (32 Panels)

Dashboard **"Hệ thống cân bằng tải gRPC hai tầng (NT533)"** được tổ chức thành các khu vực rõ ràng:

| Khu vực (Row) | Các Panel chính & Ý nghĩa kỹ thuật |
| :--- | :--- |
| **Tổng quan lưu lượng** | **Tổng thông lượng Ingress (req/s)**: Số request tiếp nhận mỗi giây.<br>**Tỷ lệ thành công (%)**: Hiển thị "Chưa có tải" khi rảnh, 100% khi bình thường, giảm khi có lỗi.<br>**Lưu lượng theo từng cổng Ingress**: So sánh tải giữa Envoy 1 và Envoy 2. |
| **Phân tích độ trễ** | **Độ trễ xử lý (Envoy -> Service A)**: Các mức P50 (trung vị), P90, P95, P99.<br>**Độ trễ P99 theo từng cổng Ingress**: So sánh độ trễ đỉnh giữa 2 Envoy. |
| **Mã phản hồi & Lỗi** | **Trạng thái phản hồi**: Số lượng 2xx (OK), 5xx (Lỗi), và Timeout.<br>**Kết nối hoạt động & Hàng đợi**: Số kết nối TCP đang mở, số request đang in-flight, số request đang xếp hàng chờ. |
| **Trạng thái cụm** | Số lượng backend A Healthy/Unhealthy theo góc nhìn của Envoy.<br>Số máy chủ Consul đang online và số thành viên Raft Quorum (Quorum = 3). |
| **Phân phối lưu lượng Ingress** | Tỷ lệ phần trăm chia tải vào từng cổng Ingress Envoy 1 & Envoy 2.<br>Tốc độ Health Check định kỳ (Success / Failure). |
| **Tài nguyên máy chủ** | Tải **CPU (%)** cả 4 VM EC2 (phát hiện bão hòa khi chạm 90%).<br>Mức sử dụng **RAM (%)** cả 4 node.<br>Băng thông mạng **Network RX (nhận)** và **TX (gửi)** từng node. |
| **Chi tiết tầng 2 (Service A)** | Tốc độ request chuyển tiếp tới Service A, P99 latency theo từng Ingress, số lượng A sẵn sàng (4/4). |
| **Phân phối tải tầng 2 (A -> B)**<br>*(MỚI NHẤT)* | **Tỷ lệ phân phối tải Service B (%)**: Biểu đồ xếp chồng 100% của 4 bản sao `b-1`, `b-2`, `b-3`, `b-4`. Bình thường mỗi dải chiếm 25%. Khi có sự cố, dải bị lỗi co hẹp, các dải khác phình to bù tải.<br>**Lưu lượng gửi tới từng Service B (req/s)**: Tốc độ request tuyệt đối đến mỗi bản sao B.<br>**Ma trận phân phối lưu lượng (A -> B)**: Chi tiết 16 đường từ từng instance A sang từng instance B (`a-X -> b-Y`). |

---

## 6. Các Kịch Bản Thử Nghiệm & Lệnh Mẫu

### Kịch bản 1: Phát tải liên tục để theo dõi thời gian thực
SSH vào **Node 4** và chạy lệnh phát tải (ví dụ 200 req/s trong 180 giây):
```bash
cd /home/ubuntu/lb-gen
.venv/bin/python scripts/scale_probe.py --targets 172.31.20.204:80 172.31.30.73:80 --rates 200 --duration 180 --output results/test-live
```

### Kịch bản 2: Tiêm lỗi làm chậm bản sao `b-2`
Mở PowerShell tại máy cá nhân của bạn và chạy:
```powershell
python scripts/labenv.py --env results/aws-4node-4a4b/lab.json degrade-instance b-2 --delay-ms 100
```
> **Quan sát Grafana**: Xem dải màu của `b-2` co hẹp lại, các bản sao khác tự động gánh tải bù.

### Kịch bản 3: Hồi phục hệ thống (Heal)
```powershell
python scripts/labenv.py --env results/aws-4node-4a4b/lab.json heal
```
> **Quan sát Grafana**: Tỷ lệ của `b-2` tự động hồi phục về mức cân bằng 25%.

---

## 7. Cách Chuyển Đổi Các Thuật Toán Cân Bằng Tải

Hệ thống cho phép bạn chuyển đổi giữa các thuật toán cân bằng tải bất cứ lúc nào chỉ với 1 câu lệnh từ PowerShell (Prometheus và Grafana vẫn chạy bình thường không cần khởi động lại):

### 1. Thuật toán Least Request (`static-lr-ingress`)
Ưu tiên đưa request vào bản sao đang rảnh nhất (ít in-flight request nhất):
```powershell
python scripts/labenv.py --env results/aws-4node-4a4b/lab.json up --variant static-lr-ingress
```

### 2. Thuật toán Học Tăng Cường AI / RL (`xds-rl`)
Sử dụng mô hình Contextual Bandit (`configs/shape-model.json`) để Control Plane tự động tính toán trọng số tải (Weight) dựa trên độ trễ thực tế:
```powershell
python scripts/labenv.py --env results/aws-4node-4a4b/lab.json up --variant xds-rl
```
*Để xem AI ra quyết định theo thời gian thực trên Node 1:*
```bash
sudo docker logs -f lb-cloud-controlplane-1 | grep "rl_window"
```

### 3. Thuật toán Round Robin mặc định (`xds`)
Quay về chế độ xDS luân phiên vòng tròn:
```powershell
python scripts/labenv.py --env results/aws-4node-4a4b/lab.json up --variant xds
```

---

## 8. Dọn Dẹp & Giải Phóng Tài Nguyên

Khi kết thúc đo lường hoặc thử nghiệm:

* **Tắt SSH Tunnel tại máy local**:
  ```powershell
  Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "3000:localhost:3000" } | Stop-Process -Force
  ```
* **Tắt Prometheus & Grafana trên Node 4 (nếu muốn giải phóng RAM/CPU)**:
  ```powershell
  ssh -F configs/ssh-config.aws-new node4 "cd ~/monitoring && sudo docker compose down"
  ```
