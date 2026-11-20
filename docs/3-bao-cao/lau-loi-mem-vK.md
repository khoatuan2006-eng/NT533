# Mục 5 — Lỗi mềm trên cụm (vK)

Bản vK do Khoa chạy trên ba EC2 mới và ghi số liệu độc lập. Bản gốc của Lầu nằm tại [lau-loi-mem.md](lau-loi-mem.md).

## Nhật ký lượt chạy

Mỗi lượt một dòng, kể cả lượt hỏng.

| Ngày | Mã | Lệnh (rút gọn) | Thư mục kết quả | `release` | Ghi chú |
|---|---|---|---|---|---|
| 18/11/2026 | L1 | `run_load.py --variants xds static static-lr --rates 50 100 200 400 --repetitions 3` | [L1](../../results/aws-new-lau-vK/L1/report.md) | `e40288499095b6f2` | 72 lượt |
| 18/11/2026 | L2 | `run_faults.py --scenarios delay-node3 loss-node3 --repetitions 5` | [L2](../../results/aws-new-lau-vK/L2/report.md) | `e40288499095b6f2` | 10 lượt |
| 18/11/2026 | L3 | `run_load.py --variants xds xds-beta xds-rl static-lr --rates 50 100 200 --repetitions 3 --settle 90` | [L3](../../results/aws-new-lau-vK/L3/report.md) | `e40288499095b6f2` | 72 lượt |
| 18/11/2026 | before-after | `run_load.py --variants xds xds-lr-ingress --scenarios slow-node3 --rates 200 --repetitions 3` | [before-after](../../results/aws-new-lau-vK/before-after/report.md) | `e40288499095b6f2` | Cặp trước/sau |

## 1. Dự đoán (commit trước khi đo)

Ở kịch bản `slow-node3` (node 3 thêm 50 ms):

| Phương án | Dự đoán phần b-2 (%) | Dự đoán P95 so với `even` | Dự đoán P99 so với `even` | Vì sao |
|---|---|---|---|---|
| `static` (Round Robin) | Khoảng 33% | Tăng | Tăng | Lượt gọi vẫn xoay đều khi b-2 còn khỏe |
| `static-lr` (Least Request) | Dưới 33% khi đủ tải | Tăng ít hơn Round Robin | Vẫn tăng | Request trên b-2 tồn đọng lâu hơn |
| `xds` (Round Robin) | Khoảng 33% | Tăng | Tăng | Health probe có thể vẫn giữ backend chậm trong tập endpoint |
| `xds-beta` (β cố định) | Dưới 33% | Có thể giảm so với xds | Vẫn tăng | Trọng số phản ứng theo latency và luôn giữ một phần tải cho b-2 |
| `xds-rl` (RL) | Dưới 33% | Có thể giảm so với xds | Vẫn tăng | RL chọn β theo cửa sổ telemetry; phần tải còn lại đi qua backend chậm |

- Dự đoán ở `even`: 50 và 100 RPS còn dư; mức 400 RPS có khả năng chạm giới hạn trước trên t3.small.
- Với 100 ms trễ thêm, dự đoán probe vẫn giữ b-2 và c-2 nếu chúng đáp trong timeout, còn độ trễ RPC tăng.
- Với rớt gói 20%, retransmission có thể làm P99 và số deadline tăng; endpoint có thể bị rút tạm nếu probe thất bại liên tiếp.
## 2. Kết quả L1 — Round Robin và Least Request

Release `e40288499095b6f2`. [L1](../../results/aws-new-lau-vK/L1/report.md) · Đếm trong `slow-node3-static-lr-rps100-r1.jsonl`: 926/3000 lượt gọi B thành công đi vào b-2 (30.87%), khớp `report.json`.

| Kịch bản | Phương án | RPS đặt | Lần lặp | Goodput (RPS) | P95 OK (ms) | P99 OK (ms) | Lỗi (%) | Phần b-2 (%) | Đạt |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| even | static | 50 | 3 | 50.0 [50.0–50.0] | 30.7 [29.4–40.6] | 36.1 [34.1–67.6] | 0.00 [0.00–0.00] | 33 [33–33] | đạt |
| even | static | 100 | 3 | 100.0 [100.0–100.0] | 48.3 [37.6–89.4] | 101.7 [47.0–160.3] | 0.00 [0.00–0.00] | 33 [33–33] | đạt |
| even | static | 200 | 3 | 200.0 [197.6–200.0] | 34.8 [29.1–277.1] | 45.0 [35.1–1010.8] | 0.00 [0.00–1.22] | 33 [33–33] | KHÔNG ĐẠT |
| even | static | 400 | 3 | 397.0 [392.7–400.0] | 66.3 [49.2–116.6] | 348.6 [77.0–506.5] | 0.74 [0.00–1.82] | 33 [33–33] | KHÔNG ĐẠT |
| even | static-lr | 50 | 3 | 50.0 [50.0–50.0] | 46.0 [27.3–47.9] | 84.1 [30.8–88.0] | 0.00 [0.00–0.00] | 33 [33–33] | đạt |
| even | static-lr | 100 | 3 | 100.0 [100.0–100.0] | 40.6 [26.1–46.3] | 51.8 [29.4–93.9] | 0.00 [0.00–0.00] | 33 [33–33] | đạt |
| even | static-lr | 200 | 3 | 199.9 [199.8–200.0] | 136.0 [29.3–183.5] | 608.9 [34.7–654.0] | 0.07 [0.00–0.08] | 34 [33–34] | đạt |
| even | static-lr | 400 | 3 | 400.0 [390.1–400.0] | 38.4 [33.9–267.5] | 85.6 [61.4–608.3] | 0.00 [0.00–2.48] | 34 [33–35] | KHÔNG ĐẠT |
| even | xds | 50 | 3 | 50.0 [50.0–50.0] | 28.4 [26.5–61.8] | 33.4 [31.7–398.1] | 0.00 [0.00–0.00] | 33 [33–33] | đạt |
| even | xds | 100 | 3 | 100.0 [100.0–100.0] | 45.5 [32.2–75.3] | 93.1 [40.5–165.6] | 0.00 [0.00–0.00] | 33 [33–33] | đạt |
| even | xds | 200 | 3 | 200.0 [200.0–200.0] | 28.4 [27.0–101.4] | 34.0 [31.1–361.9] | 0.00 [0.00–0.00] | 33 [33–33] | đạt |
| even | xds | 400 | 3 | 395.3 [386.9–400.0] | 98.9 [34.8–147.9] | 469.1 [44.6–643.8] | 1.18 [0.00–3.27] | 33 [33–33] | KHÔNG ĐẠT |
| slow-node3 | static | 50 | 3 | 50.0 [50.0–50.0] | 123.9 [123.6–125.4] | 147.5 [127.5–168.3] | 0.00 [0.00–0.00] | 33 [33–33] | đạt |
| slow-node3 | static | 100 | 3 | 100.0 [100.0–100.0] | 124.3 [124.0–132.3] | 129.3 [126.8–214.3] | 0.00 [0.00–0.00] | 33 [33–33] | đạt |
| slow-node3 | static | 200 | 3 | 200.0 [200.0–200.0] | 125.4 [124.5–132.8] | 132.1 [127.1–220.7] | 0.00 [0.00–0.00] | 33 [33–33] | đạt |
| slow-node3 | static | 400 | 3 | 388.5 [387.7–400.0] | 161.1 [126.1–240.4] | 621.9 [130.1–643.7] | 2.88 [0.00–3.08] | 33 [33–33] | KHÔNG ĐẠT |
| slow-node3 | static-lr | 50 | 3 | 50.0 [50.0–50.0] | 76.3 [75.1–83.5] | 81.2 [77.9–133.2] | 0.00 [0.00–0.00] | 33 [33–33] | đạt |
| slow-node3 | static-lr | 100 | 3 | 100.0 [100.0–100.0] | 125.3 [124.3–143.8] | 180.9 [129.1–286.1] | 0.00 [0.00–0.00] | 31 [31–32] | đạt |
| slow-node3 | static-lr | 200 | 3 | 200.0 [200.0–200.0] | 124.2 [122.9–137.7] | 213.0 [128.8–316.7] | 0.00 [0.00–0.00] | 15 [14–15] | đạt |
| slow-node3 | static-lr | 400 | 3 | 400.0 [385.0–400.0] | 84.7 [80.6–205.5] | 127.3 [125.8–673.2] | 0.00 [0.00–3.74] | 12 [11–13] | KHÔNG ĐẠT |
| slow-node3 | xds | 50 | 3 | 50.0 [50.0–50.0] | 124.2 [123.8–124.4] | 145.8 [129.8–149.5] | 0.00 [0.00–0.00] | 33 [33–33] | đạt |
| slow-node3 | xds | 100 | 3 | 100.0 [100.0–100.0] | 124.1 [123.9–124.9] | 127.3 [126.3–144.2] | 0.00 [0.00–0.00] | 33 [33–33] | đạt |
| slow-node3 | xds | 200 | 3 | 200.0 [195.5–200.0] | 125.2 [124.8–179.7] | 131.8 [128.3–1249.6] | 0.00 [0.00–2.23] | 33 [33–33] | KHÔNG ĐẠT |
| slow-node3 | xds | 400 | 3 | 394.8 [391.1–400.0] | 173.7 [125.9–177.6] | 413.2 [130.9–542.9] | 1.29 [0.00–2.23] | 33 [33–33] | KHÔNG ĐẠT |

| Kịch bản | Phương án | RPS cao nhất đạt |
|---|---|---:|
| even | static | 100 |
| even | static-lr | 200 |
| even | xds | 200 |
| slow-node3 | static | 200 |
| slow-node3 | static-lr | 200 |
| slow-node3 | xds | 100 |

Có ô chưa đạt trong dải tải đã chọn của L1.

## 3. Kết quả L2 — `delay-node3` và `loss-node3`

Release `e40288499095b6f2`. [L2](../../results/aws-new-lau-vK/L2/report.md)

| Tình huống | Mode | Lần lặp | Gián đoạn (s) | Lỗi trong lúc lỗi (%) | P95 OK (ms) | Phục hồi sau khôi phục (s) | Instance còn phục vụ | Kết luận |
|---|---|---:|---:|---:|---:|---:|---|---|
| Node 3 ở xa: thêm 100 ms độ trễ | xds | 5 | 0.00 [0.00–0.00] | 0.00 [0.00–0.00] | 223.7 [223.2–223.9] | 0.05 [0.05–0.05] | a-1, a-2, b-1, b-2, b-3, c-1, c-2 | đạt |
| Mạng kém: node 3 rớt 20% gói | xds | 5 | 0.00 [0.00–0.00] (1 lần chưa hội tụ) | 0.00 [0.00–2.38] | 294.1 [236.1–433.6] | 0.05 [0.05–0.05] | a-1, a-2, b-1, b-2, b-3, c-1, c-2 | quan sát |

## 4. Số liệu L3 — bài tải có RL

Release `e40288499095b6f2`. Đã dùng `--settle 90` cho mọi phương án. [L3](../../results/aws-new-lau-vK/L3/report.md)

| Kịch bản | Phương án | RPS đặt | Lần lặp | Goodput (RPS) | P95 OK (ms) | P99 OK (ms) | Lỗi (%) | Phần b-2 (%) | Đạt |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| even | static-lr | 50 | 3 | 50.0 [50.0–50.0] | 47.0 [25.5–53.2] | 76.9 [28.6–99.8] | 0.00 [0.00–0.00] | 33 [33–33] | đạt |
| even | static-lr | 100 | 3 | 100.0 [100.0–100.0] | 27.3 [26.4–49.7] | 32.9 [31.2–95.1] | 0.00 [0.00–0.00] | 33 [33–33] | đạt |
| even | static-lr | 200 | 3 | 200.0 [197.2–200.0] | 28.9 [28.2–86.6] | 34.9 [33.3–871.6] | 0.00 [0.00–1.42] | 33 [33–33] | KHÔNG ĐẠT |
| even | xds | 50 | 3 | 50.0 [50.0–50.0] | 47.9 [24.5–54.5] | 82.2 [26.6–94.6] | 0.00 [0.00–0.00] | 33 [33–33] | đạt |
| even | xds | 100 | 3 | 100.0 [100.0–100.0] | 28.3 [25.4–74.3] | 33.3 [28.3–195.9] | 0.00 [0.00–0.00] | 33 [33–33] | đạt |
| even | xds | 200 | 3 | 200.0 [200.0–200.0] | 34.1 [27.1–120.5] | 44.8 [30.8–289.0] | 0.00 [0.00–0.00] | 33 [33–33] | đạt |
| even | xds-beta | 50 | 3 | 50.0 [50.0–50.0] | 25.2 [24.6–25.3] | 28.5 [27.5–29.1] | 0.00 [0.00–0.00] | 33 [32–35] | đạt |
| even | xds-beta | 100 | 3 | 100.0 [100.0–100.0] | 68.0 [25.4–142.2] | 138.7 [28.5–555.0] | 0.00 [0.00–0.00] | 33 [32–35] | đạt |
| even | xds-beta | 200 | 3 | 200.0 [200.0–200.0] | 70.4 [27.8–96.6] | 247.9 [97.1–249.5] | 0.00 [0.00–0.00] | 34 [33–34] | đạt |
| even | xds-rl | 50 | 3 | 50.0 [50.0–50.0] | 45.9 [35.3–53.8] | 83.4 [81.2–101.4] | 0.00 [0.00–0.00] | 34 [34–36] | đạt |
| even | xds-rl | 100 | 3 | 100.0 [100.0–100.0] | 56.0 [25.4–132.0] | 108.4 [28.2–521.9] | 0.00 [0.00–0.00] | 34 [33–36] | đạt |
| even | xds-rl | 200 | 3 | 200.0 [200.0–200.0] | 31.6 [26.3–64.2] | 39.8 [29.2–561.3] | 0.00 [0.00–0.00] | 34 [34–35] | đạt |
| slow-node3 | static-lr | 50 | 3 | 50.0 [50.0–50.0] | 83.8 [75.0–165.9] | 123.4 [76.9–228.4] | 0.00 [0.00–0.00] | 33 [33–33] | đạt |
| slow-node3 | static-lr | 100 | 3 | 100.0 [100.0–100.0] | 124.8 [124.2–124.8] | 130.6 [126.5–163.0] | 0.00 [0.00–0.00] | 32 [32–32] | đạt |
| slow-node3 | static-lr | 200 | 3 | 200.0 [200.0–200.0] | 123.3 [123.3–125.9] | 131.4 [125.0–212.3] | 0.00 [0.00–0.00] | 15 [14–15] | đạt |
| slow-node3 | xds | 50 | 3 | 50.0 [50.0–50.0] | 124.9 [123.3–126.5] | 145.9 [131.3–165.0] | 0.00 [0.00–0.00] | 33 [33–33] | đạt |
| slow-node3 | xds | 100 | 3 | 100.0 [100.0–100.0] | 129.0 [124.0–134.5] | 136.1 [125.8–319.7] | 0.00 [0.00–0.00] | 33 [33–33] | đạt |
| slow-node3 | xds | 200 | 3 | 200.0 [200.0–200.0] | 125.1 [124.1–129.1] | 128.6 [126.0–226.1] | 0.00 [0.00–0.00] | 33 [33–33] | đạt |
| slow-node3 | xds-beta | 50 | 3 | 50.0 [50.0–50.0] | 80.7 [75.0–111.8] | 123.6 [122.7–134.5] | 0.00 [0.00–0.00] | 5 [4–5] | đạt |
| slow-node3 | xds-beta | 100 | 3 | 100.0 [100.0–100.0] | 76.5 [75.8–78.6] | 124.0 [123.7–124.2] | 0.00 [0.00–0.00] | 5 [5–6] | đạt |
| slow-node3 | xds-beta | 200 | 3 | 200.0 [200.0–200.0] | 102.1 [79.7–125.9] | 162.1 [124.7–335.4] | 0.00 [0.00–0.00] | 5 [5–5] | đạt |
| slow-node3 | xds-rl | 50 | 3 | 50.0 [50.0–50.0] | 76.8 [76.4–82.7] | 123.1 [123.1–123.8] | 0.00 [0.00–0.00] | 4 [4–4] | đạt |
| slow-node3 | xds-rl | 100 | 3 | 100.0 [100.0–100.0] | 122.9 [88.4–124.8] | 165.7 [124.7–309.2] | 0.00 [0.00–0.00] | 5 [4–5] | đạt |
| slow-node3 | xds-rl | 200 | 3 | 199.3 [198.6–200.0] | 168.1 [76.1–463.5] | 677.3 [123.6–685.4] | 0.35 [0.00–0.68] | 5 [5–5] | đạt |

Ở `slow-node3`, 100 RPS, phần b-2 theo median là `xds` 33.3%, `xds-beta` 5.2%, `xds-rl` 4.5%. Đọc cột goodput và lỗi bên cạnh phần b-2 để phân biệt né được backend chậm với việc có ít RPC hoàn tất.

## 5. Dự đoán so với số đo và giải thích

So sánh tại 100 RPS với `slow-node3` trên cùng release. Số trong bảng là median của ba lần lặp.

| Phương án | b-2 (%) | P95 (ms) | P99 (ms) | Goodput (RPS) | Lỗi (%) | Đạt |
|---|---:|---:|---:|---:|---:|---|
| static | 33.3 | 124.3 | 129.3 | 100.0 | 0.00 | đạt |
| static-lr | 31.3 | 125.3 | 180.9 | 100.0 | 0.00 | đạt |
| xds | 33.3 | 124.1 | 127.3 | 100.0 | 0.00 | đạt |

Tại 100 RPS, Least Request gửi ít tải hơn vào b-2, giảm 2.1 điểm phần trăm so với Round Robin static. P95 của `static-lr` là 40.6 ms ở `even` và 125.3 ms ở `slow-node3`. Với Round Robin static, cặp tương ứng là 48.3 và 124.3 ms. Tỷ lệ b-2, P95, P99 và goodput có thể đổi khác nhau vì chúng lần lượt phản ánh phân tải, phần đuôi và số request hoàn tất.

- **Bão hòa.** Dùng các mức cao nhất còn đạt trong bảng L1 và bảng mở rộng nếu có. Nếu qua mọi mức đã thử, phép đo chỉ xác nhận cận dưới của năng lực cụm.
- **xDS so với static.** Ở 100 RPS và `slow-node3`, P95 của Round Robin xDS là 124.1 ms, so với 124.3 ms của Round Robin static. Hai cách khám phá endpoint khác nhau; số này đo chênh lệch trên toàn đường gRPC.

- `delay-node3`: b-2 có RPC thành công sau 5 giây đầu lỗi ở 5/5 lượt; c-2 ở 5/5 lượt. Log ghi 0 lần probe chuyển b-2 sang không phục vụ và 0 lần với c-2 trong các cửa sổ lỗi đã ghi.
- `loss-node3`: b-2 có RPC thành công sau 5 giây đầu lỗi ở 5/5 lượt; c-2 ở 5/5 lượt. Log ghi 0 lần probe chuyển b-2 sang không phục vụ và 1 lần với c-2 trong các cửa sổ lỗi đã ghi.

[Log node1](../../results/aws-new-lau-vK/L2-controlplane-node1.log) · [Log node2](../../results/aws-new-lau-vK/L2-controlplane-node2.log)

## 6. Cặp trước / sau

Giữ nguyên release, 200 RPS và `slow-node3`. Đổi từ Round Robin sang Least Request tại Envoy, giữ A → B/C ở xDS để xem lựa chọn instance A có làm giảm chờ khi node3 chậm không. Mỗi phương án có ba lần lặp trong cùng đợt. [before-after](../../results/aws-new-lau-vK/before-after/report.md)

| Kịch bản | Phương án | RPS đặt | Lần lặp | Goodput (RPS) | P95 OK (ms) | P99 OK (ms) | Lỗi (%) | Phần b-2 (%) | Đạt |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| slow-node3 | xds | 200 | 3 | 200.0 [199.5–200.0] | 128.3 [124.0–194.3] | 265.6 [125.5–709.4] | 0.00 [0.00–0.27] | 33 [33–33] | đạt |
| slow-node3 | xds-lr-ingress | 200 | 3 | 200.0 [200.0–200.0] | 123.9 [123.6–125.6] | 125.7 [125.2–238.9] | 0.00 [0.00–0.00] | 33 [33–33] | đạt |

P95 đổi -4.4 ms; goodput và tỷ lệ lỗi ở bảng trên cho biết cải thiện độ trễ có đánh đổi khả năng phục vụ hay không.

## 7. Giới hạn của phép đo

Bộ phát tải chạy trên EC2 node1 và đi qua Envoy node1. Kết quả có độ trễ nội bộ VPC và chịu ảnh hưởng tài nguyên dùng chung trên node1. Cả ba VM dùng t3.small. Số đo áp dụng cho cấu hình và thời điểm chạy này. Phần b-2 chỉ đếm trong RPC thành công. L2 phản ánh cả thời gian phản ứng của health probe lẫn lỗi gRPC khi đường truyền chậm hoặc mất gói.

## 8. Trả lời câu hỏi của nhánh

Tại 100 RPS, Least Request gửi ít tải hơn vào b-2, giảm 2.1 điểm phần trăm so với Round Robin static. Least Request có cơ hội tự chuyển tải nếu request tới một backend giữ trạng thái đang xử lý lâu hơn. Bảng L1 cho thấy kết quả thực tế theo từng mức tải. Đánh giá lợi ích cho người gọi cần đọc đồng thời goodput, lỗi, P95 và P99.
