# Hệ thống cân bằng tải phân tán

Đồ án môn Hệ tính toán phân bố. Hệ thống cân bằng tải hai tầng: **Envoy cân bằng request vào service điều phối** và **lookaside (gRPC xDS) cân bằng lời gọi từ service điều phối tới các service xử lý**.

## Câu hỏi của đồ án

**Câu hỏi chính:** hệ thống cân bằng tải hai tầng này tiếp tục phục vụ thế nào khi từng thành phần phân tán bị lỗi — backend, máy chủ, control plane, registry, hoặc khi mạng bị phân mảnh?

**Câu hỏi mở rộng (RL):** một bộ điều khiển học được có thể điều chỉnh trọng số qua control plane nhân bản mà vẫn an toàn khi chính nó lỗi hoặc thiếu dữ liệu không?

## Kiến trúc

### Kiến trúc logic: request đi đâu, ai quyết định

![Kiến trúc logic: client gọi hai Envoy, Envoy chia tải vào service A, A gọi trực tiếp service B và C; Consul và control plane phát endpoint cho A qua xDS](docs/images/architecture-logical.svg)

- Có **hai lần cân bằng tải**: Envoy chọn a-1 hay a-2; sau đó chính A chọn instance B và C rồi gọi thẳng tới đó (mô hình lookaside).
- **Control plane không nằm trên đường đi của request.** Nó kiểm tra health, lấy danh sách instance từ Consul, và chỉ phát cập nhật khi có thay đổi. Control plane chết thì A vẫn dùng danh sách đã có.
- **RL là phần mở rộng tùy chọn**: nó chỉ đổi trọng số mà control plane phát ra, không tác động trực tiếp lên request.

### Bố trí trên ba node: một node chết thì mất gì

![Bố trí trên ba node: node1 và node2 mỗi node có Consul, control plane, Envoy, một bản A và một backend; node3 có Consul và hai backend; bộ phát tải nằm ngoài cụm](docs/images/architecture-nodes.svg)

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
- Nguyên mẫu Q-learning điều chỉnh trọng số qua xDS, chạy trong lab với một caller và hai instance.

Kết quả và giới hạn: [báo cáo nghiệm thu](docs/history/cloud-validation-report.md), [thử nghiệm RL](docs/rl-lookaside-experiment.md).

Tài liệu các giai đoạn trước (Go, k3s, AIOps, các đợt nghiệm thu cũ) nằm trong [`docs/history/`](docs/history/README.md); chúng giữ bằng chứng, không mô tả cách làm hiện tại.

## Cách nhóm làm việc (11–18/11/2026)

Mọi thành viên đều tự chạy, kiểm tra, đánh giá và sửa; không ai chỉ nhận kết quả của người khác. Mỗi người làm theo [hướng dẫn cho nhóm](docs/team-guide.md):

1. **Chạy**: thử lỗi (`run_faults.py`) và đo tải (`run_load.py`) trên ba VM.
2. **Kiểm tra**: đọc bảng kết quả, đối chiếu với trace gốc, tự giải thích từng con số.
3. **Đánh giá**: kết luận đạt / không đạt theo tiêu chí, nêu nguyên nhân cho ô không đạt.
4. **Sửa để cải thiện**: đặt giả thuyết, sửa trên nhánh riêng, triển khai, chạy lại đúng ô đó, ghi cặp trước / sau.

Cả nhóm dùng chung một cụm ba VM nên **mỗi lúc chỉ một người chạy**; các script tự giữ khóa cụm và từ chối nếu người khác đang chạy. Báo trong nhóm trước khi triển khai code mới, vì triển khai khởi động lại toàn bộ cụm.

Để báo cáo không trùng lặp, mỗi người đứng tên một phần (vẫn tự chạy cả phần còn lại để hiểu):

| Người | Đứng tên trong báo cáo |
|---|---|
| Đạt | Thử lỗi và phân mảnh mạng: gián đoạn, phục hồi, fail closed |
| Lầu | Tải và cân bằng: điểm bão hòa, Round Robin so với Least Request, backend chậm |
| Khoa | RL (phần mở rộng) và ghép báo cáo |

Ô đã biết là "không đạt" để cả nhóm luyện quy trình sửa: phục hồi chậm sau khi gỡ phân mảnh node 1 (xem [tham chiếu thí nghiệm](docs/running.md#vấn-đề-đã-biết)).

### Tiêu chí cho phần RL

- RL không nằm trên đường xử lý request; khi telemetry cũ, sai hoặc thiếu mẫu thì quay về Round Robin.
- Trọng số mỗi endpoint giới hạn 30–70%; không chạy exploration trên cấu hình mặc định.
- Chỉ kết luận RL tốt hơn khi vượt policy theo quy tắc trên nhiều seed. Kết quả hiện tại chưa cho thấy điều đó.

### Cách đo và báo cáo

Chỉ số: số request theo instance, goodput, p50/p95/p99, tỷ lệ lỗi/timeout, thời gian cập nhật endpoint và thời gian phục hồi. Giữ trace cả thành công lẫn lỗi; không kết luận tốt hơn chỉ từ latency của request thành công. Phương pháp và định nghĩa chỉ số: [thực nghiệm đối chứng](docs/history/controlled-experiments.md). Phần kế hoạch k3s và ma trận E1–E8 trong tài liệu đó được thay bằng bảng trên.

## Ngoài phạm vi

Ghi vào mục hướng phát triển của báo cáo, không làm trong tuần này:

- Chạy ứng dụng bằng Kubernetes (k3s) và HPA. Ba VM có cài sẵn k3s từ trước, nhưng đồ án không dùng; ứng dụng chạy bằng Docker Compose.
- Observability đầy đủ (Prometheus, tracing) và AIOps.
- RL cho số backend bất kỳ.
- TLS/mTLS, nhiều zone/region.
