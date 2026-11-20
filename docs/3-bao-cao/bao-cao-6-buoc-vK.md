# Báo cáo tổng hợp sáu bước cân bằng tải gRPC (vK)

Thực nghiệm ngày 19/11/2026 trên ba EC2 Ubuntu `t3.small`, mỗi node 2 vCPU và khoảng 2 GiB RAM. Release ứng dụng là `f97f0ab1dca653d0`. Báo cáo tổng hợp từ các tệp JSON, trace và mẫu CPU đã lưu. Các kết quả về Windows, SSH và việc phục hồi sau bước 6 lấy từ log và kiểm tra trong phiên vận hành.

| Bước | Kết quả chính |
|---|---|
| 1. Chất lượng | 282 test đạt trước triển khai, 283 test đạt sau bản sửa công cụ đo hồ sơ tải |
| 2. Triển khai Consul | 3/3 node, `verify` báo Sẵn sàng |
| 3. Kiểm chứng `consul-leader` | 5/5 lượt, 11,000/11,000 RPC thành công |
| 4. Quét năng lực | 200 RPC/s đạt trong 60 s, bậc 300 RPC/s vượt ngưỡng lỗi |
| 5. Ước tính quy mô | 281 node 4 vCPU, tổng 1,124 vCPU theo đầu vào đo được |
| 6. So sánh khi tải thay đổi | 72/72 lượt hợp lệ, 21/24 tổ hợp đạt cả ba lần |

## 1. Kiểm tra chất lượng

```powershell
.\.venv\Scripts\python.exe scripts/check_quality.py
```

Lint và kiểm tra định dạng đều đạt. Lệnh ghi **282 test đạt trong 43,43 s** trước triển khai. Sau khi bổ sung test đi qua `run_load.main()` với `--profiles`, lần kiểm tra thứ hai ghi **283 test đạt trong 46,03 s**. Kết quả hai lần kiểm tra này in ở terminal. [Test hồ sơ tải](../../tests/test_load_profile.py)

## 2. Triển khai bản sửa Consul

```powershell
.\.venv\Scripts\python.exe scripts/multihost.py render --spec configs/cluster.aws-new.json --output results/aws-new-cluster-consul-20261119
.\.venv\Scripts\python.exe scripts/multihost.py bundle --cluster results/aws-new-cluster-consul-20261119 --output results/loadbalance-release-consul-20261119.tar.gz
.\.venv\Scripts\python.exe scripts/multihost.py deploy --spec configs/cluster.aws-new.json --ssh-config configs/ssh-config.aws-new --key <KEY> --archive results/loadbalance-release-consul-20261119.tar.gz
.\.venv\Scripts\python.exe scripts/multihost.py verify --spec configs/cluster.aws-new.json --ssh-config configs/ssh-config.aws-new --key <KEY>
```

Đã xác minh ánh xạ SSH theo node1, node2, node3 lần lượt là `3.27.8.103`, `3.26.114.251`, `16.176.22.169`. Cụm rảnh trước khi triển khai. Deploy lên **3/3 node trong 54 s**. `verify` báo **Sẵn sàng** trên ba node, gồm trạng thái container và các cổng cần thiết. [Gói triển khai](../../results/loadbalance-release-consul-20261119.tar.gz)

## 3. Kiểm chứng dừng Consul leader

Dùng `run_faults.py --scenarios consul-leader --repetitions 5`. Lần thử đầu từ Windows bị ngắt tại lượt 4 sau khi đường hầm SSH reset. Lượt dừng `consul-2` trong lần thử đó đạt 2.200/2.200 RPC. Lần kiểm chứng dùng bộ đo trên node1, gọi Envoy tại `127.0.0.1:80` và đi trong VPC. [Lần đo qua Windows](../../results/lab-faults/20261119-161946/report.md) · [Báo cáo kiểm chứng trong VPC](../../results/lab-faults/consul-recheck-20261119-164158/D1/report.md)

| Lượt | Consul dừng | RPC thành công | Goodput (RPC/s) | Lỗi (%) | P95 / P99 (ms) |
|---:|---|---:|---:|---:|---:|
| 1 | `consul-2` | 2,200/2,200 | 20.00 | 0.00 | 24.55 / 45.96 |
| 2 | `consul-1` | 2,200/2,200 | 20.00 | 0.00 | 26.47 / 49.22 |
| 3 | `consul-2` | 2,200/2,200 | 20.00 | 0.00 | 25.30 / 56.82 |
| 4 | `consul-3` | 2,200/2,200 | 20.00 | 0.00 | 26.31 / 49.67 |
| 5 | `consul-2` | 2,200/2,200 | 20.00 | 0.00 | 24.87 / 51.03 |

Đạt cả năm lượt, gồm 3 lượt dừng `consul-2`. Gián đoạn đo được là **0 s** ở mọi lượt. P95/P99 toàn lượt là **25.30 [24.55–26.47] / 49.67 [45.96–56.82] ms**, theo trung vị [min–max]. Thời gian phục hồi sau khi gửi lệnh khôi phục là **0.05 [0.05–0.05] s**.

## 4. Quét năng lực và lấy mẫu CPU

Lần chạy Windows qua tunnel SSH đạt 50 RPC/s trong 10 s. Ở 100 RPC/s, goodput là 67.82 RPC/s, lỗi 32.18% và P99 trễ lịch 19.60 ms. Đường hầm bị reset giữa lượt. [Quét tải qua Windows](../../results/scale/calib-20261119-165549/report.md)

Lần đo dùng để tính bước 5 chạy `scale_probe.py` trên node3, gọi Envoy node1 tại `172.31.20.204:80`. Mỗi bậc có 10 s khởi động. Bài smoke đo 10 s, còn mỗi bậc quét đo 60 s. `multihost.py stats --seconds 600` lấy mẫu CPU đồng thời. [Smoke trong VPC](../../results/scale/scale-vpc-20261119-175602/smoke/report.md) · [Quét trong VPC](../../results/scale/scale-vpc-20261119-175602/calib/report.md) · [Mẫu CPU](../../results/scale/calib-stats-vpc-20261119-175602/stats.md)

| Tải đặt (RPC/s) | Goodput (RPC/s) | Lỗi (%) | P95 (ms) | P99 (ms) | P99 trễ lịch (ms) | Hợp lệ | Đạt |
|---:|---:|---:|---:|---:|---:|---|---|
| 50 (smoke) | 50.00 | 0.00 | 23.53 | 24.49 | 1.13 | Có | Có |
| 100 | 100.00 | 0.00 | 26.44 | 56.95 | 1.49 | Có | Có |
| 200 | 200.00 | 0.00 | 27.97 | 74.32 | 1.84 | Có | Có |
| 300 | 295.92 | 1.36 | 52.87 | 577.66 | 11.64 | Có | Không |

Bậc 300 RPC/s có 245 RPC `UNAVAILABLE`, vượt ngưỡng lỗi 1%. Script dừng trước 400, 600 và 800 RPC/s. Bậc cao nhất đạt là **200 RPC/s**. Bài tải cố định không có chỉ số ổn định sau một lần chuyển đoạn.

Các mẫu CPU đầu tiên vượt ngưỡng xuất hiện cùng mốc **18:00:00** ở bậc 300 RPC/s. `lb-cloud-a-2-1` 199.02%, `lb-cloud-c-2-1` 119.26%, `lb-cloud-b-3-1` 102.15%. Cùng mốc giây nên chưa xác định thứ tự giữa ba container. Đỉnh của ingress trên node1 là 32.50% trong phiên lấy mẫu.

## 5. Ước tính quy mô cho 16.667 RPC/s

```powershell
.\.venv\Scripts\python.exe scripts/multihost.py plan --target-rps 16667 --capacity a=100.0 b=66.66666666666667 c=100.0 --vcpus 4
```

Đầu vào của planner là goodput bậc đạt cuối chia cho số instance của từng tầng. Hệ số tải là 70%, mỗi node 4 vCPU. Ước tính dùng công suất Envoy mặc định của công cụ là 4.000 RPC/s mỗi vCPU.

| Tầng | Instance trong lab | Goodput quan sát / instance (RPC/s) | Số instance ước tính |
|---|---:|---:|---:|
| A | 2 | 100.00 | 239 |
| B | 3 | 66.67 | 358 |
| C | 2 | 100.00 | 239 |

Kế hoạch tính **281 node**, trong đó **3 node ingress**, với tổng **1,124 vCPU**. Đây là ngoại suy từ bậc đạt 200 RPC/s và giả định của planner. Mục tiêu 16.667 RPC/s chưa được đo trên một cụm ở quy mô đó.

## 6. So sánh chiến lược khi tải thay đổi

`run_load.py` chạy trên node1 với địa chỉ `127.0.0.1:80`, bốn chiến lược `xds`, `xds-beta`, `xds-rl`, `static-lr` và hai kịch bản `even`, `slow-node3`. Node3 thêm 50 ms ở kịch bản chậm. Mỗi tổ hợp chạy ba lần và phát tải chuẩn bị 90 s trước mỗi lượt. [Cấu hình lượt đo](../../results/lab-load/profiles-vpc-20261119-181920/step6/run.json) · [72 trace gốc](../../results/lab-load/profiles-vpc-20261119-181920/step6) · [Log runner](../../results/lab-load/profiles-vpc-20261119-181920/runner.log)

Lượt khởi chạy đầu dừng khi `run.json` gặp kiểu `Profile`. Sau khi sửa công cụ đo, lần chạy mới ghi đủ 72 trace, rồi kernel node1 dừng Python với mã 137 vì hết RAM khi tổng hợp. Báo cáo này dựng lại từng lượt từ trace trên Windows. Sau bước 6 cụm được đưa về xDS và lệnh `verify` báo Sẵn sàng. [Log lỗi JSON ban đầu](../../results/lab-load/profiles-vpc-20261119-181154/runner.log)

| Hồ sơ | Lịch tải | Số tổ hợp đạt cả ba lượt |
|---|---|---:|
| `low` | `20:120` (RPC/s:giây) | 8/8 |
| `step-up` | `100:60,200:60,400:60` (RPC/s:giây) | 6/8 |
| `spike` | `150:60,600:10,150:90` (RPC/s:giây) | 7/8 |

Một lượt đạt khi bộ phát hợp lệ, lỗi không quá 1%, goodput ít nhất 98% tải đặt và P99 RPC thành công dưới 3.000 ms. Số đo trong hai bảng dưới là trung vị [min–max] qua ba lần lặp. Cột đạt đếm số lượt đạt theo tiêu chí toàn hồ sơ.

### Kết quả theo toàn hồ sơ

| Hồ sơ | Kịch bản | Phương án | Goodput (RPC/s) | Lỗi (%) | P95 (ms) | P99 (ms) | Đạt |
|---|---|---|---:|---:|---:|---:|---:|
| `low` | `even` | `xds` | 20.00 [20.00–20.00] | 0.00 [0.00–0.00] | 24.8 [23.9–25.1] | 45.5 [42.9–56.0] | 3/3 |
| `low` | `even` | `xds-beta` | 20.00 [20.00–20.00] | 0.00 [0.00–0.00] | 25.0 [24.8–25.5] | 50.1 [45.6–56.4] | 3/3 |
| `low` | `even` | `xds-rl` | 20.00 [20.00–20.00] | 0.00 [0.00–0.00] | 24.9 [24.4–26.5] | 49.4 [49.0–56.4] | 3/3 |
| `low` | `even` | `static-lr` | 20.00 [20.00–20.00] | 0.00 [0.00–0.00] | 24.0 [24.0–30.0] | 62.7 [46.6–63.7] | 3/3 |
| `low` | `slow-node3` | `xds` | 20.00 [20.00–20.00] | 0.00 [0.00–0.00] | 123.6 [123.5–123.6] | 125.1 [124.7–125.4] | 3/3 |
| `low` | `slow-node3` | `xds-beta` | 20.00 [20.00–20.00] | 0.00 [0.00–0.00] | 122.5 [75.7–123.0] | 123.4 [123.4–123.9] | 3/3 |
| `low` | `slow-node3` | `xds-rl` | 20.00 [20.00–20.00] | 0.00 [0.00–0.00] | 122.7 [91.1–122.9] | 123.7 [123.4–124.5] | 3/3 |
| `low` | `slow-node3` | `static-lr` | 20.00 [20.00–20.00] | 0.00 [0.00–0.00] | 123.4 [123.1–123.7] | 125.3 [124.7–126.1] | 3/3 |
| `step-up` | `even` | `xds` | 232.86 [232.81–233.12] | 0.20 [0.09–0.22] | 29.3 [28.8–29.9] | 177.4 [131.5–209.6] | 3/3 |
| `step-up` | `even` | `xds-beta` | 233.31 [233.17–233.33] | 0.01 [0.00–0.07] | 29.3 [29.0–30.0] | 188.4 [110.8–194.5] | 3/3 |
| `step-up` | `even` | `xds-rl` | 232.77 [232.61–232.99] | 0.24 [0.15–0.31] | 30.2 [29.7–30.3] | 210.7 [185.1–221.7] | 3/3 |
| `step-up` | `even` | `static-lr` | 233.33 [232.67–233.33] | 0.00 [0.00–0.29] | 28.4 [28.2–30.2] | 107.2 [93.6–234.2] | 3/3 |
| `step-up` | `slow-node3` | `xds` | 232.97 [232.74–233.24] | 0.15 [0.04–0.25] | 124.9 [124.7–124.9] | 204.3 [164.1–210.7] | 3/3 |
| `step-up` | `slow-node3` | `xds-beta` | 232.71 [229.98–233.33] | 0.27 [0.00–1.44] | 92.2 [81.5–116.9] | 162.4 [143.6–273.0] | 2/3 |
| `step-up` | `slow-node3` | `xds-rl` | 233.03 [232.72–233.31] | 0.13 [0.01–0.26] | 85.8 [83.1–86.0] | 182.7 [147.6–213.0] | 3/3 |
| `step-up` | `slow-node3` | `static-lr` | 232.08 [230.96–232.41] | 0.54 [0.40–1.02] | 121.9 [121.9–123.1] | 192.7 [166.1–260.1] | 2/3 |
| `spike` | `even` | `xds` | 178.12 [175.82–178.12] | 0.00 [0.00–1.29] | 39.0 [33.3–48.6] | 228.7 [84.8–262.3] | 2/3 |
| `spike` | `even` | `xds-beta` | 178.12 [178.12–178.12] | 0.00 [0.00–0.00] | 34.7 [34.0–38.5] | 89.8 [89.7–129.8] | 3/3 |
| `spike` | `even` | `xds-rl` | 178.12 [178.12–178.12] | 0.00 [0.00–0.00] | 34.2 [31.7–41.1] | 123.5 [54.7–128.3] | 3/3 |
| `spike` | `even` | `static-lr` | 178.12 [176.59–178.12] | 0.00 [0.00–0.86] | 32.4 [31.6–43.4] | 66.3 [61.4–227.9] | 3/3 |
| `spike` | `slow-node3` | `xds` | 178.12 [178.11–178.12] | 0.00 [0.00–0.01] | 124.8 [124.4–124.9] | 154.8 [140.9–157.2] | 3/3 |
| `spike` | `slow-node3` | `xds-beta` | 178.12 [178.12–178.12] | 0.00 [0.00–0.00] | 85.0 [83.9–92.0] | 128.9 [124.9–144.8] | 3/3 |
| `spike` | `slow-node3` | `xds-rl` | 178.12 [178.12–178.12] | 0.00 [0.00–0.00] | 85.3 [85.0–87.5] | 124.6 [124.2–127.1] | 3/3 |
| `spike` | `slow-node3` | `static-lr` | 178.12 [178.12–178.12] | 0.00 [0.00–0.00] | 123.4 [123.2–123.4] | 127.1 [127.0–136.1] | 3/3 |

### Kết quả theo từng đoạn tải

Ổn định sau là thời điểm bắt đầu cửa sổ 5 giây đầu tiên trong đoạn có 0 RPC lỗi. P95 của cửa sổ đó phải ở dưới hoặc bằng ngưỡng lớn hơn giữa 1,5 lần P95 đoạn đầu và P95 đoạn đầu cộng 20 ms. Đoạn đầu làm mốc.

#### `low`

| Kịch bản | Phương án | Đoạn và tải | Goodput (RPC/s) | Lỗi (%) | P95 (ms) | P99 (ms) | Phần b-2 (%) | Ổn định sau (s) |
|---|---|---|---:|---:|---:|---:|---:|---|
| `even` | `xds` | 1. 20 RPS × 120 s | 20.00 [20.00–20.00] | 0.00 [0.00–0.00] | 24.8 [23.9–25.1] | 45.5 [42.9–56.0] | 33.3 [33.3–33.3] | Mốc |
| `even` | `xds-beta` | 1. 20 RPS × 120 s | 20.00 [20.00–20.00] | 0.00 [0.00–0.00] | 25.0 [24.8–25.5] | 50.1 [45.6–56.4] | 33.6 [31.8–34.9] | Mốc |
| `even` | `xds-rl` | 1. 20 RPS × 120 s | 20.00 [20.00–20.00] | 0.00 [0.00–0.00] | 24.9 [24.4–26.5] | 49.4 [49.0–56.4] | 33.1 [31.5–33.2] | Mốc |
| `even` | `static-lr` | 1. 20 RPS × 120 s | 20.00 [20.00–20.00] | 0.00 [0.00–0.00] | 24.0 [24.0–30.0] | 62.7 [46.6–63.7] | 33.3 [33.3–33.3] | Mốc |
| `slow-node3` | `xds` | 1. 20 RPS × 120 s | 20.00 [20.00–20.00] | 0.00 [0.00–0.00] | 123.6 [123.5–123.6] | 125.1 [124.7–125.4] | 33.3 [33.3–33.3] | Mốc |
| `slow-node3` | `xds-beta` | 1. 20 RPS × 120 s | 20.00 [20.00–20.00] | 0.00 [0.00–0.00] | 122.5 [75.7–123.0] | 123.4 [123.4–123.9] | 12.5 [5.1–16.9] | Mốc |
| `slow-node3` | `xds-rl` | 1. 20 RPS × 120 s | 20.00 [20.00–20.00] | 0.00 [0.00–0.00] | 122.7 [91.1–122.9] | 123.7 [123.4–124.5] | 12.5 [8.8–14.3] | Mốc |
| `slow-node3` | `static-lr` | 1. 20 RPS × 120 s | 20.00 [20.00–20.00] | 0.00 [0.00–0.00] | 123.4 [123.1–123.7] | 125.3 [124.7–126.1] | 33.3 [33.3–33.3] | Mốc |

#### `step-up`

| Kịch bản | Phương án | Đoạn và tải | Goodput (RPC/s) | Lỗi (%) | P95 (ms) | P99 (ms) | Phần b-2 (%) | Ổn định sau (s) |
|---|---|---|---:|---:|---:|---:|---:|---|
| `even` | `xds` | 1. 100 RPS × 60 s | 100.00 [100.00–100.00] | 0.00 [0.00–0.00] | 24.6 [24.5–25.8] | 54.3 [46.3–65.2] | 33.3 [33.3–33.3] | Mốc |
| `even` | `xds` | 2. 200 RPS × 60 s | 200.00 [200.00–200.00] | 0.00 [0.00–0.00] | 27.2 [26.8–27.6] | 123.7 [112.3–166.2] | 33.3 [33.3–33.3] | 0.0 [0.0–0.0] |
| `even` | `xds` | 3. 400 RPS × 60 s | 398.57 [398.43–399.37] | 0.36 [0.16–0.39] | 30.1 [29.6–30.9] | 281.3 [187.4–295.1] | 33.3 [33.3–33.3] | 0.0 [0.0–0.0] |
| `even` | `xds-beta` | 1. 100 RPS × 60 s | 100.00 [100.00–100.00] | 0.00 [0.00–0.00] | 25.0 [24.3–25.6] | 53.1 [49.3–98.2] | 33.2 [32.8–33.5] | Mốc |
| `even` | `xds-beta` | 2. 200 RPS × 60 s | 200.00 [200.00–200.00] | 0.00 [0.00–0.00] | 26.9 [26.2–27.4] | 112.3 [76.2–165.4] | 32.8 [32.7–33.8] | 0.0 [0.0–0.0] |
| `even` | `xds-beta` | 3. 400 RPS × 60 s | 399.92 [399.52–400.00] | 0.02 [0.00–0.12] | 31.1 [29.7–31.2] | 211.4 [135.2–233.8] | 34.0 [33.9–34.8] | 0.0 [0.0–0.0] |
| `even` | `xds-rl` | 1. 100 RPS × 60 s | 100.00 [100.00–100.00] | 0.00 [0.00–0.00] | 25.4 [24.9–25.6] | 53.6 [49.0–73.3] | 32.7 [32.6–33.1] | Mốc |
| `even` | `xds-rl` | 2. 200 RPS × 60 s | 200.00 [198.40–200.00] | 0.00 [0.00–0.80] | 27.3 [27.0–33.5] | 442.7 [142.2–592.3] | 33.0 [32.8–33.3] | 0.0 [0.0–0.0] |
| `even` | `xds-rl` | 3. 400 RPS × 60 s | 398.97 [397.82–399.92] | 0.26 [0.02–0.55] | 30.6 [30.3–31.6] | 203.7 [121.4–409.3] | 35.0 [34.0–35.0] | 0.0 [0.0–0.0] |
| `even` | `static-lr` | 1. 100 RPS × 60 s | 100.00 [100.00–100.00] | 0.00 [0.00–0.00] | 25.0 [24.7–25.3] | 60.4 [56.5–83.6] | 33.3 [33.3–33.4] | Mốc |
| `even` | `static-lr` | 2. 200 RPS × 60 s | 200.00 [200.00–200.00] | 0.00 [0.00–0.00] | 26.8 [26.5–27.1] | 90.7 [77.1–138.2] | 33.3 [33.3–33.4] | 0.0 [0.0–0.0] |
| `even` | `static-lr` | 3. 400 RPS × 60 s | 400.00 [398.00–400.00] | 0.00 [0.00–0.50] | 29.0 [28.8–32.0] | 116.8 [102.6–330.9] | 33.4 [33.3–33.5] | 0.0 [0.0–0.0] |
| `slow-node3` | `xds` | 1. 100 RPS × 60 s | 100.00 [100.00–100.00] | 0.00 [0.00–0.00] | 123.9 [123.6–124.1] | 187.5 [125.1–206.1] | 33.3 [33.3–33.3] | Mốc |
| `slow-node3` | `xds` | 2. 200 RPS × 60 s | 200.00 [200.00–200.00] | 0.00 [0.00–0.00] | 124.0 [123.9–124.1] | 158.9 [131.2–169.0] | 33.3 [33.3–33.3] | 0.0 [0.0–0.0] |
| `slow-node3` | `xds` | 3. 400 RPS × 60 s | 398.92 [398.22–399.73] | 0.27 [0.07–0.45] | 125.5 [125.4–125.7] | 248.5 [178.5–270.9] | 33.3 [33.3–33.3] | 0.0 [0.0–0.0] |
| `slow-node3` | `xds-beta` | 1. 100 RPS × 60 s | 100.00 [100.00–100.00] | 0.00 [0.00–0.00] | 82.8 [75.8–122.5] | 141.2 [123.7–205.3] | 5.2 [4.9–5.3] | Mốc |
| `slow-node3` | `xds-beta` | 2. 200 RPS × 60 s | 200.00 [200.00–200.00] | 0.00 [0.00–0.00] | 90.8 [76.5–92.2] | 174.2 [124.4–260.5] | 4.9 [4.8–5.0] | 0.0 [0.0–0.0] |
| `slow-node3` | `xds-beta` | 3. 400 RPS × 60 s | 398.12 [389.95–400.00] | 0.47 [0.00–2.51] | 83.2 [82.8–123.2] | 207.1 [141.8–384.7] | 4.9 [4.9–5.1] | 0.0 [0.0–0.0] |
| `slow-node3` | `xds-rl` | 1. 100 RPS × 60 s | 100.00 [100.00–100.00] | 0.00 [0.00–0.00] | 76.0 [75.7–92.0] | 123.7 [123.5–145.5] | 4.8 [4.7–5.0] | Mốc |
| `slow-node3` | `xds-rl` | 2. 200 RPS × 60 s | 200.00 [200.00–200.00] | 0.00 [0.00–0.00] | 82.5 [77.0–84.0] | 140.9 [123.5–214.5] | 5.0 [4.9–5.3] | 0.0 [0.0–0.0] |
| `slow-node3` | `xds-rl` | 3. 400 RPS × 60 s | 399.08 [398.15–399.92] | 0.23 [0.02–0.46] | 95.3 [86.4–98.5] | 234.7 [189.4–294.8] | 4.9 [4.8–5.0] | 0.0 [0.0–0.0] |
| `slow-node3` | `static-lr` | 1. 100 RPS × 60 s | 100.00 [100.00–100.00] | 0.00 [0.00–0.00] | 74.8 [74.6–75.3] | 124.2 [124.1–125.2] | 32.8 [32.6–32.9] | Mốc |
| `slow-node3` | `static-lr` | 2. 200 RPS × 60 s | 200.00 [200.00–200.00] | 0.00 [0.00–0.00] | 122.8 [122.6–123.6] | 128.2 [124.8–169.7] | 14.5 [14.4–14.5] | 26.0 [17.0–35.0] (1/3 chưa ổn định) |
| `slow-node3` | `static-lr` | 3. 400 RPS × 60 s | 396.25 [392.88–397.22] | 0.94 [0.70–1.78] | 121.7 [113.6–123.1] | 309.7 [292.4–481.6] | 10.9 [10.8–10.9] | 0.0 [0.0–0.0] |

#### `spike`

| Kịch bản | Phương án | Đoạn và tải | Goodput (RPC/s) | Lỗi (%) | P95 (ms) | P99 (ms) | Phần b-2 (%) | Ổn định sau (s) |
|---|---|---|---:|---:|---:|---:|---:|---|
| `even` | `xds` | 1. 150 RPS × 60 s | 150.00 [150.00–150.00] | 0.00 [0.00–0.00] | 25.9 [25.2–66.8] | 109.8 [70.8–772.7] | 33.3 [33.3–33.3] | Mốc |
| `even` | `xds` | 2. 600 RPS × 10 s | 600.00 [563.10–600.00] | 0.00 [0.00–6.15] | 59.1 [33.3–190.8] | 128.7 [37.2–612.4] | 33.3 [33.3–33.3] | 1.0 [0.0–4.0] |
| `even` | `xds` | 3. 150 RPS × 90 s | 150.00 [150.00–150.00] | 0.00 [0.00–0.00] | 24.9 [24.7–25.1] | 49.0 [43.8–108.5] | 33.3 [33.3–33.3] | 0.0 [0.0–0.0] |
| `even` | `xds-beta` | 1. 150 RPS × 60 s | 150.00 [150.00–150.00] | 0.00 [0.00–0.00] | 27.6 [26.7–28.6] | 248.1 [123.0–435.2] | 33.4 [33.3–33.9] | Mốc |
| `even` | `xds-beta` | 2. 600 RPS × 10 s | 600.00 [600.00–600.00] | 0.00 [0.00–0.00] | 40.1 [35.8–53.5] | 64.5 [43.4–74.9] | 33.9 [33.4–34.1] | 0.0 [0.0–0.0] |
| `even` | `xds-beta` | 3. 150 RPS × 90 s | 150.00 [150.00–150.00] | 0.00 [0.00–0.00] | 25.0 [25.0–29.8] | 52.8 [49.8–163.6] | 33.9 [33.2–34.0] | 0.0 [0.0–0.0] |
| `even` | `xds-rl` | 1. 150 RPS × 60 s | 150.00 [150.00–150.00] | 0.00 [0.00–0.00] | 26.9 [25.1–32.1] | 250.7 [60.2–676.5] | 34.4 [33.7–34.6] | Mốc |
| `even` | `xds-rl` | 2. 600 RPS × 10 s | 600.00 [600.00–600.00] | 0.00 [0.00–0.00] | 36.0 [35.1–55.3] | 45.2 [41.7–84.3] | 35.1 [33.0–35.6] | 0.0 [0.0–0.0] |
| `even` | `xds-rl` | 3. 150 RPS × 90 s | 150.00 [150.00–150.00] | 0.00 [0.00–0.00] | 26.9 [25.2–30.0] | 72.6 [67.3–112.9] | 35.0 [34.5–35.5] | 0.0 [0.0–0.0] |
| `even` | `static-lr` | 1. 150 RPS × 60 s | 150.00 [150.00–150.00] | 0.00 [0.00–0.00] | 25.5 [25.1–26.4] | 65.7 [57.1–86.4] | 33.3 [33.3–33.4] | Mốc |
| `even` | `static-lr` | 2. 600 RPS × 10 s | 600.00 [575.40–600.00] | 0.00 [0.00–4.10] | 34.8 [34.7–224.5] | 39.3 [38.9–469.0] | 33.7 [33.7–33.8] | 0.0 [0.0–0.0] (1/3 chưa ổn định) |
| `even` | `static-lr` | 3. 150 RPS × 90 s | 150.00 [150.00–150.00] | 0.00 [0.00–0.00] | 24.9 [24.8–25.7] | 67.8 [57.2–78.6] | 33.3 [33.3–33.4] | 0.0 [0.0–0.0] |
| `slow-node3` | `xds` | 1. 150 RPS × 60 s | 150.00 [150.00–150.00] | 0.00 [0.00–0.00] | 124.0 [123.7–124.2] | 133.3 [129.5–134.9] | 33.3 [33.3–33.3] | Mốc |
| `slow-node3` | `xds` | 2. 600 RPS × 10 s | 600.00 [600.00–600.00] | 0.00 [0.00–0.00] | 127.7 [127.3–129.1] | 131.5 [130.4–133.3] | 33.3 [33.3–33.3] | 0.0 [0.0–0.0] |
| `slow-node3` | `xds` | 3. 150 RPS × 90 s | 150.00 [149.97–150.00] | 0.00 [0.00–0.02] | 124.1 [123.9–124.4] | 219.1 [215.3–252.0] | 33.3 [33.3–33.3] | 0.0 [0.0–0.0] |
| `slow-node3` | `xds-beta` | 1. 150 RPS × 60 s | 150.00 [150.00–150.00] | 0.00 [0.00–0.00] | 77.4 [77.0–79.5] | 124.3 [124.2–130.1] | 5.2 [5.1–5.3] | Mốc |
| `slow-node3` | `xds-beta` | 2. 600 RPS × 10 s | 600.00 [600.00–600.00] | 0.00 [0.00–0.00] | 87.3 [87.3–88.4] | 128.7 [128.6–128.7] | 5.3 [5.2–5.5] | 0.0 [0.0–0.0] |
| `slow-node3` | `xds-beta` | 3. 150 RPS × 90 s | 150.00 [150.00–150.00] | 0.00 [0.00–0.00] | 77.3 [76.2–121.0] | 128.6 [124.0–208.0] | 5.1 [5.0–5.4] | 0.0 [0.0–0.0] |
| `slow-node3` | `xds-rl` | 1. 150 RPS × 60 s | 150.00 [150.00–150.00] | 0.00 [0.00–0.00] | 77.1 [76.3–77.2] | 124.0 [123.9–125.0] | 5.0 [5.0–5.1] | Mốc |
| `slow-node3` | `xds-rl` | 2. 600 RPS × 10 s | 600.00 [600.00–600.00] | 0.00 [0.00–0.00] | 89.9 [88.3–90.2] | 128.2 [127.6–128.9] | 5.1 [4.3–5.6] | 0.0 [0.0–0.0] |
| `slow-node3` | `xds-rl` | 3. 150 RPS × 90 s | 150.00 [150.00–150.00] | 0.00 [0.00–0.00] | 79.9 [76.3–88.9] | 123.8 [123.7–133.3] | 4.9 [4.8–4.9] | 0.0 [0.0–0.0] |
| `slow-node3` | `static-lr` | 1. 150 RPS × 60 s | 150.00 [150.00–150.00] | 0.00 [0.00–0.00] | 123.5 [123.4–123.6] | 154.3 [137.7–159.3] | 20.1 [20.1–20.1] | Mốc |
| `slow-node3` | `static-lr` | 2. 600 RPS × 10 s | 600.00 [600.00–600.00] | 0.00 [0.00–0.00] | 85.1 [84.3–85.4] | 128.8 [128.2–128.9] | 11.4 [11.1–11.4] | 0.0 [0.0–0.0] |
| `slow-node3` | `static-lr` | 3. 150 RPS × 90 s | 150.00 [150.00–150.00] | 0.00 [0.00–0.00] | 123.4 [123.4–123.5] | 124.7 [124.6–156.2] | 20.0 [19.9–20.0] | 0.0 [0.0–0.0] |

### Kết luận và giới hạn

- Với `step-up` và node3 chậm, P95 trung vị là 124.9 ms cho xDS Round Robin, 92.2 ms cho β cố định và 85.8 ms cho RL. β cố định có P99 trung vị 162.4 ms, còn RL có 182.7 ms. RL đạt cả ba lần trong tổ hợp này.
- Cả 24 lượt `spike` đạt cửa sổ ổn định ngay từ đầu đoạn quay về 150 RPC/s. Đoạn 600 RPC/s chỉ dài 10 giây. Kết quả phục hồi sau đột biến áp dụng cho lịch tải này.
- Bộ phát trong VPC dùng chung tài nguyên node3 ở bước 4 và node1 ở bước 6. Mức 200 RPC/s là cận dưới đã kiểm chứng trong cấu hình đo. Khi đo từ Windows, P95/P99 còn bao gồm đường truyền ngoài VPC. Cột **Hợp lệ** đánh giá lịch gửi. Một lượt có thể giữ lịch dù đường truyền bị đứt.
- `static-lr` dùng khám phá tĩnh. Chênh lệch tuyệt đối với RL còn phản ánh khác biệt giữa khám phá tĩnh và xDS. Bảng so sánh trực tiếp thay đổi trọng số dùng `xds`, `xds-beta`, `xds-rl` trên cùng đường khám phá.
