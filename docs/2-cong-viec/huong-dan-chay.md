# Hướng dẫn chạy

Hệ thống chạy trên **ba VM của cloud UIT-IoT**. Các script chạy trên **máy của bạn**: bộ phát tải gửi request qua VPN tới LB của cloud, còn thao tác gây lỗi được gửi tới VM qua SSH.

```text
Máy của bạn                                Cloud UIT-IoT
┌───────────────────────┐   VPN    ┌──────────────────────────────┐
│ run_faults / run_load │ ───────► │ LB 192.168.120.109:80        │
│  - bộ phát tải        │          │   → Envoy node1 / node2      │
│  - gây lỗi qua SSH    │ ───────► │ node1, node2, node3 (Docker) │
└───────────────────────┘   SSH    └──────────────────────────────┘
```

Mọi lệnh chạy trong PowerShell, từ thư mục gốc của repo. Mỗi lượt tạo một thư mục mới dưới `results/`; **không xóa hay sửa** thư mục kết quả, kể cả lượt không đạt.

## 1. Chuẩn bị máy

```powershell
git pull
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```

Kết quả mong đợi: mọi test đạt.

Kết nối cloud:

1. Bật VPN UIT-IoT.
2. Chép file khóa SSH của nhóm thành `C:\Users\<tên-bạn>\.ssh\uitiot-lab.pem`. Không cần sửa file cấu hình nào.
3. Kiểm tra:

```powershell
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json check
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json status
```

Kết quả mong đợi: `check` báo `true` cho cả ba node; `status` liệt kê 14 thành phần đều `running`. Node nào `false` thì lý do in ngay bên dưới.

## 2. Thử tay để hiểu hệ thống

Làm từng lệnh, quan sát rồi mới chạy lệnh sau:

```powershell
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json stop b-2
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json start b-2
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json partition --a node3 --b node1 node2
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json degrade node3 --delay-ms 100
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json heal
```

Xem log control plane để thấy nó phát hiện và rút endpoint:

```powershell
ssh -F configs/ssh-config lb-node1 "sudo docker compose -p lb-cloud -f /opt/loadbalance/cluster/node1/compose.json logs --tail 20 controlplane"
```

Câu nên tự trả lời được: khi b-2 dừng, ai phát hiện, sau bao lâu, và A biết bằng cách nào?

### Ghi dự đoán trước khi đo

Viết dự đoán vào mục 1 trong file báo cáo của mình và commit **trước** khi có kết quả. Dự đoán sai không sao: chỗ lệch giữa dự đoán và số đo là phần đáng viết nhất.

## 3. Chạy thử

Trước lượt chính thức, chạy hai bài ngắn (mỗi bài khoảng 2 phút):

```powershell
.\.venv\Scripts\python.exe scripts/run_faults.py --env configs/lab.cloud.json --scenarios backend-stop --repetitions 1
.\.venv\Scripts\python.exe scripts/run_faults.py --env configs/lab.cloud.json --scenarios partition-node3 --repetitions 1
```

## 4. Các lượt chạy

Mọi lượt trên cụm phải chạy trên **bản baseline** (xem [Baseline chung](#baseline-chung)).

**D1 — lỗi cứng (Đạt):**

```powershell
.\.venv\Scripts\python.exe scripts/run_faults.py --env configs/lab.cloud.json --repetitions 5 --scenarios backend-stop controlplane-stop consul-leader quorum-loss node3-down node1-down partition-node3 partition-node1
```

**D2 — RL trên gRPC thật, máy cục bộ (Đạt).** Không dùng cụm. Đóng các chương trình nặng khác; mỗi lúc chỉ chạy một lượt.

```powershell
.\.venv\Scripts\python.exe scripts/compare_shape_rl.py --model configs/shape-model.json --output-dir results/shape-compare-3
```

**L1 — bài tải (Lầu):**

```powershell
.\.venv\Scripts\python.exe scripts/run_load.py --env configs/lab.cloud.json --rates 50 100 200 400 --repetitions 3
```

**L2 — lỗi mềm (Lầu):**

```powershell
.\.venv\Scripts\python.exe scripts/run_faults.py --env configs/lab.cloud.json --repetitions 5 --scenarios delay-node3 loss-node3
```

**L3 — bài tải có RL (Lầu):**

```powershell
.\.venv\Scripts\python.exe scripts/run_load.py --env configs/lab.cloud.json --variants xds xds-beta xds-rl static-lr --rates 50 100 200 --repetitions 3 --settle 90
```

`--settle 90` là bắt buộc khi có `xds-rl` hoặc `xds-beta`: trọng số mỗi cửa sổ 10 giây chỉ dịch tối đa 10 điểm, nên cần thời gian để chúng tới đích trước khi đo.

Nếu lượt chạy bị ngắt giữa chừng:

```powershell
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json heal
```

### Dùng chung cụm

- **Mỗi lúc chỉ một người chạy.** `run_faults.py` và `run_load.py` tự giữ khóa cụm; người khác chạy cùng lúc sẽ bị từ chối và được báo ai đang dùng.
- Xem ai đang dùng: `labenv.py --env configs/lab.cloud.json lock-status`.
- Khóa bị bỏ lại (máy người chạy tắt giữa chừng): **hỏi người đó trước**, rồi mới `unlock` và `heal`.
- Báo nhóm trước khi triển khai code mới: triển khai khởi động lại toàn bộ cụm.

### Baseline chung

Mọi bảng "trước" phải đo trên cùng một bản code, nếu không số của người này lẫn bản sửa của người kia.

1. Sau khi triển khai bản `main`, Khoa gắn tag `lab-baseline` và báo nhóm giá trị `release` (mã băm của bản đang chạy trên cụm, nằm trong `run.json` của mọi lượt).
2. **Chưa ai triển khai bản sửa cho tới khi D1, L1, L2, L3 xong.**
3. Trước khi dùng một thư mục kết quả, kiểm tra `release` trong `run.json` của nó trùng mã baseline. `git_commit` trong `run.json` là commit trên máy người chạy, không phải bản trên cụm.

## 5. Kiểm tra và đánh giá

Mỗi lượt tạo một thư mục, ví dụ `results/lab-faults/20261113-210000/`:

| File | Nội dung |
|---|---|
| `report.md` | Bảng tổng hợp và kết luận đạt / không đạt |
| `report.json` | Số liệu từng lần lặp |
| `*.jsonl` | Từng request: thời điểm, mã kết quả, latency, instance đã phục vụ |
| `*.events.json` | Mốc gây lỗi và khôi phục |
| `run.json` | Tham số, commit, `release` của từng VM |

**Kiểm tra.** Đừng chỉ đọc `report.md`. Chọn một lần lặp, mở `*.jsonl` và `*.events.json`, tự đếm lại số request lỗi quanh mốc gây lỗi (bài lỗi) hoặc tỷ lệ request do b-2 phục vụ (bài tải), rồi so với bảng.

Dựng lại bảng từ dữ liệu có sẵn, không chạy lại:

```powershell
.\.venv\Scripts\python.exe scripts/run_faults.py --report results/lab-faults/<thư-mục>
.\.venv\Scripts\python.exe scripts/run_load.py --report results/lab-load/<thư-mục>
```

**Đánh giá.** Với mỗi ô, viết: đạt hay không theo [tiêu chí](tra-cuu.md); con số chính; và **vì sao** hệ thống hành xử như vậy, dựa vào kiến trúc. Đặt số đo cạnh dự đoán; ô nào lệch thì giải thích. Ô không đạt thì phân loại trước khi sửa: lỗi hệ thống, lỗi công cụ đo, hay tiêu chí chưa hợp lý.

**Sửa một ô (cặp trước / sau).**

1. Xác định hiện tượng: trong `report.json`, xem loại lỗi và instance còn phục vụ.
2. Tìm thời điểm: lọc request lỗi trong `*.jsonl`, so với mốc trong `*.events.json`.
3. Đọc log thành phần liên quan quanh thời điểm đó.
4. Đặt một giả thuyết, sửa một thứ trên nhánh riêng, chạy `pytest`.
5. Báo nhóm, triển khai (mục 6), chạy lại riêng tình huống đó với cùng số lần lặp.
6. Ghi cặp trước / sau vào file báo cáo. Bản sửa tốt hơn thì mở pull request vào `main`; không thì triển khai lại `main`. Cả hai trường hợp đều báo nhóm giá trị `release` mới.

**Giới hạn một buổi cho mỗi ô** (3–4 giờ). Hết giới hạn mà chưa cải thiện thì dừng, ghi lại hiện tượng, các giả thuyết đã thử và số đo của từng lần thử. Đó vẫn là một cặp trước / sau hợp lệ.

## 6. Triển khai code lên cụm

Báo nhóm trước. Lệnh tự từ chối khi có người đang chạy.

```powershell
.\.venv\Scripts\python.exe scripts/multihost.py render --nodes 192.168.1.238 192.168.1.172 192.168.1.217 --ingress-port 18080 --output results/cloud-cluster
.\.venv\Scripts\python.exe scripts/multihost.py bundle --cluster results/cloud-cluster --output results/loadbalance-release.tar.gz
.\.venv\Scripts\python.exe scripts/multihost.py deploy --inventory configs/cloud-inventory.json --ssh-config configs/ssh-config --key $HOME\.ssh\uitiot-lab.pem --archive results/loadbalance-release.tar.gz
```

`deploy` build image trên từng VM nên mất vài phút. Sau khi xong, chạy `check` và `status`.

Gắn tag baseline (chỉ Khoa, sau lần triển khai đầu):

```powershell
git tag lab-baseline
git push origin lab-baseline
```

## 7. Khi gặp lỗi công cụ, báo kèm

- Lệnh đã chạy, nguyên văn.
- Phần in ra cuối cùng (traceback hoặc thông báo lỗi).
- Đường dẫn thư mục kết quả nếu đã tạo.
- Kết quả của `labenv.py --env configs/lab.cloud.json check`.
