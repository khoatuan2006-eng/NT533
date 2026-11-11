# Kết quả tải trên lab

Ô số là median [min–max] giữa các lần lặp. Một ô (phương án, kịch bản, mức tải) **đạt** khi mọi lần lặp thỏa: lỗi ≤ 1%, goodput ≥ 98% tải đặt, P99 request thành công < 3000 ms và bộ phát tải không bỏ request.

| Kịch bản | Phương án | RPS đặt | Lần lặp | Goodput (RPS) | P95 OK (ms) | P99 OK (ms) | Lỗi (%) | Phần b-2 (%) | Đạt |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| even | static-lr | 50 | 1 | 50.0 [50.0–50.0] | 63.6 [63.6–63.6] | 70.5 [70.5–70.5] | 0.00 [0.00–0.00] | 50 [50–50] | đạt |
| even | xds | 50 | 1 | 50.0 [50.0–50.0] | 64.5 [64.5–64.5] | 70.8 [70.8–70.8] | 0.00 [0.00–0.00] | 50 [50–50] | đạt |
| slow-node3 | static-lr | 50 | 1 | 50.0 [50.0–50.0] | 142.1 [142.1–142.1] | 154.1 [154.1–154.1] | 0.00 [0.00–0.00] | 49 [49–49] | đạt |
| slow-node3 | xds | 50 | 1 | 50.0 [50.0–50.0] | 117.6 [117.6–117.6] | 145.8 [145.8–145.8] | 0.00 [0.00–0.00] | 50 [50–50] | đạt |

## Mức tải cao nhất còn đạt

| Kịch bản | Phương án | RPS cao nhất đạt |
|---|---|---:|
| even | static-lr | 50 |
| even | xds | 50 |
| slow-node3 | static-lr | 50 |
| slow-node3 | xds | 50 |

Nếu mọi mức đều đạt thì chưa tìm thấy điểm bão hòa: tăng `--rates` rồi chạy lại.
“Phần b-2” là tỷ lệ request B do b-2 (node 3) xử lý; Round Robin cho khoảng 50%, phương án né backend chậm cho số thấp hơn ở kịch bản slow-node3.
Lab chạy trên một máy, bộ phát tải cũng chung máy: so sánh tương đối giữa các phương án, không dùng làm số tuyệt đối.
