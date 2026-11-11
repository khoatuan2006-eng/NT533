# Tài liệu lịch sử

Các tài liệu trong thư mục này ghi lại những giai đoạn **đã qua** của dự án. Chúng giữ bằng chứng và quyết định thiết kế để trích dẫn trong báo cáo, nhưng **không mô tả cách làm hiện tại**: nhiều chỗ nói về Go, k3s, AIOps, một host, hay ba VM đã bị xóa. Cách làm hiện tại nằm ở [README](../../README.md), [hướng dẫn cho nhóm](../team-guide.md) và [tham chiếu thí nghiệm](../running.md).

Các lệnh trong những tài liệu này dùng `scripts/...` và `deploy/...` theo vị trí cũ; phần lớn file đó nay nằm trong [`legacy/`](../../legacy/README.md). Muốn chạy lại, lấy bản repo trước khi dọn theo hướng dẫn trong `legacy/README.md`.

| Tài liệu | Giai đoạn | Còn dùng để |
|---|---|---|
| [M1.md](M1.md) | 09/2026: client gRPC, Round Robin / Least Request, phân công ban đầu | Nguồn gốc `balancer.py`, `loadgen.py`, `summarize.py` |
| [balancing.md](balancing.md) | 09/2026: thiết kế Round Robin và Least Request phía client | Giải thích `LeastRequestBalancer`, nay dùng cho phương án `static-lr` |
| [project-direction.md](project-direction.md) | 25/10: định hướng ban đầu, có AIOps | Bối cảnh; phạm vi hiện tại đã thu hẹp (xem README, mục "Ngoài phạm vi") |
| [ingress.md](ingress.md) | 25/10: Envoy trước ba replica của một service | Bước đầu của tầng ingress |
| [microservices.md](microservices.md) | 25/10: service A → B/C với topology JSON | Mô tả lớp xử lý, deadline, workload |
| [acceptance.md](acceptance.md), [reference-verification.md](reference-verification.md) | 25/10: nghiệm thu một host trước khi nâng gRPC | Mốc "trước" của lỗi deadline |
| [reference-design.md](reference-design.md) | 25/10: đối chiếu cơ chế với tài liệu gRPC, Envoy | Nguồn tham khảo cho báo cáo |
| [deadline-investigation.md](deadline-investigation.md) | 26/10: lỗi deadline, nâng gRPC 1.84 | Bằng chứng mục "Deadline xuyên chuỗi gọi" |
| [lookaside.md](lookaside.md) | 26/10: lookaside xDS, control plane đọc file | Bằng chứng mục "Khám phá dịch vụ" |
| [lookaside-ha.md](lookaside-ha.md), [ha-improvement.md](ha-improvement.md) | 26/10: Consul ba server, health độc lập, HA control plane | Bằng chứng quorum, fail closed, 34 s → 2–3 s |
| [python-controlplane.md](python-controlplane.md) | 26/10: chuyển control plane từ Go sang Python | Lý do kiến trúc control plane hiện tại |
| [multihost.md](multihost.md) | 27/10: ba VM đầu tiên (đã xóa) | Lịch sử triển khai |
| [cloud-rebuild-2026-10-28.md](cloud-rebuild-2026-10-28.md) | 28/10: triển khai lại trên ba VM hiện tại | Chi tiết hạ tầng: cổng, LB cloud, DNS, security group |
| [cloud-validation-report.md](cloud-validation-report.md), [controlled-experiments.md](controlled-experiments.md) | 28/10: nghiệm thu và đối chứng trên cloud | Số liệu cloud đầu tiên (tắt VM1 1,75–3,95 s); kế hoạch k3s ở cuối đã bị thay thế |
