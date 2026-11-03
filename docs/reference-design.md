# Cơ sở thiết kế từ hệ thống tham khảo

Tài liệu này là bảng đối chiếu cơ chế đã triển khai với nguồn chính thức. Không coi một cấu hình mẫu hoặc một lần benchmark là bằng chứng vận hành production. Không tuyên bố đã sao chép kiến trúc nội bộ của Google hay LINE MAN khi chưa có tài liệu công khai đủ chi tiết.

## Các cơ chế đã đối chiếu và triển khai

| Vấn đề | Nguồn tham khảo | Áp dụng trong dự án | Bằng chứng |
|---|---|---|---|
| Deadline xuyên nhiều service | [gRPC Deadlines](https://grpc.io/docs/guides/deadlines/) | Caller đặt timeout; service lấy ngân sách còn lại từ context, giới hạn bởi timeout cấu hình và chỉ truyền phần còn lại xuống dependency. Hủy nhánh đang chạy khi request cha kết thúc | Test tổng budget, cancellation và delayed timeout callback |
| Proxy phải thực thi timeout gRPC | [Envoy 1.35.5 RouteAction.MaxStreamDuration](https://github.com/envoyproxy/envoy/blob/v1.35.5/api/envoy/config/route/v3/route_components.proto) và [Envoy timeout FAQ](https://www.envoyproxy.io/docs/envoy/latest/faq/configuration/timeouts.html) | `timeout: 0s` đi kèm `max_stream_duration`, `grpc_timeout_header_max` và offset; không chỉ tắt route timeout rồi cho rằng proxy đã thực thi deadline | Envoy binary validation và bài chạy xuyên proxy |
| Quá tải phải có backpressure | [Envoy circuit breaking](https://www.envoyproxy.io/docs/envoy/v1.35.5/intro/arch_overview/upstream/circuit_breaking) | Chặn số connection, request đang xử lý và pending request tại Envoy; backend tiếp tục có admission riêng. Không bật retry cho workload chưa có hợp đồng idempotency | Bài overload phân biệt RESOURCE_EXHAUSTED từ backend và UNAVAILABLE có marker `x-envoy-overloaded` từ proxy |
| Health cần tách khỏi lỗi từng request | [gRPC Health Checking](https://grpc.io/docs/guides/health-checking/) | Chỉ status health quyết định eligibility; không loại endpoint chỉ vì một RPC trả UNAVAILABLE | Test lỗi mô phỏng không làm thay đổi tập endpoint khỏe |
| Health trong static pool | [gRPC Health Checking — Check và Watch](https://grpc.io/docs/guides/health-checking/) | Static pool dùng một lần Check để bootstrap rồi Watch trên channel tái sử dụng. Stream mất thì đánh dấu chưa khỏe và reconnect bằng backoff có jitter | Test đổi NOT_SERVING → SERVING mà không gọi refresh/polling |
| Registry và health trong stack HA | [Consul registration](https://developer.hashicorp.com/consul/api-docs/agent/service), [gRPC health](https://grpc.io/docs/guides/health-checking/), [Envoy active health](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/health_checking) | Instance đăng ký với agent; xDS đọc membership consistent, probe gRPC trực tiếp trên IP:port đã resolve và publish health độc lập với RPC registry. Không nhân health checker theo mọi caller | NOT_SERVING khi business RPC vẫn trả lời; registry RPC bị treo không chặn đổi health; membership lease vẫn fail closed |
| HA registry/control plane | [Consul reliability](https://developer.hashicorp.com/consul/docs/concept/reliability), [gRPC load balancing](https://github.com/grpc/grpc/blob/v1.84.0/doc/load-balancing.md) | Ba server Consul chia sẻ registry qua Raft; hai replica xDS dùng nguồn registry chung; native gRPC kết nối tên DNS chứa hai địa chỉ | Dừng từng replica vẫn nhận được health update mới, cold caller thành công, Consul leader thay thế và khôi phục voter |
| Tránh chi phí tạo channel theo request | [gRPC Performance Best Practices](https://grpc.io/docs/guides/performance/) | Tái sử dụng channel/stub; không tạo connection mới cho từng RPC | Pool có lifecycle start/close; bài stress dùng connection tái sử dụng |
| Không đoán tầng gây chậm | [gRPC AsyncIO caveats](https://grpc.github.io/grpc/python/grpc_asyncio.html#blocking-code-in-asyncio) và [gRPC metadata/lifecycle](https://grpc.io/docs/what-is-grpc/core-concepts/) | Đo nhận response trước validation; thu queue time, handler duration, budget qua trailer và event-loop lag tại client. So sánh client Windows với client Linux | `timing_samples`, `failure_samples`, `max_client_event_loop_lag_ms`, `max_successful_server_duration_ms` trong report |

## Giá trị cấu hình là của thí nghiệm

Các con số sau là cấu hình mẫu của dự án, không phải khuyến nghị cố định từ Google hoặc Envoy:

Circuit breaker giới hạn trên từng proxy/cluster, không phải bộ đếm toàn cục có đồng thuận giữa các node. Admission tại mỗi backend vẫn là lớp bảo vệ cuối cùng.

- Deadline tối đa tại ingress: 5 giây; offset 10 ms để proxy có cơ hội kết thúc trước deadline caller. Caller có timeout ngắn hơn thì proxy dùng giá trị ngắn hơn sau offset. Đây không phải bảo đảm callback của client luôn được chạy đúng thời điểm.
- Generic stack có hai instance A, mỗi instance 50 active slot; circuit breaker đặt 100 request outstanding cho cluster. Stack M1 ba replica có ngưỡng 150 tương ứng cấu hình mẫu. Khi đổi capacity/topology phải rà soát các ngưỡng này.
- `max_pending_requests: 32`, `max_connections: 16`, `max_retries: 0` là giới hạn sample. Với HTTP/2, pending và active request có ý nghĩa khác nhau; không hiểu nhầm pending limit là giới hạn toàn bộ RPC.
- Health Watch reconnect bắt đầu từ health interval cấu hình, tăng gấp đôi, cap 30 giây và jitter ±20%. Đây là policy của adapter hiện tại, không tuyên bố trùng hằng số backoff của SDK gRPC.
- Watch UNIMPLEMENTED được xử lý fail closed và báo lỗi: hệ thống này yêu cầu health protocol đầy đủ. Khác với một số SDK có thể disable health checking khi gặp UNIMPLEMENTED; đây là lựa chọn có chủ đích để tránh tự coi endpoint chưa được kiểm tra là khỏe.

## Phần còn là adapter hoặc workaround

`DependencyPool` là adapter thử nghiệm của chế độ static, dùng protocol health chuẩn. Chế độ lookaside dùng `XdsDependencyPool` với channel xDS native của gRPC Core và control plane Python `grpc.aio` + protobuf `xds-protos==1.84.0`. Server phục vụ SotW ADS v3 theo protocol chuẩn của Envoy. Nguồn, cách chạy và giới hạn nằm trong [lookaside.md](lookaside.md); không suy ra Health Watch của static pool tự áp dụng cho xDS.

Stack [lookaside HA](lookaside-ha.md) bổ sung nguồn membership Consul, health theo service và hai replica xDS. Sau [điều tra độ trễ HA](ha-improvement.md), health quyết định EDS được probe trực tiếp tại control plane, độc lập với health được lưu trong catalog. Các policy freshness, ngưỡng health và service allowlist là quyết định của lab, không phải triển khai hoàn chỉnh do Google/HashiCorp cung cấp sẵn.

Workaround `use_oghttp2_codec: false` từng được dùng khi điều tra bộ runtime cũ. Ngày 26/10/2026 đã bỏ field này sau thử nghiệm gRPC 1.84.0; bài nghiệm thu hiện chạy codec mặc định của Envoy. Không còn dùng field work-in-progress để chốt nghiệm thu. Bằng chứng và giới hạn: [deadline-investigation.md](deadline-investigation.md).

## Cách đọc kết quả deadline

gRPC xác định kết quả phía client và server độc lập; server hoàn thành không có nghĩa client phải nhận thành công. Điều này được mô tả trong [RPC termination](https://grpc.io/docs/what-is-grpc/core-concepts/#rpc-termination). Tuy nhiên, nó không hợp thức hóa việc chấp nhận response OK đến muộn tại ứng dụng.

Do đó dự án giữ hai lớp đánh giá riêng: mã RPC quan sát được và deadline theo thời gian đo tại client. Report không đổi nhãn OK đến muộn thành thành công đúng hạn, không cộng thêm độ trễ cho phép tùy ý, không tăng timeout để làm gate xanh. Verdict tổng thể vẫn không đạt nếu còn late OK.

Server timing trong trailer chỉ mô tả khoảng thời gian tại một process, không phải distributed trace hoàn chỉnh và không cho phép so trực tiếp timestamp từ các máy có clock khác nhau. Event-loop lag lớn là bằng chứng scheduler có độ trễ, chưa đủ để kết luận nó giải thích toàn bộ thời gian bị mất. Network path, client runtime, proxy queue và callback cần được tách bằng thí nghiệm tiếp theo nếu số liệu còn chưa nhất quán.

## Nguyên tắc cho các thay đổi tiếp theo

Mỗi thay đổi đáng kể cần ghi: nguồn chính thức và phiên bản đã đối chiếu, hành vi mong đợi, khác biệt so với tham khảo, test tái hiện, và bằng chứng sau sửa. Giá trị tuning phải được gắn với capacity/workload cụ thể. Kết quả chưa đạt phải được giữ trong báo cáo; không dùng số lượng unit test làm chứng nhận độ tin cậy toàn hệ thống.
