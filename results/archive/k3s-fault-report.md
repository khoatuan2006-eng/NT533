# Kết quả thử lỗi

Mỗi tình huống dưới đây chạy một lần ở 20 RPS trong 120 giây.

| Trace | Mode | OK/tổng | Goodput RPS | P95/P99 OK ms | P95/P99 mọi RPC ms | Lỗi toàn bài % | Lỗi trong fault % | Gián đoạn RPC s | OK khi xác nhận fault |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| backend-static | static | 2400/2400 | 20.00 | 101.57/128.27 | 101.57/128.27 | 0.000 | 0.00 | 0.000 | 400 |
| backend-xds | xds | 2400/2400 | 20.00 | 109.60/155.63 | 109.60/155.63 | 0.000 | 0.00 | 0.000 | 399 |
| vm1-primed-xds | xds | 2322/2400 | 19.34 | 100.80/145.23 | 103.77/958.72 | 3.250 | 13.66 | 3.950 | 415 |
| vm1-xds | xds | 2382/2400 | 19.85 | 107.76/161.60 | 107.68/160.95 | 0.750 | 3.24 | 1.750 | 439 |

Gián đoạn đo từ RPC đầu lỗi tới đầu cửa sổ 5 giây toàn OK sau lỗi cuối, trước restore.
0 nghĩa là không ghi nhận lỗi ở tần suất lấy mẫu, không có nghĩa failover tức thì.
Mốc SSH/API dispatch và confirmed nằm trong JSON; không coi độ trễ quản trị là độ trễ ứng dụng.
Lỗi trong fault chỉ tính arrival từ dispatch stop tới dispatch restore; lỗi toàn bài dùng toàn bộ 120 giây.
Đây là số đo theo từng tình huống, chưa có nhiều lần lặp để suy ra phân bố RTO.
