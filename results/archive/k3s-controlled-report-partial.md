# Kết quả đối chứng

Mỗi ô là median giữa các lượt, không gộp các P99 thành P99 chung.

| Workload | Offered RPS | Mode | Lượt | Goodput thực tế RPS | P95 OK ms | P99 OK ms | Lỗi RPC % | Đạt |
|---|---:|---|---:|---:|---:|---:|---:|---|
| heavy | 50 | static | 2 | 49.54 | 204.98 | 233.53 | 0.00 | Có |
| heavy | 50 | xds | 2 | 49.60 | 195.37 | 228.61 | 0.00 | Có |
| heavy | 150 | static | 1 | 148.88 | 188.42 | 215.96 | 0.00 | Có |
| heavy | 150 | xds | 2 | 148.68 | 202.36 | 253.19 | 0.00 | Có |
| light | 50 | static | 2 | 49.73 | 175.48 | 211.98 | 0.00 | Có |
| light | 50 | xds | 2 | 49.71 | 172.15 | 206.01 | 0.00 | Có |
| light | 150 | static | 2 | 148.78 | 256.30 | 331.24 | 0.00 | Có |
| light | 150 | xds | 2 | 149.16 | 176.24 | 195.27 | 0.00 | Có |

Dữ liệu thô và metadata từng lượt ở thư mục `results\k3s-controlled-initial`.

Xem `docs/controlled-experiments.md` về phương pháp, tiêu chí, nhiễu VPN/SSH và giới hạn thống kê.
Không suy ra lookaside nhanh hơn trong mọi trường hợp từ bảng này.
