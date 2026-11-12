# Hệ thống cân bằng tải phân tán

Đồ án môn Hệ tính toán phân bố. Hệ thống cân bằng tải hai tầng: **Envoy cân bằng request vào service điều phối** và **lookaside (gRPC xDS) cân bằng lời gọi từ service điều phối tới các service xử lý**.

## Tài liệu nằm ở đâu

| Bạn cần | Mở | Trong đó có |
|---|---|---|
| **Hiểu đồ án** | README này | Câu hỏi, kiến trúc, khái niệm hệ phân tán, phạm vi |
| | [`docs/1-do-an/`](docs/1-do-an/) | Chi tiết: kiến trúc code, thử nghiệm RL |
| **Biết việc phải làm** | [`docs/2-cong-viec/`](docs/2-cong-viec/1-viec-cua-toi.md) | Ba file đọc theo số thứ tự: việc của tôi, hướng dẫn từng bước, tra cứu |
| **Viết báo cáo** | [`docs/3-bao-cao/`](docs/3-bao-cao/README.md) | Dàn ý, quy tắc viết, mỗi người một file để điền |

`docs/history/` giữ tài liệu các giai đoạn trước, chỉ dùng để trích dẫn bằng chứng.

## Câu hỏi của đồ án

**Câu hỏi chính:** hệ thống cân bằng tải hai tầng này tiếp tục phục vụ thế nào khi từng thành phần phân tán bị lỗi — backend, máy chủ, control plane, registry, hoặc khi mạng bị phân mảnh?

Câu hỏi này chia thành hai nhánh theo loại lỗi; mọi phần của đồ án đều thuộc một trong hai nhánh:

| Nhánh | Lỗi | Cơ chế chịu lỗi | Câu hỏi cần trả lời |
|---|---|---|---|
| **Lỗi cứng** | Thành phần ngừng trả lời: backend, node, control plane, Consul chết; mạng phân mảnh | Phát hiện lỗi (health probe, timeout), nhân bản, quorum, fail closed | Ai phát hiện, sau bao lâu; hệ thống gián đoạn bao lâu và tự phục hồi thế nào? |
| **Lỗi mềm** | Thành phần vẫn trả lời nhưng chậm: backend chậm, mạng trễ hoặc rớt gói | Health probe không bắt được, nên **chiến lược cân bằng tải** là cơ chế chịu lỗi duy nhất | Round Robin, Least Request hay RL né backend chậm tốt hơn, và vì sao? |

**RL là phần mở rộng trong nhánh lỗi mềm**, không phải câu hỏi riêng. Nó phải trả lời thêm một điều: bộ điều khiển học được có an toàn khi chính nó lỗi hoặc thiếu dữ liệu không (xem [thử nghiệm RL](docs/1-do-an/thu-nghiem-rl.md)).

## Kiến trúc

### Kiến trúc logic: request đi đâu, ai quyết định

![Kiến trúc logic: client gọi hai Envoy, Envoy chia tải vào service A, A gọi trực tiếp service B và C; Consul và control plane phát endpoint cho A qua xDS](docs/images/architecture-logical.svg)

- Có **hai lần cân bằng tải**: Envoy chọn a-1 hay a-2; sau đó chính A chọn instance B và C rồi gọi thẳng tới đó (mô hình lookaside).
- **Control plane không nằm trên đường đi của request.** Nó kiểm tra health, lấy danh sách instance từ Consul, và chỉ phát cập nhật khi có thay đổi. Control plane chết thì A vẫn dùng danh sách đã có.
- **RL là phần mở rộng tùy chọn**: nó chỉ đổi trọng số mà control plane phát ra, không tác động trực tiếp lên request.

### Bố trí trên ba node: một node chết thì mất gì

![Bố trí trên ba node: node1 có Consul, control plane, Envoy, a-1, b-1; node2 có Consul, control plane, Envoy, a-2, c-1, b-3; node3 có Consul, b-2, c-2; bộ phát tải nằm ngoài cụm](docs/images/architecture-nodes.svg)

- **Hai bản của cùng một thành phần không bao giờ nằm chung node.** Chết một node bất kỳ thì thành phần nào cũng còn ít nhất một bản, và Consul còn 2/3, vẫn đủ quorum.
- **Node 1 gánh nặng nhất** (có cả Envoy lẫn control plane), nên "node 1 chết" là bài thử khó nhất.
- **Bộ phát tải nằm ngoài cụm** và giữ kết nối tới cả hai Envoy, nên khi một node chết nó vẫn tới được Envoy còn lại.

Mỗi node là một VM trên cloud UIT-IoT; các thành phần trên mỗi VM chạy bằng Docker Compose. Việc chuyển sang workload do k3s quản lý **nằm ngoài phạm vi đồ án** (xem [Ngoài phạm vi](#ngoài-phạm-vi)).

## Khái niệm hệ phân tán được thể hiện

| Khái niệm | Thể hiện trong đồ án | Bằng chứng |
|---|---|---|
| Đồng thuận, quorum | Ba Consul voter (Raft); mất leader vẫn cập nhật endpoint | [HA lookaside](docs/history/lookaside-ha.md) |
| Nhất quán so với sẵn sàng | Mất quorum quá lease 15 giây thì fail closed, trả `UNAVAILABLE` thay vì dùng endpoint cũ | [Cải thiện HA](docs/history/ha-improvement.md) |
| Phát hiện lỗi | Control plane probe health trực tiếp, độc lập với catalog; hội tụ khi mất leader từ ~34 giây xuống 2–3 giây | [Cải thiện HA](docs/history/ha-improvement.md) |
| Nhân bản, chịu lỗi | Hai control plane, hai Envoy; tắt VM1 gián đoạn 1,75–3,95 giây | [Báo cáo nghiệm thu](docs/history/cloud-validation-report.md) |
| Khám phá dịch vụ | Registry + xDS; caller giữ cấu hình hợp lệ gần nhất | [Lookaside](docs/history/lookaside.md) |
| Deadline xuyên chuỗi gọi | Một budget cho hàng đợi, xử lý cục bộ và lời gọi phụ thuộc | [Điều tra deadline](docs/history/deadline-investigation.md) |

## Đã hoàn thành

- Envoy ingress cân bằng tải giữa các instance điều phối.
- Service điều phối và các service xử lý với nhiều instance, workload cấu hình được và kiểm soát deadline.
- Lookaside bằng gRPC xDS: cập nhật endpoint động, baseline Round Robin.
- Đăng ký service với Consul, health checking và HA cho control plane.
- Triển khai trên ba VM; có công cụ tạo tải, đo đạc và thử lỗi/phục hồi.
- RL điều chỉnh trọng số qua xDS cho chặng A → B (bandit chọn mức mạnh tay β của một công thức chia theo latency), kèm đối chứng β cố định; chạy được trên cụm qua phương án `xds-rl` và `xds-beta`.

Kết quả và giới hạn: [báo cáo nghiệm thu](docs/history/cloud-validation-report.md), [thử nghiệm RL](docs/1-do-an/thu-nghiem-rl.md).

Tài liệu các giai đoạn trước (Go, k3s, AIOps, các đợt nghiệm thu cũ) nằm trong [`docs/history/`](docs/history/README.md); chúng giữ bằng chứng, không mô tả cách làm hiện tại.

## Ngoài phạm vi

Ghi vào mục hướng phát triển của báo cáo, không làm trong đồ án:

- Chạy ứng dụng bằng Kubernetes (k3s) và HPA. k3s đã được gỡ khỏi ba VM; ứng dụng chạy bằng Docker Compose.
- Observability đầy đủ (Prometheus, tracing) và AIOps.
- RL với nhiều instance B hơn (cụm có ba; mô phỏng và lab localhost thử tới 10).
- TLS/mTLS, nhiều zone/region.
