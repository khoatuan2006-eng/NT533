# Đề tài

Đồ án môn Hệ tính toán phân bố.

## Câu hỏi

Hệ thống cân bằng tải hai tầng tiếp tục phục vụ thế nào khi từng thành phần phân tán gặp sự cố?

| Nhánh | Sự cố | Cơ chế chịu lỗi | Câu hỏi phải trả lời bằng số đo |
|---|---|---|---|
| **Lỗi cứng** | Thành phần ngừng trả lời: backend, node, control plane, Consul; mạng phân mảnh | Health probe, nhân bản, quorum, fail closed | Gián đoạn bao lâu, ai phát hiện, phục hồi thế nào? |
| **Lỗi mềm** | Thành phần vẫn trả lời nhưng chậm: độ trễ, rớt gói | Health probe không bắt được, nên chỉ còn chiến lược cân bằng tải | Round Robin, Least Request hay RL né backend chậm tốt hơn, và vì sao? |

RL là một chiến lược trong nhánh lỗi mềm, không phải câu hỏi riêng.

## Khái niệm hệ phân tán được thể hiện

| Khái niệm | Thể hiện trong hệ thống |
|---|---|
| Đồng thuận, quorum | Ba Consul voter (Raft); mất một vẫn cập nhật được danh sách instance |
| Nhất quán so với sẵn sàng | Mất quorum quá lease 15 giây thì fail closed: trả `UNAVAILABLE` thay vì dùng danh sách cũ |
| Phát hiện lỗi | Control plane tự probe health từng instance, độc lập với Consul |
| Nhân bản, chịu lỗi | Hai Envoy, hai control plane, mỗi service nhiều instance trên các node khác nhau |
| Khám phá dịch vụ | Consul và xDS; caller giữ cấu hình hợp lệ gần nhất |
| Deadline xuyên chuỗi gọi | Một quỹ thời gian cho hàng đợi, xử lý cục bộ và lời gọi phụ thuộc |

## Tình trạng (21/11/2026)

| Hạng mục | Tình trạng |
|---|---|
| Hệ thống: Envoy, service A/B/C, control plane xDS, Consul, HA | Đã xây xong |
| Chiến lược: Round Robin, Least Request, β cố định, RL | Đã xây xong |
| Công cụ: gây lỗi, phát tải, triển khai N node, báo cáo tự động | Đã xây xong và chạy trên VM thật |
| Cụm chốt | 4 VM AWS `c7i-flex.large` (8 vCPU), 4 A, 2 B, 2 C ([bố trí](kien-truc.md)) |
| Số đo trên cụm chốt | Đủ 6 bước kiểm nghiệm (bảng dưới) |
| Lỗi cứng ngoài Consul leader trên cụm chốt | **Chưa đo lại**; số có trên cụm ba VM `t3.small` |
| Báo cáo | Đang viết |

Số đo trên cụm chốt, ngày 21/11/2026. Bộ đo chạy trên node4, trong VPC.

| Bước | Kết quả |
|---|---|
| 1. Chất lượng | 289/289 test đạt |
| 2. Triển khai | 4/4 node, `verify` báo Sẵn sàng |
| 3. Dừng Consul leader | 5/5 lượt đạt, 11.000/11.000 RPC, gián đoạn 0 s |
| 4. Năng lực | **1.500 RPS (90.000 RPM)**, 0 lỗi, P99 khoảng 66 ms, đạt 3/3 lượt 60 s và 1 lượt 300 s; 1.800 RPS không đạt khi node4 (có bộ đo) hết CPU |
| 5. Quy mô cho 1 triệu RPM | Ước tính 46 node × 4 vCPU = 184 vCPU (64 A, 32 B, 32 C, 5 node ingress); ngoại suy tuyến tính |
| 6. Chiến lược khi b-2 chậm thêm 50 ms | β cố định và RL đưa phần tải của b-2 xuống mức sàn 5%, ngang nhau; P50 khoảng 22 ms, P90 khoảng 29 ms, so với Round Robin 68 ms và 72 ms. Least Request còn khoảng 19% tải trên b-2 |

Trên cụm ba VM `t3.small` trước đó, mức đạt là 200 RPS; trên bốn VM `t3.small` là 1.000 RPS, với lỗi dồn thành đợt 1–2 giây ở đầu mỗi phút, trùng lúc VM bị lấy CPU (steal 30–60%). `c7i-flex` không có hiện tượng này.

## Ngoài phạm vi

Ghi vào mục hướng phát triển của báo cáo:

- Chạy bằng Kubernetes và tự co giãn.
- Observability đầy đủ (Prometheus, tracing).
- TLS/mTLS, nhiều zone hoặc region.
- RL học trực tuyến trên cụm, và RL cho chặng A → C.
