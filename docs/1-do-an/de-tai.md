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

## Tình trạng (12/11/2026)

| Hạng mục | Tình trạng |
|---|---|
| Hệ thống: Envoy, service A/B/C, control plane xDS, Consul, HA | Đã xây xong |
| Chiến lược: Round Robin, Least Request, β cố định, RL | Đã xây xong |
| Công cụ: gây lỗi, phát tải, triển khai, báo cáo tự động | Đã xây xong; phần điều khiển VM qua SSH chưa chạy trên VM thật |
| Triển khai bản hiện tại lên ba VM | **Chưa làm** |
| Số đo trên cụm cho cả hai nhánh | **Chưa có** |
| Báo cáo | **Chưa viết** |

Các lượt đã chạy trong `results/` đều trên bản dựng một máy; chúng xác nhận công cụ hoạt động và không dùng làm số báo cáo. Việc còn lại và người làm: [phân công](../2-cong-viec/phan-cong.md).

## Ngoài phạm vi

Ghi vào mục hướng phát triển của báo cáo:

- Chạy bằng Kubernetes và tự co giãn.
- Observability đầy đủ (Prometheus, tracing).
- TLS/mTLS, nhiều zone hoặc region.
- RL học trực tuyến trên cụm, và RL cho chặng A → C.
