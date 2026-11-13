# Cải thiện HA của lookaside

Các số đo và kiểm thử Go trong tài liệu này là mốc lịch sử trước khi chuyển ngôn ngữ. Thiết kế health/membership độc lập được giữ trong Python; xem [migration control plane và nghiệm thu mới](python-controlplane.md). Báo cáo cuối của Go giữ tại `results/before-python-controlplane-ha.json`; `results/lookaside-ha-acceptance.json` là báo cáo hiện hành.

Mục tiêu là giảm độ trễ cập nhật EDS sau lỗi Consul leader, giữ nguyên deadline RPC 3 giây, consistent reads và freshness membership 15 giây. Không dùng stale reads hoặc kéo dài thời gian giữ endpoint cũ để làm phép thử đạt.

## Nguyên nhân và thí nghiệm đối chứng

Bản trước có một phép đo hội tụ 34,266 giây. Thêm observer chạy cùng mạng container để ghi health cục bộ của agent, consistent catalog trên từng server, catalog qua agent và RPC thực xuyên ingress trong suốt lỗi. Trong phép đối chứng có instrument, cấu hình mặc định cho thời gian 17,719 giây; giảm riêng `rpc_client_timeout` xuống 2 giây cho thời gian 12,797 giây. Các số này thể hiện biến thiên giữa lần chạy, không phải một hằng số failover.

Chỉ thay `raft_multiplier=1` và timeout RPC nội bộ 2 giây vẫn không giải quyết được: một trial mất 28,235 giây, trial sau không hội tụ trong 45 giây. Giữ bằng chứng tại `results/ha-fast/report.json`; không nhận cấu hình này là bản sửa hoàn chỉnh.

Timeline cho thấy health cục bộ có thể đã critical nhưng catalog còn giữ passing khoảng 26 giây. [Consul 2.1.0 local state](https://github.com/hashicorp/consul/blob/v2.1.0/agent/local/state.go) khóa trạng thái trong `SyncChanges` và gọi RPC khi sync service/check; [RPC client](https://github.com/hashicorp/consul/blob/v2.1.0/agent/consul/client.go) có vòng retry/hold riêng. Timeout HTTP ở adapter không hủy trực tiếp các RPC nội bộ đó. Vì vậy cần tách health quyết định eligibility khỏi độ trễ đồng bộ catalog.

## Thay đổi

Consul cung cấp membership có quorum. Controller xDS kiểm tra health gRPC trực tiếp trên IP:port đã resolve, bằng đúng tên `processing.v1.ProcessingService/<service-id>`. Hai vòng chạy độc lập; HTTP registry chậm không giữ khóa hoặc ngăn publish health của endpoint đã biết. Khi có thay đổi health, controller đánh thức vòng publish ngay; ticker 1 giây kiểm tra freshness và làm dự phòng.

Health dùng `Check` chuẩn, channel tái sử dụng, interval 1 giây, timeout 500 ms và ngưỡng hai lần. Endpoint ban đầu chưa được coi là healthy; lỗi/NOT_SERVING/timeout làm mất eligibility. Khi membership bị xóa, probe bị hủy. Khi membership quá hạn 15 giây, health vẫn SERVING cũng không giữ endpoint trong EDS.

Đây là kiểm tra tập trung theo số endpoint × số replica control plane, không polling từ mọi caller. [gRPC Health Checking](https://grpc.io/docs/guides/health-checking/) nêu unary Check phù hợp cho monitoring/load balancing tập trung. [Envoy active health checking](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/health_checking) cũng kết hợp discovery với kiểm tra health độc lập. Phần reconcile và policy của controller là mã dự án; protocol health, gRPC channel và ADS/cache dùng thư viện chuẩn.

`configs/consul-ha.json` đặt Raft multiplier 1 và RPC client timeout 2 giây cho môi trường LAN của lab. [Tài liệu HashiCorp về Raft](https://developer.hashicorp.com/consul/docs/reference/architecture/server) giải thích default multiplier 5 và cấu hình 1; [tham số RPC](https://developer.hashicorp.com/consul/docs/reference/agent/configuration-file/general) giải thích timeout mặc định 60 giây. Giá trị 2 giây là budget RPC nội bộ của lab, không phải khuyến nghị cố định cho WAN hoặc blocking query. Tuning này giúp election/connection failover, còn controller health độc lập mới xử lý độ trễ catalog đã quan sát.

## Tái lập và tiêu chí

```powershell
# Đối chứng: registry health từ catalog, thông số mặc định
.\.venv\Scripts\python.exe scripts/investigate_ha.py --label baseline --controller-mode catalog --raft-multiplier 5 --rpc-timeout 60s --wsl Ubuntu-22.04 --compose-binary /mnt/d/LoadBalance/.venv/acceptance-tools/docker-compose-linux-x86_64

# Bản cải thiện: controller health trực tiếp, lặp ba lần lỗi leader
.\.venv\Scripts\python.exe scripts/investigate_ha.py --label active --controller-mode active --raft-multiplier 1 --rpc-timeout 2s --repetitions 3 --wsl Ubuntu-22.04 --compose-binary /mnt/d/LoadBalance/.venv/acceptance-tools/docker-compose-linux-x86_64

# Nghiệm thu toàn tuyến, gồm mất quorum và phục hồi
.\.venv\Scripts\python.exe scripts/acceptance_lookaside_ha.py --wsl Ubuntu-22.04 --compose-binary /mnt/d/LoadBalance/.venv/acceptance-tools/docker-compose-linux-x86_64
```

Mỗi trial giữ leader đóng băng 45 giây, có traffic thực xuyên ingress, rồi phục hồi node và chờ đủ ba voter trước trial sau. Gate của bản active: hội tụ health tối đa 10 giây, ít nhất 500 RPC quan sát, không lỗi và không OK muộn trong cửa sổ quan sát. Ngưỡng 10 giây là mục tiêu của dự án, không phải mặc định vendor.

Observer mới dùng `perf_counter` cho latency và lưu cả chênh lệch với wall clock. Hai số OK trên 3 giây trong trial tuning-only cũ được đo bằng wall clock; giữ nguyên dữ liệu, chưa quy chúng cho lỗi runtime hay chỉnh đồng hồ. Gate deadline của observer mới và bài nghiệm thu chính đều dùng monotonic clock.

Kiểm thử Go bổ sung chứng minh: registry RPC bị treo không chặn đổi health; endpoint còn mở transport nhưng không trả lời health bị rút; health sống không vượt qua freshness lease; xóa membership hủy probe. Các thử nghiệm vẫn chỉ trên một host và workload tổng hợp, chưa thay thế kiểm chứng đa host/zone hoặc SLO production.

## Kết quả nghiệm thu — 26/10/2026

| Phép thử | Trước | Sau |
|---|---|---|
| Nghiệm thu toàn tuyến: health update khi mất leader | 34,266 giây | **2,015 giây** |
| Lỗi leader giữ 45 giây, có traffic liên tục | Cấu hình tuning-only còn 28,235 giây và một trial không đạt 45 giây | Ba trial **2,969 / 3,047 / 2,157 giây**, đều dưới gate 10 giây |
| RPC trong ba trial active | Chưa có phép đo tương đương trong nghiệm thu gốc | **7.386 OK**, không lỗi, không OK sau timeout 3 giây |
| Tải toàn tuyến nhẹ/nặng/stress | 6.300 OK | **6.300 OK**, không OK sau deadline |
| Mất hai trong ba voter | Hết hạn membership, 40 UNAVAILABLE | Vẫn giữ hành vi fail closed: **40/40 UNAVAILABLE** |
| Phục hồi quorum | 7,390 giây | 3,360 giây |
| Kiểm thử và cleanup | 91 Python tests | **91/91 Python tests**, bộ Go mới đạt; project được dọn sạch |

Các số trong bảng là lần nghiệm thu cải thiện HA trước đợt refactor, được giữ tại `results/before-code-cleanup-ha-acceptance.json`. Kết quả toàn tuyến mới nhất nằm ở `results/lookaside-ha-acceptance.json`; sau refactor phép thử leader đạt 3,109 giây và toàn bộ gate vẫn đạt, xem [rà soát cấu trúc mã](../1-do-an/kien-truc-code.md). Đối chiếu mốc trước cải thiện: `results/lookaside-ha-before-fast-failover.json`. Ba trial có traffic liên tục và gate riêng: `results/ha-active/report.json`; raw observations nằm ở các `trial-*-timeline.json` cùng thư mục. Các trial dùng leader consul-1, consul-2, rồi consul-1; chưa phải chứng nhận mọi thứ tự lỗi hay mọi node đều cho cùng độ trễ.

Không suy ra đây là cải thiện chỉ từ Raft tuning: thử nghiệm tuning-only đã không đạt. Thay đổi chính là tách authoritative membership khỏi health đang đồng bộ chậm, và không để RPC đọc registry giữ vòng publish health. Tất cả thời gian hội tụ trong bảng đều gồm 24 RPC xác nhận graph, không phải thời gian bầu leader tách riêng. Số liệu là kết quả lab một host, chưa phải cam kết SLO production hoặc kiểm chứng partition nhiều zone.
