# BẢNG SO SÁNH HIỆU NĂNG: ROUND ROBIN VS LEAST REQUEST
**Kịch bản tải:** `low` | **Số lần lặp lại:** `1`

| Tiêu chí đánh giá | Round Robin | Least Request | Chênh lệch (LR vs RR) |
| :--- | :---: | :---: | :---: |
| **Thông lượng (Throughput)** | `30.66 ± 0.0` req/s | `30.96 ± 0.0` req/s | +0.30 req/s |
| **Độ trễ p50 (Median)** | `4.277` ms | `7.317` ms | +3.040 ms |
| **Độ trễ p95** | `8.406` ms | `16.961` ms | +8.555 ms |
| **Độ trễ p99 (Đuôi trễ)** | `11.724` ms | `19.938` ms | +8.214 ms |
| **Tỷ lệ thành công (OK)** | `100.0%` | `100.0%` | +0.00% |
| **Tỷ lệ Timeout** | `0.0%` | `0.0%` | +0.00% |

### Phân bố Request theo Replica (Tổng hợp qua tất cả các lần chạy):

| Replica | Round Robin (Số request) | Least Request (Số request) |
| :--- | :---: | :---: |
| `replica-1` | 10 | 10 |
| `replica-2` | 10 | 10 |
| `replica-3` | 10 | 10 |
