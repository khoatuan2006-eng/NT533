# Kết quả thử lỗi trên lab

Mỗi ô số là median [min–max] giữa các lần lặp. Gián đoạn tính từ RPC lỗi đầu tiên tới đầu cửa sổ 5 giây toàn OK, trong lúc lỗi còn hiệu lực; 0 nghĩa là không ghi nhận lỗi ở tần suất lấy mẫu.

| Tình huống | Mode | Lần lặp | Gián đoạn (s) | Lỗi trong lúc lỗi (%) | P95 OK (ms) | Phục hồi sau khôi phục (s) | Instance còn phục vụ | Kết luận |
|---|---|---:|---:|---:|---:|---:|---|---|
| Node 1 chết (envoy-1, controlplane-1, consul-1, a-1, b-1) | static-lr | 1 | 1.95 [1.95–1.95] | 2.50 [2.50–2.50] | 40.1 [40.1–40.1] | 0.00 [0.00–0.00] | a-2, b-2, c-1, c-2 | đạt |

Tiêu chí: tình huống “tiếp tục phục vụ” đạt khi gián đoạn ≤ 30 giây ở mọi lần lặp; tình huống mất quorum đạt khi caller xDS trả UNAVAILABLE thay vì dùng endpoint cũ.
“Instance còn phục vụ” lấy từ các RPC thành công sau 5 giây đầu của lỗi.
Lab chạy trên một máy: các node chung CPU, không có độ trễ mạng thật; không dùng số liệu này để kết luận về hiệu năng.
