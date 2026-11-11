# Hướng dẫn từng bước cho nhóm

Hệ thống chạy trên **ba VM của cloud UIT-IoT** (node1, node2, node3). Các script chạy trên **máy Windows của bạn**: bộ phát tải gửi request qua VPN tới địa chỉ LB cloud, còn các thao tác gây lỗi được gửi tới VM qua SSH.

```text
Máy của bạn (Windows)                      Cloud UIT-IoT
┌───────────────────────┐   VPN    ┌──────────────────────────────┐
│ run_faults / run_load │ ───────► │ LB 192.168.120.109:80        │
│  - bộ phát tải        │          │   → Envoy node1 / node2      │
│  - gây lỗi qua SSH    │ ───────► │ node1, node2, node3 (Docker) │
└───────────────────────┘   SSH    └──────────────────────────────┘
```

Mọi lệnh chạy từ thư mục gốc của repo trong PowerShell. Kết quả mỗi lượt nằm trong một thư mục mới dưới `results/`; **không xóa hay sửa** thư mục kết quả, kể cả lần không đạt.

## 1. Chuẩn bị máy (mỗi người, một lần)

```powershell
git pull
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```

Kết quả mong đợi: tất cả test đạt.

Kết nối cloud:

1. Bật VPN UIT-IoT.
2. Đặt file khóa SSH của nhóm lên máy, rồi sửa dòng `IdentityFile` trong `results/k3s-ssh-config` cho đúng đường dẫn trên máy bạn.
3. Kiểm tra:

```powershell
ssh -F results/k3s-ssh-config 192.168.121.214 hostname
```

node2 và node3 không có IP công khai; SSH tới chúng đi qua node1 (đã cấu hình sẵn trong file trên).

## 2. Triển khai code lên VM (một người làm, mỗi khi code đổi)

VM đang chạy bản ngày 28/10. Bản mới có thêm phương án Least Request và việc gán lỗi theo endpoint, nên cần triển khai lại:

```powershell
.\.venv\Scripts\python.exe scripts/multihost.py render --nodes 192.168.1.238 192.168.1.172 192.168.1.217 --ingress-port 18080 --output results/k3s-cluster
.\.venv\Scripts\python.exe scripts/multihost.py bundle --cluster results/k3s-cluster --output results/loadbalance-k3s-release.tar.gz
.\.venv\Scripts\python.exe scripts/multihost.py deploy --inventory results/k3s-ssh.json --ssh-config results/k3s-ssh-config --key <đường-dẫn-file-khóa> --archive results/loadbalance-k3s-release.tar.gz
```

Lệnh cuối build image trên từng VM nên mất vài phút (VM chỉ 1–2 vCPU). Nếu build lỗi vì không tải được gói Python, VM đó đang lỗi DNS: SSH vào VM và chạy `sudo resolvectl dns ens3 1.1.1.1`, rồi chạy lại lệnh `deploy`.

## 3. Cấu hình môi trường (một lần)

```powershell
Copy-Item configs/lab.cloud.example.json configs/lab.cloud.json
```

File này cho script biết: địa chỉ LB (`address`), file SSH (`ssh_config`), và với từng node là địa chỉ SSH, IP nội bộ, và service nào chạy trên node đó. Chỉ sửa nếu IP hoặc đường dẫn khác.

Kiểm tra:

```powershell
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json check
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json status
```

Kết quả mong đợi: `check` báo `true` cho cả ba node; `status` liệt kê 13 thành phần đều `running`. Nếu một node `false`, lý do được in ngay bên dưới.

## 4. Thử tay để hiểu hệ thống (nên làm trước khi đo)

Mỗi bước, quan sát rồi mới chuyển bước sau:

```powershell
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json stop b-2
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json start b-2
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json partition --a node3 --b node1 node2
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json heal
```

Xem log control plane để thấy nó phát hiện và rút endpoint:

```powershell
ssh -F results/k3s-ssh-config 192.168.121.214 "sudo docker compose -p lb-cloud -f /opt/loadbalance/cluster/node1/compose.json logs --tail 20 controlplane"
```

Câu hỏi nên tự trả lời được sau bước này: khi b-2 dừng, ai phát hiện, sau bao lâu, và service A biết bằng cách nào? (Gợi ý: README, mục kiến trúc logic.)

## 5. Lần chạy đầu

Driver điều khiển VM **chưa từng chạy trên VM thật**. Chạy hai bài ngắn này trước, dừng ở bước đầu tiên lỗi:

```powershell
.\.venv\Scripts\python.exe scripts/run_faults.py --env configs/lab.cloud.json --scenarios backend-stop --repetitions 1
.\.venv\Scripts\python.exe scripts/run_faults.py --env configs/lab.cloud.json --scenarios partition-node3 --repetitions 1
```

## 6. Chạy chính thức

Hai bài dưới dùng chung cụm, **không chạy cùng lúc**.

**Đạt — bài lỗi và phân mảnh** (10 tình huống × 5 lần, khoảng 2 giờ):

```powershell
.\.venv\Scripts\python.exe scripts/run_faults.py --env configs/lab.cloud.json --repetitions 5
```

**Lầu — bài tải** (khoảng 75 phút):

```powershell
.\.venv\Scripts\python.exe scripts/run_load.py --env configs/lab.cloud.json --rates 50 100 200 400 --repetitions 3
```

Nếu mọi mức RPS đều đạt, chưa thấy điểm bão hòa: chạy thêm với mức cao hơn (ví dụ `--rates 600 800 1200`).

**Khoa — RL nhiều seed** (chạy trên máy cục bộ, không dùng cloud):

```powershell
.\.venv\Scripts\python.exe scripts/compare_rl_scenarios.py --model results/rl-convergence-balanced-20261101/model-7.json --output-dir results/rl-multiseed --seeds 41 42 43 44 45
```

Nếu một lượt bị ngắt giữa chừng, chạy `labenv.py --env configs/lab.cloud.json heal` để gỡ luật mạng còn sót. Dù quên, các luật này cũng tự hết hạn sau 15 phút.

## 7. Đọc kết quả

Mỗi lượt tạo một thư mục, ví dụ `results/lab-faults/20261112-210000/`:

| File | Nội dung |
|---|---|
| `report.md` | Bảng tổng hợp và kết luận đạt / không đạt |
| `report.json` | Số liệu từng lần lặp |
| `*.jsonl` | Từng request: thời điểm, mã kết quả, latency, instance đã phục vụ |
| `*.events.json` | Mốc gây lỗi và khôi phục |
| `run.json` | Tham số, commit git, cấu hình từng VM |

Bảng bài lỗi: **gián đoạn** là thời gian từ request lỗi đầu tiên đến khi hệ thống ổn định lại; **instance còn phục vụ** cho biết tải đã chuyển sang đâu. Bảng bài tải: **mức tải cao nhất còn đạt** là điểm bão hòa; **phần b-2** cho biết có né backend chậm không (Round Robin ≈ 50%).

Dựng lại bảng từ dữ liệu có sẵn, không chạy lại:

```powershell
.\.venv\Scripts\python.exe scripts/run_faults.py --report results/lab-faults/<thư-mục>
.\.venv\Scripts\python.exe scripts/run_load.py --report results/lab-load/<thư-mục>
```

## 8. Khi một ô không đạt

1. **Xác định hiện tượng**: trong `report.json`, xem `error_codes_in_fault` (loại lỗi) và `instances_serving_in_fault` (ai còn phục vụ).
2. **Tìm thời điểm**: trong `*.jsonl`, lọc request lỗi theo `scheduled_s`; so với mốc trong `*.events.json`.
3. **Đọc log** thành phần liên quan quanh thời điểm đó (lệnh `logs` như mục 4, đổi node và tên service).
4. **Đặt giả thuyết, sửa một thứ**, triển khai lại (mục 2), rồi chạy lại **riêng tình huống đó** (`--scenarios <tên>`) với cùng số lần lặp.
5. **Ghi cặp trước / sau** vào báo cáo; giữ cả hai thư mục kết quả.

Ví dụ để luyện: trong lần chạy thử, sau khi gỡ phân mảnh node1, service a-1 mất khoảng 2 phút mới phục vụ lại. Manh mối nằm trong mục "Vấn đề đã biết" của [hướng dẫn chạy](running.md).

## 9. Khi gặp lỗi công cụ, báo kèm

- Lệnh đã chạy, nguyên văn.
- Phần in ra cuối cùng (traceback hoặc thông báo lỗi).
- Đường dẫn thư mục kết quả nếu đã tạo.
- Kết quả của `labenv.py --env configs/lab.cloud.json check`.

## Phụ lục: chạy thử trên máy cá nhân

Khi cần thử một thay đổi code mà chưa muốn triển khai lên cloud, có thể dựng cùng 13 thành phần trên một máy bằng Docker trong WSL: bỏ `--env ...`, thay bằng `--wsl Ubuntu-22.04` (chi tiết ở [hướng dẫn chạy](running.md)). Số liệu từ cách này **không dùng cho báo cáo**.
