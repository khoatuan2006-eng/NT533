# Hướng dẫn từng bước cho nhóm

Tài liệu này đi theo thứ tự làm việc: chuẩn bị máy → chạy trên lab một máy → triển khai và chạy trên ba VM → đọc kết quả → xử lý khi không đạt. Chi tiết từng tham số nằm trong [hướng dẫn chạy](running.md).

Mọi lệnh chạy từ thư mục gốc của repo trong PowerShell. Kết quả mỗi lượt chạy nằm trong một thư mục mới dưới `results/`; **không xóa hay sửa** thư mục kết quả, kể cả lần không đạt.

## 1. Chuẩn bị máy (mỗi người, một lần)

```powershell
git pull
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```

Kết quả mong đợi: tất cả test đạt. Nếu có test lỗi, dừng lại và báo trước khi chạy thí nghiệm.

Lab một máy cần Docker trong WSL (Ubuntu). Kiểm tra:

```powershell
wsl -d Ubuntu-22.04 -- docker version
```

Nếu báo `Wsl/Service/E_UNEXPECTED`: chạy `wsl --shutdown` rồi thử lại; vẫn lỗi thì khởi động lại Windows.

## 2. Chạy trên lab một máy

### 2.1. Dựng lab

Mở **terminal 1** và để lệnh này chạy suốt buổi làm việc (giữ WSL không tự tắt):

```powershell
.\.venv\Scripts\python.exe scripts/labenv.py --wsl Ubuntu-22.04 hold
```

Ở **terminal 2**:

```powershell
.\.venv\Scripts\python.exe scripts/labenv.py --wsl Ubuntu-22.04 check
.\.venv\Scripts\python.exe scripts/labenv.py --wsl Ubuntu-22.04 up --variant xds --build
.\.venv\Scripts\python.exe scripts/labenv.py --wsl Ubuntu-22.04 status
```

Kết quả mong đợi: `check` báo `true` cho mọi mục; `status` liệt kê 13 container đều `running`. Lần đầu `--build` mất vài phút.

### 2.2. Thử tay để hiểu hệ thống (nên làm trước khi đo)

Mỗi bước, quan sát hệ thống phản ứng thế nào rồi mới chuyển bước sau:

```powershell
.\.venv\Scripts\python.exe scripts/labenv.py --wsl Ubuntu-22.04 stop b-2           # dừng một backend
.\.venv\Scripts\python.exe scripts/labenv.py --wsl Ubuntu-22.04 start b-2
.\.venv\Scripts\python.exe scripts/labenv.py --wsl Ubuntu-22.04 partition --a node3 --b node1 node2
.\.venv\Scripts\python.exe scripts/labenv.py --wsl Ubuntu-22.04 heal
```

Xem log control plane để thấy nó phát hiện và rút endpoint (chạy trong WSL):

```powershell
wsl -d Ubuntu-22.04 -u root -- docker logs --tail 20 lb-lab-controlplane-1-1
```

Câu hỏi nên tự trả lời được sau bước này: khi b-2 dừng, ai phát hiện, sau bao lâu, và A biết bằng cách nào? (Gợi ý: README, mục kiến trúc logic.)

### 2.3. Chạy chính thức

**Đạt — bài lỗi** (khoảng 2 giờ; nên chạy buổi tối):

```powershell
.\.venv\Scripts\python.exe scripts/run_faults.py --wsl Ubuntu-22.04 --repetitions 5
```

**Lầu — bài tải** (khoảng 75 phút; không chạy cùng lúc với bài lỗi):

```powershell
.\.venv\Scripts\python.exe scripts/run_load.py --wsl Ubuntu-22.04 --rates 50 100 200 400 --repetitions 3
```

Nếu mọi mức RPS đều đạt, chưa thấy điểm bão hòa: chạy thêm với mức cao hơn (ví dụ `--rates 600 800 1200`).

**Khoa — RL nhiều seed** (không cần Docker; chạy khi lab rảnh để không tranh CPU):

```powershell
.\.venv\Scripts\python.exe scripts/compare_rl_scenarios.py --model results/rl-convergence-balanced-20261101/model-7.json --output-dir results/rl-multiseed --seeds 41 42 43 44 45
```

Trong lúc chạy: không chạy việc nặng CPU khác trên cùng máy, không tắt terminal `hold`.

## 3. Chạy trên ba VM cloud

### 3.1. Kết nối

Bật VPN của nhóm. File khóa phải ở đường dẫn ghi trong `results/k3s-ssh-config` (hiện là `C:/Users/Khoa/Downloads/nhom15_monitor.pem`); máy khác thì sửa dòng `IdentityFile`. Kiểm tra:

```powershell
ssh -F results/k3s-ssh-config 192.168.121.214 hostname
```

### 3.2. Triển khai code mới lên VM

VM đang chạy bản ngày 28/10, chưa có `STATIC_LB_POLICY` và trailer `x-instance-id`. Triển khai lại:

```powershell
.\.venv\Scripts\python.exe scripts/multihost.py render --nodes 192.168.1.238 192.168.1.172 192.168.1.217 --ingress-port 18080 --output results/k3s-cluster
.\.venv\Scripts\python.exe scripts/multihost.py bundle --cluster results/k3s-cluster --output results/loadbalance-k3s-release.tar.gz
.\.venv\Scripts\python.exe scripts/multihost.py deploy --inventory results/k3s-ssh.json --ssh-config results/k3s-ssh-config --key C:/Users/Khoa/Downloads/nhom15_monitor.pem --archive results/loadbalance-k3s-release.tar.gz
```

Lệnh cuối build image trên từng VM nên chậm (VM 1–2 vCPU). Nếu build lỗi vì không tải được gói Python, VM đang lỗi DNS: SSH vào VM đó và chạy `sudo resolvectl dns ens3 1.1.1.1` rồi chạy lại (xem [ghi chú triển khai 28/10](cloud-rebuild-2026-10-28.md)).

### 3.3. Cấu hình môi trường lab

```powershell
Copy-Item configs/lab.cloud.example.json configs/lab.cloud.json
```

Sửa `configs/lab.cloud.json` nếu IP hoặc đường dẫn khác: `address` là địa chỉ LB cloud, `ssh_config` là file SSH, mỗi node có `ssh` (địa chỉ để SSH tới), `ip` (IP nội bộ) và `services` (tên trong lab → tên service Compose trên VM).

### 3.4. Lần chạy đầu trên cloud

Driver ba VM **chưa từng chạy trên VM thật**. Chạy đúng thứ tự, dừng ở bước đầu tiên lỗi:

```powershell
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json check
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json status
.\.venv\Scripts\python.exe scripts/run_faults.py --env configs/lab.cloud.json --scenarios backend-stop --repetitions 1
.\.venv\Scripts\python.exe scripts/run_faults.py --env configs/lab.cloud.json --scenarios partition-node3 --repetitions 1
```

Sau đó chạy bộ đầy đủ như mục 2.3, thêm `--env configs/lab.cloud.json` (bỏ `--wsl`).

An toàn: luật mạng tự hết hạn sau 15 phút; nếu một lượt bị ngắt giữa chừng, chạy `labenv.py --env configs/lab.cloud.json heal`. Không dùng `down` trên cloud (bị chặn có chủ đích).

## 4. Đọc kết quả

Mỗi lượt tạo một thư mục, ví dụ `results/lab-faults/20261112-210000/`:

| File | Nội dung |
|---|---|
| `report.md` | Bảng tổng hợp và kết luận đạt / không đạt |
| `report.json` | Số liệu từng lần lặp, dùng khi cần đào sâu |
| `*.jsonl` | Từng request: thời điểm, mã kết quả, latency, instance đã phục vụ |
| `*.events.json` | Mốc gây lỗi và khôi phục |
| `run.json` | Tham số, commit git, cấu hình máy |

Cách đọc bảng bài lỗi: **gián đoạn** là thời gian từ request lỗi đầu tiên đến khi hệ thống ổn định trở lại; **instance còn phục vụ** cho biết hệ thống đã chuyển tải sang đâu. Bảng bài tải: **mức tải cao nhất còn đạt** là điểm bão hòa; **phần b-2** cho biết có né backend chậm không (Round Robin ≈ 50%).

Dựng lại bảng từ dữ liệu có sẵn (không chạy lại):

```powershell
.\.venv\Scripts\python.exe scripts/run_faults.py --report results/lab-faults/<thư-mục>
.\.venv\Scripts\python.exe scripts/run_load.py --report results/lab-load/<thư-mục>
```

## 5. Khi một ô không đạt

1. **Xác định hiện tượng**: trong `report.json`, xem `error_codes_in_fault` (loại lỗi) và `instances_serving_in_fault` (ai còn phục vụ).
2. **Tìm thời điểm**: trong `*.jsonl`, lọc các request lỗi theo `scheduled_s`; so với mốc trong `*.events.json`.
3. **Đọc log** thành phần liên quan quanh thời điểm đó (`docker logs` cho control plane, service A, Consul).
4. **Đặt giả thuyết, sửa một thứ**, rồi chạy lại **riêng tình huống đó** (`--scenarios <tên>`) với cùng số lần lặp.
5. **Ghi cặp trước / sau** vào báo cáo; giữ cả hai thư mục kết quả.

Ví dụ đã biết để luyện: `partition-node1` phục hồi chậm khoảng 2 phút sau khi gỡ phân mảnh. Manh mối nằm trong mục "Vấn đề đã biết" của [hướng dẫn chạy](running.md).

## 6. Khi gặp lỗi công cụ, báo kèm

- Lệnh đã chạy, nguyên văn.
- Phần in ra cuối cùng (traceback hoặc thông báo lỗi).
- Đường dẫn thư mục kết quả nếu đã tạo.
- Đang chạy lab một máy hay cloud, và kết quả của `check`.
