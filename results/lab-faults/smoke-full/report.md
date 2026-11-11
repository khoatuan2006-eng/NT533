# Kết quả thử lỗi trên lab

Mỗi ô số là median [min–max] giữa các lần lặp. Gián đoạn tính từ RPC lỗi đầu tiên tới đầu cửa sổ 5 giây toàn OK, trong lúc lỗi còn hiệu lực; 0 nghĩa là không ghi nhận lỗi ở tần suất lấy mẫu.

| Tình huống | Mode | Lần lặp | Gián đoạn (s) | Lỗi trong lúc lỗi (%) | P95 OK (ms) | Phục hồi sau khôi phục (s) | Instance còn phục vụ | Kết luận |
|---|---|---:|---:|---:|---:|---:|---|---|
| Dừng một backend (b-2) | xds | 1 | 0.00 [0.00–0.00] | 0.00 [0.00–0.00] | 38.9 [38.9–38.9] | 0.04 [0.04–0.04] | a-1, a-2, b-1, c-1, c-2 | đạt |
| Dừng Consul leader | xds | 1 | 0.00 [0.00–0.00] | 0.00 [0.00–0.00] | 38.5 [38.5–38.5] | 0.04 [0.04–0.04] | a-1, a-2, b-1, b-2, c-1, c-2 | đạt |
| Dừng một control plane (controlplane-1) | xds | 1 | 0.00 [0.00–0.00] | 0.00 [0.00–0.00] | 35.9 [35.9–35.9] | 0.05 [0.05–0.05] | a-1, a-2, b-1, b-2, c-1, c-2 | đạt |
| Node 3 ở xa: thêm 100 ms độ trễ | xds | 1 | 0.00 [0.00–0.00] | 0.00 [0.00–0.00] | 234.1 [234.1–234.1] | 0.00 [0.00–0.00] | a-1, a-2, b-1, b-2, c-1, c-2 | đạt |
| Mạng kém: node 3 rớt 20% gói | xds | 1 | — (1 lần chưa hội tụ) | 2.50 [2.50–2.50] | 661.1 [661.1–661.1] | 0.01 [0.01–0.01] | a-1, a-2, b-1, b-2, c-1, c-2 | quan sát |
| Node 1 chết (envoy-1, controlplane-1, consul-1, a-1, b-1) | xds | 1 | 1.90 [1.90–1.90] | 2.50 [2.50–2.50] | 34.4 [34.4–34.4] | 0.00 [0.00–0.00] | a-2, b-2, c-1, c-2 | đạt |
| Node 3 chết (consul-3, b-2, c-2) | xds | 1 | 0.00 [0.00–0.00] | 0.00 [0.00–0.00] | 42.6 [42.6–42.6] | 0.04 [0.04–0.04] | a-1, a-2, b-1, c-1 | đạt |
| Cô lập node 1 (có Envoy) khỏi node 2 và node 3 | xds | 1 | — (1 lần chưa hội tụ) | 93.75 [93.75–93.75] | 37.1 [37.1–37.1] | — | — | quan sát |
| Cô lập node 3 khỏi node 1 và node 2 | xds | 1 | 2.30 [2.30–2.30] | 4.13 [4.13–4.13] | 39.0 [39.0–39.0] | 0.02 [0.02–0.02] | a-1, a-2, b-1, c-1 | đạt |
| Mất quorum (dừng consul-2 và consul-3) | xds | 1 | — (1 lần chưa hội tụ) | 50.56 [50.56–50.56] | 41.7 [41.7–41.7] | 3.88 [3.88–3.88] | a-1, a-2, b-1, b-2, c-1, c-2 | đạt |

Tiêu chí: tình huống “tiếp tục phục vụ” đạt khi gián đoạn ≤ 30 giây ở mọi lần lặp; tình huống mất quorum đạt khi caller xDS trả UNAVAILABLE thay vì dùng endpoint cũ.
“Instance còn phục vụ” lấy từ các RPC thành công sau 5 giây đầu của lỗi.
Lab chạy trên một máy: các node chung CPU, không có độ trễ mạng thật; không dùng số liệu này để kết luận về hiệu năng.
