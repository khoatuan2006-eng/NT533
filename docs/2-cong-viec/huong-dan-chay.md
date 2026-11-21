# Hướng dẫn chạy

Hệ thống chạy trên **bốn VM AWS EC2** `c7i-flex.large` ở Sydney ([bố trí](../1-do-an/kien-truc.md)). Script triển khai và thử tay chạy trên **máy của bạn** và điều khiển VM qua SSH. Bộ đo (phát tải, gây lỗi trong lúc đo) chạy trên **node4**, trong VPC, vì tài khoản chỉ được 8 vCPU nên chưa có máy phát tải riêng (mục 7.0).

```text
Máy của bạn                        AWS VPC (Sydney)
┌──────────────────────┐   SSH   ┌──────────────────────────────────────┐
│ multihost.py         │ ──────► │ node1, node2: Envoy, control plane   │
│ labenv.py (thử tay)  │         │ node1–node3: Consul server           │
└──────────────────────┘         │ node1–node4: A, B, C (Docker)        │
                                 │ node4: bộ đo → Envoy node1 / node2   │
                                 └──────────────────────────────────────┘
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

Kết nối cụm:

1. Chép file khóa SSH của nhóm vào đúng đường dẫn `IdentityFile` trong `configs/ssh-config.aws-new`.
2. Mỗi lần bật lại VM, IP public đổi: sửa trường `ssh` của từng node trong `configs/cluster.aws-4node.json` (ghép theo IP private, IP private không đổi), rồi sinh lại mô tả cụm:

```powershell
.\.venv\Scripts\python.exe scripts/multihost.py render --spec configs/cluster.aws-4node.json --output results/aws-4node-cluster
$LAB = "results/aws-4node-cluster/lab.json"
.\.venv\Scripts\python.exe scripts/labenv.py --env $LAB check
.\.venv\Scripts\python.exe scripts/labenv.py --env $LAB status
```

Kết quả mong đợi: `check` báo `true` cho cả bốn node; `status` liệt kê 16 thành phần đều `running`. Node nào `false` thì lý do in ngay bên dưới.

## 2. Thử tay để hiểu hệ thống

Làm từng lệnh, quan sát rồi mới chạy lệnh sau:

```powershell
.\.venv\Scripts\python.exe scripts/labenv.py --env $LAB stop b-2
.\.venv\Scripts\python.exe scripts/labenv.py --env $LAB start b-2
.\.venv\Scripts\python.exe scripts/labenv.py --env $LAB partition --a node3 --b node1 node2 node4
.\.venv\Scripts\python.exe scripts/labenv.py --env $LAB degrade node3 --delay-ms 100
.\.venv\Scripts\python.exe scripts/labenv.py --env $LAB degrade-instance b-2 --delay-ms 50
.\.venv\Scripts\python.exe scripts/labenv.py --env $LAB heal
```

Xem log control plane để thấy nó phát hiện và rút endpoint:

```powershell
ssh -F configs/ssh-config.aws-new <IP-public-node1> "sudo docker compose -p lb-cloud -f /opt/loadbalance/cluster/node1/compose.json logs --tail 20 controlplane"
```

Câu nên tự trả lời được: khi b-2 dừng, ai phát hiện, sau bao lâu, và A biết bằng cách nào?

### Ghi dự đoán trước khi đo

Viết dự đoán vào mục 1 trong file báo cáo của mình và commit **trước** khi có kết quả. Dự đoán sai không sao: chỗ lệch giữa dự đoán và số đo là phần đáng viết nhất.

## 3. Chạy thử

Trước lượt chính thức, chạy hai bài ngắn (mỗi bài khoảng 2 phút):

```powershell
.\.venv\Scripts\python.exe scripts/run_faults.py --env $LAB --scenarios backend-stop --repetitions 1
.\.venv\Scripts\python.exe scripts/run_faults.py --env $LAB --scenarios partition-node3 --repetitions 1
```

## 4. Các lượt chạy

Mọi lượt trên cụm phải chạy trên **bản baseline** (xem [Baseline chung](#baseline-chung)).

**D1 — lỗi cứng (Đạt):**

```powershell
.\.venv\Scripts\python.exe scripts/run_faults.py --env $LAB --repetitions 5 --scenarios backend-stop controlplane-stop consul-leader quorum-loss node3-down node1-down partition-node3 partition-node1
```

**D2 — RL trên gRPC thật, máy cục bộ (Đạt).** Không dùng cụm. Đóng các chương trình nặng khác; mỗi lúc chỉ chạy một lượt.

```powershell
.\.venv\Scripts\python.exe scripts/compare_shape_rl.py --model configs/shape-model.json --output-dir results/shape-compare-3
```

**L1 — bài tải (Lầu):**

```powershell
.\.venv\Scripts\python.exe scripts/run_load.py --env $LAB --rates 50 100 200 400 --repetitions 3
```

**L2 — lỗi mềm (Lầu):**

```powershell
.\.venv\Scripts\python.exe scripts/run_faults.py --env $LAB --repetitions 5 --scenarios delay-node3 loss-node3
```

**L3 — bài tải có RL (Lầu):**

```powershell
.\.venv\Scripts\python.exe scripts/run_load.py --env $LAB --variants xds xds-beta xds-rl static-lr --rates 50 100 200 --repetitions 3 --settle 90
```

Trên cụm bốn node, node3 chạy cả a-3, nên `delay-node3`, `loss-node3` và `slow-node3` làm chậm cả một instance A. Muốn đúng tình huống "một backend chậm", dùng kịch bản `slow-b2` của `run_load.py` (chỉ b-2 chậm), như ở mục 7.5.

`--settle 90` là bắt buộc khi có `xds-rl` hoặc `xds-beta`: trọng số mỗi cửa sổ 10 giây chỉ dịch tối đa 10 điểm, nên cần thời gian để chúng tới đích trước khi đo.

Nếu lượt chạy bị ngắt giữa chừng:

```powershell
.\.venv\Scripts\python.exe scripts/labenv.py --env $LAB heal
```

### Dùng chung cụm

- **Mỗi lúc chỉ một người chạy.** `run_faults.py` và `run_load.py` tự giữ khóa cụm; người khác chạy cùng lúc sẽ bị từ chối và được báo ai đang dùng.
- Xem ai đang dùng: `labenv.py --env $LAB lock-status`.
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
.\.venv\Scripts\python.exe scripts/multihost.py render --spec configs/cluster.aws-4node.json --output results/aws-4node-cluster
.\.venv\Scripts\python.exe scripts/multihost.py bundle --cluster results/aws-4node-cluster --output results/loadbalance-release.tar.gz
.\.venv\Scripts\python.exe scripts/multihost.py deploy --spec configs/cluster.aws-4node.json --ssh-config configs/ssh-config.aws-new --key <KEY> --archive results/loadbalance-release.tar.gz
.\.venv\Scripts\python.exe scripts/multihost.py verify --spec configs/cluster.aws-4node.json --ssh-config configs/ssh-config.aws-new --key <KEY>
```

`deploy` build image trên từng VM nên mất vài phút; nó cũng giữ riêng dải cổng 50101–50399 để cổng service không bị kết nối đi ra chiếm mất. Chỉ đo khi `verify` báo "Sẵn sàng".

Gắn tag baseline (chỉ Khoa, sau lần triển khai đầu):

```powershell
git tag lab-baseline
git push origin lab-baseline
```

## 7. Quét năng lực và mở rộng nhiều node

Mục tiêu: tìm mức tải cụm chịu được, rồi tính và dựng cụm N node cho mức mong muốn (1 triệu RPM ≈ 16 667 RPS). Cụm được mô tả bằng một file **spec**; mẫu ở `configs/cluster.example.json`, cụm chốt ở `configs/cluster.aws-4node.json`. `<KEY>` là file khóa SSH của AWS.

**Số đã đo trên cụm chốt (21/11):** 1.500 RPS đạt 3/3 lượt và một lượt 300 giây, 0 lỗi; 1.800 RPS không đạt khi node4 (có bộ đo) hết CPU. `plan` cho 1 triệu RPM: 46 node × 4 vCPU. Đừng dùng `t3`: nó bị lấy 30–60% CPU trong 2–4 giây ở đầu mỗi phút, gây đợt lỗi định kỳ.

**Ba điều phải biết trước khi tốn tiền.**

- Một instance Python dùng được khoảng **một lõi**. Khi quá tải, nó **sụp hẳn** (request xếp hàng tới hết deadline, goodput còn khoảng 10%) chứ không đứng ở mức trần. Vì thế giới hạn đồng thời ở A và circuit breaker của Envoy được tính theo số instance A. Đừng nới chúng để "chịu thêm tải".
- Bộ phát tải tốt nhất nằm trên một EC2 riêng, cùng VPC, cùng security group. Khi không có máy riêng, chạy nó trên node4 (mục 7.0) và ghi rõ trong báo cáo: số đo được là mức tối thiểu, vì bộ đo lấy CPU của a-4 và c-2.
- Một bước `scale_probe` chỉ **hợp lệ** khi chính bộ phát tải giữ được lịch. Bước không hợp lệ không nói gì về cụm: tăng `--workers` hoặc thêm máy phát tải.

**Security group** (giữa các node trong cụm): TCP 8300, 8301, 8500, 18000, 50101–50399; UDP 8301. Từ máy phát tải tới các node ingress: TCP 80.

### 7.0. Chạy bộ đo trên node4

Bộ đo cần SSH tới các node qua IP private. Làm một lần mỗi khi có bản code mới:

1. Chép `results/loadbalance-release.tar.gz` lên node4, giải nén vào `~/lb-gen`, rồi `python3 -m venv .venv && .venv/bin/pip install -e .`
2. Chép khóa lên `~/.ssh/lb-key` (`chmod 600`). **Xóa nó khi đo xong.**
3. Tạo `~/lb-gen/configs/ssh-config.vpc` gồm `Host *`, `User ubuntu`, `IdentityFile ~/.ssh/lb-key`, `StrictHostKeyChecking accept-new`, `ServerAliveInterval 15`.
4. Tạo `configs/cluster.aws-4node.vpc.json` từ spec, với `ssh` của mỗi node bằng `ip` và bỏ dòng `ssh_config`. Tạo `configs/lab.vpc.json` từ `results/aws-4node-cluster/lab.json` theo cùng cách, với `"ssh_config": "configs/ssh-config.vpc"` và `"address": "172.31.20.204:80"`.
5. Mọi lệnh đo dài chạy nền bằng `nohup`, ghi mã thoát ra file, rồi thoát SSH. Giữ một phiên SSH suốt lượt đo thì đứt mạng là mất cả lượt.

### 7.2. Hiệu chỉnh: một instance chịu bao nhiêu

Trên node4, trong `~/lb-gen` (mục 7.0):

```bash
.venv/bin/python scripts/scale_probe.py --targets <IP-node1>:80 <IP-node2>:80 --rates 200 400 600 800 1000 1200 --duration 60 --output results/scale/calib-$(date +%Y%m%d-%H%M)
```

Cùng lúc, cũng trên node4 (lấy mẫu CPU qua IP private, không qua Internet):

```bash
.venv/bin/python scripts/multihost.py stats --spec configs/cluster.aws-4node.vpc.json --key ~/.ssh/lb-key --seconds 900 --output results/scale/calib-stats
```

Đặt `--seconds` khoảng 1,5 lần tổng thời gian quét. Chạy thêm `vmstat -t 2` trên node3 và node4; cột 17 (`st`) là CPU bị lấy mất.

Đọc `report.md` và `stats.md`. Tầng bão hòa đầu tiên là tầng có container gần 100%. Năng lực của một instance ≈ goodput ở bước đạt cuối cùng ÷ số instance của tầng đó. Ví dụ: B bão hòa ở 1 200 RPS với 3 instance thì b ≈ 400.

### 7.3. Tính cụm và dựng

```powershell
.\.venv\Scripts\python.exe scripts/multihost.py plan --target-rps 16667 --capacity a=<A> b=<B> c=<C> --vcpus 4 --output configs/cluster.scale.json
```

`plan` in ra số instance, số node và tổng vCPU, rồi ghi khung spec. Điền `ip` (private) và `ssh` (public) cho từng node, sau đó `render --spec`, `bundle`, `deploy --spec` và `verify` như mục 6. Ước lượng tiền trước khi tạo VM: số node × giá mỗi giờ × số giờ đo.

### 7.4. Quét tới mục tiêu

```bash
.venv/bin/python scripts/scale_probe.py --targets <mọi IP ingress>:80 --rates 2000 4000 8000 12000 16667 --duration 60 --workers 7 --output results/scale/run-$(date +%Y%m%d-%H%M)
```

Chạy `stats` song song như mục 7.2. Nếu một máy phát tải không giữ được lịch, chạy cùng lệnh trên hai máy. Mỗi máy nhận một nửa `--rates` và dùng cùng một `--start-at` (giờ Unix, sớm nhất là 60 giây nữa). Xong thì gộp:

```bash
.venv/bin/python scripts/scale_probe.py merge --inputs run-m1 run-m2 --output run-all
```

### 7.5. Tải thay đổi: tăng dần, đột biến, ít tải

Quét bậc ở trên chỉ tìm mức trần. Để xem hệ thống phản ứng khi tải đổi giữa chừng, dùng hồ sơ tải (bảng hồ sơ ở [tra cứu](tra-cuu.md#bài-tải--run_loadpy)).

So sánh chiến lược trên cụm bốn node, chạy trên node4 (mục 7.0), cùng lịch gửi cho mọi phương án. `x3` và `x2` nhân tải của hồ sơ cho hợp với cụm chịu được 1.500 RPS; hồ sơ `low` được bỏ vì ở tải thấp các phương án không khác nhau:

```bash
.venv/bin/python scripts/run_load.py --env configs/lab.vpc.json --variants xds xds-beta xds-rl static-lr --scenarios even slow-b2 --profiles 'step-up x3' 'spike x2' --settle 90 --repetitions 3 --output results/lab-load/step6-$(date +%Y%m%d-%H%M)
```

48 lượt, khoảng 3 giờ 40 phút. Sau khi chạy, kiểm tra độ trễ đã thật sự áp vào b-2: gần như không request nào qua b-2 có độ trễ dưới 50 ms.

Đột biến ở quy mô lớn, trên máy phát tải. Đo trần trước, rồi chọn hồ sơ có đoạn cao vượt trần một chút:

```bash
.venv/bin/python scripts/scale_probe.py --targets <mọi IP ingress>:80 --profile "4000:60,16000:10,4000:90" --workers 7 --output results/scale/spike-$(date +%Y%m%d-%H%M)
```

Trong `scale_probe`, "ổn định sau" chỉ xét lỗi, vì độ trễ không được lưu theo từng giây. Nếu một đoạn sau đột biến **không bao giờ** ổn định, đó là dấu hiệu sụp vì quá tải. Khi đó, kiểm tra circuit breaker của Envoy trước khi thêm node.

**Tắt các VM ngay khi đo xong.**

## 8. Khi gặp lỗi công cụ, báo kèm

- Lệnh đã chạy, nguyên văn.
- Phần in ra cuối cùng (traceback hoặc thông báo lỗi).
- Đường dẫn thư mục kết quả nếu đã tạo.
- Kết quả của `labenv.py --env $LAB check`.
