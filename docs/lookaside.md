# Lookaside bằng gRPC xDS

Stack này giữ Envoy ở đầu vào và dùng xDS native cho lời gọi nội bộ. Đây là bước đầu của lớp lookaside; chưa có AIOps hoặc triển khai nhiều máy.

Đã có bước tiếp theo: [registry, health controller và HA](lookaside-ha.md), với Consul ba server và hai replica xDS. Tài liệu bên dưới giữ cấu hình/prototype dùng file và kết quả nghiệm thu của bước đầu; các giới hạn registry/health/HA nêu cho prototype này không đại diện cho stack HA mới.

```text
Client ──RPC──> Envoy ingress ──RPC──> A-1 / A-2
                                      │
                                      ├──RPC trực tiếp──> B-1 / B-2
                                      └──RPC trực tiếp──> C-1 / C-2

                         A-1 / A-2 <──ADS/xDS── Control plane
                                                    │
                                          discovery config + DNS
```

Control plane không nằm trên đường truyền request nghiệp vụ. Mỗi dependency có một channel tái sử dụng `xds:///service-b` hoặc `xds:///service-c`. gRPC Core nhận resource và chọn endpoint bằng Round Robin; mã Python không tự chọn lại backend.

## Nguồn và lựa chọn triển khai

| Cơ chế | Nguồn chính thức | Áp dụng |
|---|---|---|
| Python xDS caller | [gRPC Python xDS 1.84.0](https://github.com/grpc/grpc/tree/v1.84.0/examples/python/xds) | Bootstrap qua `GRPC_XDS_BOOTSTRAP`, target `xds:///...`, channel plaintext cho lab |
| ADS và snapshot | [Envoy xDS protocol](https://www.envoyproxy.io/docs/envoy/latest/api-docs/xds_protocol.html), [gRPC xds-protos 1.84.0](https://pypi.org/project/xds-protos/1.84.0/) | Python `grpc.aio` phục vụ SotW ADS v3 với protobuf chính thức; server quản lý subscription, nonce, ACK/NACK và snapshot |
| xDS resource | [gRFC A27](https://github.com/grpc/proposal/blob/master/A27-xds-global-load-balancing.md), [gRPC xDS features](https://github.com/grpc/grpc/blob/v1.84.0/doc/grpc_xds_features.md) | LDS ApiListener + HCM inline route → CDS EDS/ROUND_ROBIN → EDS IP endpoints |
| Ví dụ vận hành Kubernetes | [LINE MAN/Wongnai xDS](https://github.com/wongnai/xds) | Tham khảo việc tách registry khỏi caller; không dùng nguyên controller Kubernetes cho Compose |

Control plane đã chuyển từ Go sang Python. Runtime khóa `grpcio==1.84.0`, `xds-protos==1.84.0`, `protobuf==7.36.2`, `httpx==0.28.1` trong `pyproject.toml`. Docker dùng Python 3.12 slim và chạy kiểm thử control plane trước khi hoàn tất image runtime không có dev dependencies. Envoy proxy 1.35.5 ở ingress giữ nguyên.

Phần của dự án gồm adapter discovery/Consul, resolve DNS, tạo resource, snapshot store và server SotW ADS. Đây là implementation Python giới hạn ở LDS/CDS/EDS cho topology hiện tại; không thay bằng discovery protocol riêng. Delta ADS trả UNIMPLEMENTED. Poll 1 giây, budget fetch/resolve/build 4 giây, cluster connect timeout 1 giây là policy lab, không phải khuyến nghị cố định từ Google/Envoy.

Mỗi ADS stream theo dõi subscription và nonce riêng cho từng resource type. Caller yêu cầu service B được nhận B ngay, không chờ subscribe C. ACK/NACK cùng candidate không tạo vòng lặp phản hồi; snapshot mới vẫn được gửi và subscription mới vẫn được xử lý. Snapshot không đổi không push lại. Kiểm thử qua stream gRPC thật nằm trong `tests/test_controlplane_ads.py`; mốc lỗi của implementation cũ vẫn được giữ ở `results/lookaside-before-partial-subscription-fix.json`.

## Cấu hình và chạy

Từ thư mục gốc, với Docker Compose:

```sh
docker compose -f deploy/compose.microservices.yaml -f deploy/compose.lookaside.yaml up --build -d
python scripts/check_microservices.py
docker compose -f deploy/compose.microservices.yaml -f deploy/compose.lookaside.yaml down --remove-orphans --volumes
```

Trong môi trường Windows hiện tại dùng Docker qua WSL, có thể chạy nghiệm thu tự dựng và dọn stack:

```powershell
.\.venv\Scripts\python.exe scripts/acceptance_lookaside.py --wsl Ubuntu-22.04 --compose-binary /mnt/d/LoadBalance/.venv/acceptance-tools/docker-compose-linux-x86_64
```

`configs/topology.compose.json` mô tả luồng xử lý, cấu hình local workload và địa chỉ phục vụ. Khi `DISCOVERY_MODE=xds`, caller không dùng danh sách địa chỉ dependency trong topology để route; nó chỉ dùng tên service.

`configs/discovery.compose.json` là danh sách membership do control plane quản lý:

```json
{
  "schema_version": 1,
  "services": {
    "service-b": ["b-1:50051", "b-2:50051"],
    "service-c": ["c-1:50051", "c-2:50051"]
  }
}
```

Thay danh sách B bằng một endpoint để rút instance, hoặc `[]` để biểu diễn không còn instance. Control plane resolve DNS thành IP để publish EDS. Cấu hình sai schema, endpoint, duplicate key hoặc lỗi DNS bị từ chối; snapshot hợp lệ gần nhất được giữ. Hash nội dung resource làm version, không push lại nếu nội dung không thay đổi. Log `snapshot_published`, `snapshot_rejected`, `xds_ack`, `xds_nack` giúp đối chiếu.

Compose bind từng file nên khi sửa runtime config cần ghi lại nội dung file đang mount; editor dùng atomic rename có thể khiến container tiếp tục nhìn inode cũ. Trong vận hành thực nên mount thư mục cấu hình hoặc dùng registry/API có cơ chế cập nhật nguyên tử. Bài nghiệm thu chỉ sửa bản sao trong `results/`, không sửa cấu hình mẫu.

## Hành vi lỗi và giới hạn

- Caller đã nhận cấu hình giữ resource trong bộ nhớ của gRPC khi mất control plane; không cache xuống đĩa. Caller mới không có cấu hình phải lỗi trong deadline, không âm thầm chuyển sang địa chỉ tĩnh.
- Mất kết nối backend được gRPC transport phát hiện và chọn instance còn kết nối được. Có thể có lỗi RPC trong khoảng phát hiện; không tự retry request nghiệp vụ.
- EDS hiện biểu diễn membership đủ điều kiện theo file cấu hình, **chưa có controller thu Health Watch của backend**. Transport còn sống nhưng ứng dụng báo NOT_SERVING chưa được tự rút khỏi EDS. Không suy ra health đầy đủ từ connectivity hoặc từ nhãn HEALTHY của EDS.
- Chưa có service registry tự đăng ký, control plane HA, đồng bộ cấu hình nhiều replica, TTL cho last-good config, TLS/mTLS, phân quyền hay weighted policy/AIOps. Cấu hình plaintext và một nhóm snapshot dùng chung chỉ dành cho lab mạng nội bộ.
- Local workload vẫn giữ admission, deadline xuyên chuỗi và hủy tác vụ. Không tăng deadline để bù thời gian chờ discovery.

## Nghiệm thu

`scripts/acceptance_lookaside.py` kiểm chứng SDK và server thực, không mock giao thức xDS: đường Client → Envoy → A → B/C, tải nhẹ/nặng, thêm/rút endpoint khi caller còn chạy, cấu hình sai, membership rỗng, backend dừng/phục hồi, control plane dừng/phục hồi, caller mới không có cache, và deadline ngắn.

Kết quả nằm ở `results/lookaside-acceptance.json`; log protocol nằm ở `results/lookaside-controlplane.log`. Chỉ báo đạt khi toàn bộ kịch bản và cleanup đạt. Không dùng số unit test để thay cho nghiệm thu toàn tuyến.

### Kết quả lịch sử trước migration Python — 26/10/2026

Mốc bên dưới thuộc control plane Go, giữ tại `results/before-python-controlplane-lookaside.json`. Kết quả của Python xem [migration và kiểm chứng](python-controlplane.md); runner ghi báo cáo hiện hành vào `results/lookaside-acceptance.json`.

Bài chạy thực đã đạt `passed: true`, `cleanup: passed` trên một host Windows với các container Linux trong WSL. Client nghiệm thu chạy trên Windows; các caller xDS là service A trong container. Không phải thử nghiệm nhiều máy.

| Kịch bản | Kết quả quan sát |
|---|---|
| Tải nhẹ / nặng / stress | 1.000 / 300 / 5.000 request đều OK; concurrency lần lượt 32 / 16 / 64; không OK nào nhận sau timeout 3 giây |
| Rút B-2 rồi thêm lại | Caller hội tụ sang B-1 rồi nhận lại cả B-1/B-2; container ID và thời điểm khởi động process A không đổi |
| Cấu hình sai | Control plane ghi `snapshot_rejected`; traffic tiếp tục theo snapshot hợp lệ cũ |
| Membership B rỗng | 40/40 request UNAVAILABLE; khôi phục membership thì graph phục hồi |
| B-2 dừng / phục hồi | Native transport hội tụ sang B-1 và sử dụng lại B-2 sau khi khởi động |
| Control plane dừng | Các caller đã nhận snapshot vẫn hoàn thành graph; caller mới không có cache trả UNAVAILABLE sau khoảng 164 ms, không fallback static |
| Control plane trở lại | Caller nhận snapshot mới chỉ có B-1, sau đó nhận lại B-1/B-2 |
| Timeout 1 ms | 100/100 DEADLINE_EXCEEDED; bài kiểm tra graph sau đó phục hồi |
| Giao thức xDS | Có ACK từ SDK, không có NACK; cấu hình sai bị chặn trước publish |
| Kiểm thử mã nguồn | 88 kiểm thử Python; kiểm thử Go chạy trong Docker build, gồm regression ADS đăng ký từng service |

Các phép thử đổi membership và lỗi backend chờ hội tụ tối đa 45 giây rồi mới xác nhận phân phối. Chưa đo toàn bộ error rate trong khoảng chuyển tiếp, chưa chứng minh không mất request khi đổi cấu hình, và chưa đo soak/partition kéo dài.

Phiên bản Go trước đây đã sửa lỗi cache ADS và cache biên dịch. Bản hiện tại đã chuyển sang Docker build Python với kiểm thử bắt buộc; không còn Go builder. Script cho phép bước build tối đa 1.800 giây và ghi log liên tục; giới hạn này độc lập với deadline RPC, không thay đổi timeout request.
