# Kết quả đối chứng

Mỗi ô là median giữa các lượt, không gộp các P99 thành P99 chung.

| Workload | Offered RPS | Mode | Lượt | Goodput thực tế RPS | P95 OK ms | P99 OK ms | Lỗi RPC % | Đạt |
|---|---:|---|---:|---:|---:|---:|---:|---|
| heavy | 150 | static | 2 | 149.00 | 184.00 | 381.32 | 0.00 | Có |
| heavy | 150 | xds | 2 | 148.97 | 180.64 | 225.48 | 0.00 | Có |
| light | 150 | static | 2 | 149.46 | 158.35 | 195.90 | 0.00 | Có |
| light | 150 | xds | 2 | 149.44 | 210.39 | 297.45 | 0.00 | Có |

Dữ liệu thô và metadata từng lượt ở thư mục `results\k3s-controlled-final`.

Xem `docs/controlled-experiments.md` về phương pháp, tiêu chí, nhiễu VPN/SSH và giới hạn thống kê.
Không suy ra lookaside nhanh hơn trong mọi trường hợp từ bảng này.
