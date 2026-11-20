# Mục 4 — Lỗi cứng (vK)

Bản vK đo độc lập trên cụm AWS mới, với D2 chạy cục bộ trên máy Khoa. [Bản gốc của Đạt](dat-loi-cung.md).

## Nhật ký lượt chạy

Mỗi lượt một dòng, kể cả lượt hỏng.

| Ngày | Mã | Lệnh (rút gọn) | Thư mục kết quả | `release` | Ghi chú |
|---|---|---|---|---|---|
| 18/11/2026 | D1 | `run_faults.py --repetitions 5` cho 8 kịch bản D1 | [D1](../../results/aws-new-dat-vK/D1/report.md) | `e40288499095b6f2` | 40 lượt |
| 18/11/2026 | D2 | `compare_shape_rl.py --model configs/shape-model.json` | [D2](../../results/aws-new-dat-vK/D2/report.md) | — | 92/105 lượt hợp lệ |
| 18/11/2026 | Đối chứng D1 | `node1-down --repetitions 5` qua một Envoy | [Đối chứng](../../results/aws-new-dat-vK/single-ingress/report.md) | `e40288499095b6f2` | Dùng cho mục 4 |

## 1. Dự đoán (commit trước khi đo)

| Tình huống | Ai phát hiện | Theo cơ chế / timeout nào | Dự đoán gián đoạn (s) | Request lỗi mang mã gì |
|---|---|---|---|---|
| `backend-stop` | Control plane | Probe gRPC sức khỏe b-2 | 0–5 | UNAVAILABLE hoặc DEADLINE_EXCEEDED |
| `controlplane-stop` | Caller xDS | ADS mất kết nối và thử control plane còn lại | 0–15 | UNAVAILABLE hoặc DEADLINE_EXCEEDED |
| `consul-leader` | Consul | Raft bầu lại với hai voter còn lại | 0–10 | Có thể không có lỗi RPC |
| `quorum-loss` | Control plane | Lease của registry hết hạn sau 15 s | Có thể ngừng tới khi khôi phục | UNAVAILABLE sau khi hết lease |
| `node3-down` | Control plane + Consul | Probe b-2/c-2; quorum còn 2/3 | 0–10 | UNAVAILABLE hoặc DEADLINE_EXCEEDED |
| `node1-down` | Bộ phát tải + Envoy còn lại | Kết nối gRPC đứt; thử đường vào Envoy node2; probe a-1 | 0–20 | UNAVAILABLE hoặc DEADLINE_EXCEEDED |
| `partition-node3` | Control plane + Consul | Probe sang node3 mất đáp ứng; quorum còn 2/3 | 0–15 | DEADLINE_EXCEEDED trong pha chuyển tiếp |
| `partition-node1` | Raft, Envoy và caller xDS | Node1 tách khỏi đa số; probe giữa node không tới; lease có thể hết | Quan sát, có thể kéo dài tới khôi phục | DEADLINE_EXCEEDED rồi có thể UNAVAILABLE |

## 2. Kết quả D1 trên baseline

Release `e40288499095b6f2` trên cả ba EC2. Sau D1, cả 14 container chạy lại và bài kiểm tra xDS qua Envoy node1 trả 25/25 RPC thành công ở 5 RPS. [Kết quả D1](../../results/aws-new-dat-vK/D1/report.md)

Đối chiếu lượt đầu `node1-down` từ trace và mốc lỗi. Có 13/800 RPC lỗi trong cửa sổ lỗi, khớp báo cáo.

| Tình huống | Mode | Lần lặp | Gián đoạn (s) | Lỗi trong lúc lỗi (%) | P95 OK (ms) | Phục hồi sau khôi phục (s) | Instance còn phục vụ | Kết luận |
|---|---|---:|---:|---:|---:|---:|---|---|
| Dừng một backend (b-2) | xds | 5 | 0.00 [0.00–0.00] | 0.00 [0.00–0.00] | 33.4 [24.5–43.4] | 0.05 [0.05–0.05] | a-1, a-2, b-1, b-3, c-1, c-2 | đạt |
| Dừng Consul leader | xds | 5 | 0.00 [0.00–0.00] (1 lần chưa hội tụ) | 0.00 [0.00–58.75] | 50.4 [34.0–64.1] | 0.05 [0.05–3.60] | a-1, a-2, b-1, b-2, b-3, c-1, c-2 | KHÔNG ĐẠT: chưa hội tụ trong lúc lỗi |
| Dừng một control plane (controlplane-1) | xds | 5 | 0.00 [0.00–0.00] | 0.00 [0.00–0.00] | 26.5 [25.2–43.7] | 0.05 [0.05–0.05] | a-1, a-2, b-1, b-2, b-3, c-1, c-2 | đạt |
| Node 1 chết (envoy-1, controlplane-1, consul-1, a-1, b-1) | xds | 5 | 1.35 [1.00–1.85] | 1.75 [1.38–2.38] | 30.2 [26.6–43.3] | 0.05 [0.05–0.05] | a-2, b-2, b-3, c-1, c-2 | đạt |
| Node 3 chết (consul-3, b-2, c-2) | xds | 5 | 0.00 [0.00–0.00] | 0.00 [0.00–0.00] | 38.9 [34.5–45.4] | 0.05 [0.05–0.05] | a-1, a-2, b-1, b-3, c-1 | đạt |
| Cô lập node 1 (có Envoy) khỏi node 2 và node 3 | xds | 5 | — (5 lần chưa hội tụ) | 99.12 [98.25–99.25] | 26.6 [24.0–27.4] | 16.95 [2.05–33.05] | — | quan sát |
| Cô lập node 3 khỏi node 1 và node 2 | xds | 5 | 2.05 [1.85–2.45] | 3.25 [3.00–4.00] | 28.5 [25.1–49.5] | 0.05 [0.05–0.05] | a-1, a-2, b-1, b-3, c-1 | đạt |
| Mất quorum (dừng consul-2 và consul-3) | xds | 5 | — (5 lần chưa hội tụ) | 61.00 [60.25–61.38] | 27.3 [26.2–54.1] | 2.00 [1.60–2.30] | a-1, a-2, b-1, b-2, b-3, c-1, c-2 | đạt |

## 3. Dự đoán so với số đo và giải thích

Gián đoạn trình bày median [min–max] trên các lượt có số đo. Cột mã RPC đếm từ lúc xác nhận lỗi đến lúc gửi lệnh khôi phục.

| Tình huống | Gián đoạn | Kết luận | Mã RPC | Cơ chế |
|---|---:|---|---|---|
| `backend-stop` | 0.00 [0.00–0.00] s | đạt | `OK` 3905 | Phép đo ghi nhận b-1 và b-3 phục vụ sau 5 giây đầu. Health probe và xDS cho phép loại b-2 khỏi tập endpoint B. |
| `controlplane-stop` | 0.00 [0.00–0.00] s | đạt | `OK` 3929 | Kết quả phù hợp với việc xDS giữ cấu hình đang dùng khi mất kết nối ADS. Cả bảy instance workload còn xuất hiện trong trace. |
| `consul-leader` | 0.00 [0.00–0.00] s, 1 lượt chưa hội tụ | KHÔNG ĐẠT: chưa hội tụ trong lúc lỗi | `OK` 3381, `UNAVAILABLE` 470 | Bốn lượt tiếp tục phục vụ. Lượt dừng Consul trên node2 làm lease của control plane node2 hết hạn. |
| `quorum-loss` | không hội tụ trong lúc giữ lỗi | đạt | `OK` 1300, `UNAVAILABLE` 2435 | Cả năm lượt chuyển sang từ chối RPC sau khi mất quorum, phù hợp cơ chế hết lease và tiêu chí fail-closed. |
| `node3-down` | 0.00 [0.00–0.00] s | đạt | `OK` 3880 | Hai voter còn lại giữ quorum. Trace ghi nhận b-1, b-3 và c-1 phục vụ sau khi b-2 và c-2 dừng. |
| `node1-down` | 1.35 [1.00–1.85] s | đạt | `OK` 3850, `UNAVAILABLE` 51 | Dừng Envoy trên node1 làm đứt kết nối đang dùng. Client gRPC có thêm địa chỉ Envoy node2 và có thể kết nối lại qua đó. |
| `partition-node3` | 2.05 [1.85–2.45] s | đạt | `DEADLINE_EXCEEDED` 135, `OK` 3784 | Probe giữa node3 với hai node kia mất đáp ứng; hai node còn lại vẫn có thể phục vụ. |
| `partition-node1` | không hội tụ trong lúc giữ lỗi | quan sát | `DEADLINE_EXCEEDED` 227, `UNAVAILABLE` 3726 | Envoy node1 vẫn nghe TCP trên loopback. Client tiếp tục dùng đầu vào này trong khi node1 mất kết nối tới hai node còn lại. |

Trong `node1-down`, 3.850 RPC thành công sau khi xác nhận lỗi đi qua Envoy node2. Với `quorum-loss`, toàn bộ 2.235 RPC từ giây thứ 15 sau khi xác nhận lỗi đến lúc gửi lệnh khôi phục trả `UNAVAILABLE`. Sau khi gỡ `partition-node1`, cửa sổ ổn định đầu tiên đến sau trung vị 16,95 giây, dao động 2,05–33,05 giây.

### Lượt dừng Consul leader không đạt

Lượt 1 dừng Consul node2. Node3 thắng bầu cử Raft với hai phiếu lúc 11:57:50 UTC. Control plane node2 đọc registry qua Consul cục bộ tại `127.0.0.1:8500`, rồi ghi nhận lease hết hạn và rút endpoint lúc 11:58:03 UTC. Lỗi `UNAVAILABLE` bắt đầu ở giây 26,55 của lượt đo và kéo dài đến lệnh khôi phục. Bốn lượt dừng leader ở node1 hoặc node3 đều phục vụ liên tục. [Trace lượt 1](../../results/aws-new-dat-vK/D1/consul-leader-xds-r1.jsonl) · [Log Consul và control plane](../../results/aws-new-dat-vK/D1/consul-leader-r1-selected-logs.txt)

## 4. Cặp trước / sau

Ô chọn là `node1-down`. Đối chứng kiểm tra điểm lỗi đơn ở đường vào Envoy. Thêm đường vào node2 cho phép client nối lại khi Envoy node1 dừng. Cả hai cấu hình đo dùng cùng release `e40288499095b6f2` và chạy năm lượt ở 20 RPS.

| Đầu vào | Gián đoạn trong lúc lỗi | Kết luận |
|---|---|---|
| Chỉ Envoy node1 | không hội tụ trong lúc giữ lỗi | KHÔNG ĐẠT: chưa hội tụ trong lúc lỗi |
| Envoy node1 và node2 | 1.35 [1.00–1.85] s | đạt |

[Trace đối chứng](../../results/aws-new-dat-vK/single-ingress/report.md) · [Trace hai đầu vào](../../results/aws-new-dat-vK/D1/report.md)

## 5. Giới hạn của phép đo

Bộ phát tải chạy trên hệ điều hành EC2 node1. D1 mô phỏng node chết bằng cách dừng các container của lab, nên bộ phát tải và SSH vẫn sống. Client gRPC thử hai địa chỉ theo thứ tự. Đường tới Envoy node2 đi qua tunnel SSH ở AWS. Kết quả đo khả năng chuyển giữa hai Envoy theo cách này. D2 ghép bộ phát tải, control plane và backend vào một tiến trình cục bộ. Dùng mẫu hợp lệ cùng seed để so sánh độ trễ tương đối.

## 6. Số liệu D2 — RL trên gRPC thật

[Kết quả D2](../../results/aws-new-dat-vK/D2/report.md). Máy đo dùng Intel(R) Core(TM) i5-8250U CPU @ 1.60GHz với 4 lõi và 19.9 GiB RAM. D1 chạy trên EC2 trong cùng đợt. D2 có 92/105 lượt hợp lệ. Xem phần loại lượt trong báo cáo dưới đây nếu bộ phát tải không giữ được lịch.

### RL v2 trên localhost

5 kịch bản, 3 backend, tải mở ở 70% năng lực, cửa sổ 5 giây, 4 cửa sổ khởi động không đo + 8 cửa sổ đo, 3 lần lặp (thứ tự chính sách xáo theo lần lặp). Ô là median [min–max] giữa các lần.

| Kịch bản | Chính sách | Mean (ms) | P95 (ms) | P99 (ms) | Lỗi (%) | Gửi trễ lịch P99 (ms) |
|---|---|---:|---:|---:|---:|---:|
| even | beta=0.5 | 540.0 [345.2–1335.1] | 1657.0 [1437.0–1969.0] | 1954.0 [1922.0–2015.0] | 13.3 [5.2–32.6] | 23.5 [22.4–47.8] |
| even | beta=1 | 532.9 [291.2–1056.2] | 1688.0 [1578.0–1922.0] | 1937.0 [1937.0–2000.0] | 14.5 [4.4–25.0] | 26.2 [23.5–46.2] |
| even | beta=2 | 756.5 [151.8–967.6] | 1750.0 [547.0–1875.0] | 1891.0 [797.0–1985.0] | 19.6 [6.7–23.9] | 24.2 [21.1–39.0] |
| even | beta=4 | 554.4 [500.4–608.5] | 1734.0 [1609.0–1859.0] | 1953.5 [1938.0–1969.0] | 18.7 [13.4–24.1] | 29.6 [23.3–35.9] |
| even | lr | 180.0 [167.7–676.7] | 797.0 [546.0–1531.0] | 1719.0 [765.0–1812.0] | 7.5 [6.2–7.7] | 23.9 [22.3–31.3] |
| even | rl | 330.3 [227.4–390.6] | 1453.0 [1016.0–1640.0] | 1922.0 [1719.0–1953.0] | 14.3 [10.5–15.8] | 30.1 [24.0–30.5] |
| even | rr | 244.4 [244.4–244.4] | 1250.0 [1250.0–1250.0] | 1812.0 [1812.0–1812.0] | 4.5 [4.5–4.5] | 22.8 [22.8–22.8] |
| flapping | beta=0.5 | 137.6 [97.6–292.7] | 531.0 [281.0–907.0] | 812.0 [406.0–1422.0] | 3.7 [3.0–4.8] | 31.4 [25.1–33.6] |
| flapping | beta=1 | 119.2 [93.6–180.3] | 359.0 [250.0–594.0] | 500.0 [344.0–765.0] | 3.4 [3.1–3.7] | 29.8 [22.4–30.9] |
| flapping | beta=2 | 146.4 [109.6–169.5] | 454.0 [297.0–672.0] | 781.0 [625.0–875.0] | 5.4 [5.4–5.8] | 27.2 [23.6–30.8] |
| flapping | beta=4 | 162.3 [122.5–202.1] | 562.5 [406.0–719.0] | 1055.0 [985.0–1125.0] | 6.4 [4.9–7.8] | 27.3 [25.1–29.5] |
| flapping | lr | 123.3 [85.0–141.8] | 422.0 [219.0–438.0] | 594.0 [328.0–594.0] | 1.3 [0.7–1.3] | 24.0 [22.0–24.5] |
| flapping | rl | 182.5 [90.9–274.0] | 609.5 [266.0–953.0] | 828.5 [375.0–1282.0] | 5.3 [5.1–5.5] | 26.4 [22.2–30.6] |
| flapping | rr | 216.8 [97.4–336.2] | 655.5 [296.0–1015.0] | 757.5 [359.0–1156.0] | 5.9 [4.8–7.0] | 27.5 [24.3–30.8] |
| load-step | beta=0.5 | 341.9 [130.0–553.8] | 1132.0 [421.0–1843.0] | 1258.0 [547.0–1969.0] | 17.7 [9.2–26.2] | 27.6 [23.4–31.7] |
| load-step | beta=1 | 199.3 [78.8–468.1] | 1000.0 [171.0–1594.0] | 1156.0 [406.0–1797.0] | 8.7 [7.8–15.3] | 28.5 [26.0–29.4] |
| load-step | beta=2 | 145.2 [68.6–322.3] | 719.0 [125.0–1172.0] | 1391.0 [188.0–1421.0] | 6.7 [5.7–9.4] | 27.7 [23.0–28.1] |
| load-step | beta=4 | 233.6 [74.4–353.9] | 984.0 [125.0–1094.0] | 1297.0 [234.0–1593.0] | 8.7 [5.6–11.6] | 24.8 [23.1–25.6] |
| load-step | lr | 402.9 [177.5–628.4] | 1156.0 [1015.0–1297.0] | 1492.0 [1375.0–1609.0] | 8.6 [7.4–9.8] | 23.0 [21.5–24.4] |
| load-step | rl | 137.7 [89.2–510.5] | 547.0 [297.0–1468.0] | 719.0 [421.0–1625.0] | 8.3 [6.5–10.0] | 24.4 [22.7–24.6] |
| load-step | rr | 209.7 [91.5–832.3] | 1093.0 [281.0–1954.0] | 1515.0 [375.0–2016.0] | 10.2 [8.2–34.7] | 23.1 [22.8–26.4] |
| processing-slow | beta=0.5 | 132.2 [97.4–299.1] | 375.0 [250.0–1094.0] | 625.0 [329.0–1797.0] | 5.0 [4.8–8.2] | 24.2 [21.8–24.4] |
| processing-slow | beta=1 | 100.2 [96.5–205.5] | 250.0 [234.0–625.0] | 359.0 [297.0–828.0] | 4.6 [4.3–7.0] | 25.2 [25.1–26.2] |
| processing-slow | beta=2 | 140.4 [80.4–153.6] | 359.0 [156.0–453.0] | 578.0 [250.0–625.0] | 4.7 [3.1–6.9] | 24.6 [24.0–25.5] |
| processing-slow | beta=4 | 109.6 [83.9–176.4] | 281.0 [172.0–453.0] | 593.0 [234.0–640.0] | 5.2 [5.1–8.7] | 25.6 [25.6–44.2] |
| processing-slow | lr | 103.8 [100.9–187.8] | 266.0 [265.0–485.0] | 469.0 [422.0–625.0] | 3.0 [2.7–5.2] | 29.9 [25.8–48.3] |
| processing-slow | rl | 113.1 [86.3–200.1] | 312.0 [188.0–859.0] | 641.0 [375.0–1266.0] | 6.0 [5.2–6.3] | 24.8 [23.7–28.3] |
| processing-slow | rr | 155.9 [144.3–211.1] | 515.0 [391.0–609.0] | 750.0 [516.0–844.0] | 13.1 [13.1–14.3] | 22.5 [22.1–24.3] |
| uneven-capacity | beta=0.5 | 1078.2 [855.3–1301.1] | 1906.0 [1859.0–1953.0] | 1984.5 [1969.0–2000.0] | 50.4 [33.3–67.6] | 23.7 [19.3–28.0] |
| uneven-capacity | beta=1 | 703.3 [703.3–703.3] | 1859.0 [1859.0–1859.0] | 1984.0 [1984.0–1984.0] | 47.9 [47.9–47.9] | 19.2 [19.2–19.2] |
| uneven-capacity | beta=2 | 647.9 [408.0–976.3] | 1687.0 [1687.0–1937.0] | 1953.0 [1937.0–1985.0] | 51.2 [38.8–70.5] | 15.9 [15.9–27.6] |
| uneven-capacity | beta=4 | 825.7 [553.9–829.2] | 1859.0 [1641.0–1937.0] | 1984.0 [1813.0–1984.0] | 38.5 [29.3–61.1] | 16.0 [15.9–20.6] |
| uneven-capacity | lr | 714.2 [358.9–1069.5] | 1531.0 [1125.0–1937.0] | 1727.0 [1454.0–2000.0] | 39.0 [19.2–58.9] | 19.3 [17.6–21.1] |
| uneven-capacity | rl | 746.5 [258.6–1044.5] | 1797.0 [1375.0–1969.0] | 1969.0 [1859.0–2015.0] | 32.4 [14.7–69.2] | 21.7 [21.7–27.6] |
| uneven-capacity | rr | 1111.4 [1062.9–1159.9] | 1937.5 [1922.0–1953.0] | 1992.5 [1985.0–2000.0] | 43.5 [39.4–47.6] | 20.6 [20.3–20.9] |

**13 lượt bị loại** vì p99 độ trễ gửi vượt ngưỡng 50 ms. Các lượt bị loại vẫn có trong [dữ liệu gốc](../../results/aws-new-dat-vK/D2/runs.jsonl).

### RL so với đối thủ mạnh nhất

Hiệu theo từng lần lặp (cùng lần lặp, cùng tải), median [min–max]; số âm là RL nhanh hơn. β cố định tốt nhất chọn một lần cho mọi kịch bản (mean thấp nhất tính chung), như khi triển khai thật.

β cố định tốt nhất: `beta=1`.

| Kịch bản | RL − beta=1 mean (ms) | RL − lr mean (ms) | RL − rr mean (ms) |
|---|---:|---:|---:|
| even | -142.3 [-828.7–39.1] | 150.3 [-449.3–222.9] | 146.2 [146.2–146.2] |
| processing-slow | -5.4 [-13.9–16.6] | 12.2 [-17.5–12.3] | -31.2 [-69.6–-11.0] |
| uneven-capacity | 341.3 [341.3–341.3] | -62.7 [-100.4–-25.0] | -215.8 [-413.3–-18.4] |
| flapping | 45.5 [-2.7–93.7] | 69.0 [5.9–132.2] | -34.3 [-62.2–-6.5] |
| load-step | 10.4 [-330.4–311.3] | -103.0 [-117.9–-88.2] | -120.5 [-321.7–46.3] |

Số cặp hợp lệ lần lượt với `beta=1`, `lr` và `rr` là 3/3/1 cho `even`, 3/3/3 cho `processing-slow`, 1/2/2 cho `uneven-capacity`, 2/2/2 cho `flapping` và 3/2/3 cho `load-step`. Ô có một cặp chỉ phản ánh một lần đo. Một tiến trình chạy cả backend lẫn bộ phát tải, nên các số đo dùng để so sánh tương đối trong từng kịch bản.

## 7. Trả lời câu hỏi của nhánh

D1 có 6 kịch bản đạt, 1 kịch bản không đạt và 1 kịch bản quan sát. Dừng một backend, một control plane hoặc node3 không ghi nhận gián đoạn ở 20 RPS. Khi node1 dừng, hai đầu vào Envoy đưa gián đoạn xuống 1,00–1,85 giây. Khi node1 bị cô lập, cả năm lượt chỉ hội tụ lại sau khi gỡ cô lập. Mất quorum làm hệ thống từ chối RPC sau khi lease hết hạn. Lượt dừng Consul node2 cho thấy đường đọc registry của control plane có thể mất dù Raft đã bầu được leader mới.
