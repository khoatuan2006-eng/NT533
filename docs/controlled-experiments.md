# Thực nghiệm đối chứng và demo tái lập

> **Cập nhật 11/11/2026.** Phần "Kế hoạch đợt đánh giá tiếp theo" (k3s, ma trận E1–E8, phân công E-số) ở cuối tài liệu này **đã được thay thế**: phạm vi mới là lab ba node trên một máy, xem [README](../README.md) và [hướng dẫn chạy](running.md). Các protocol, tiêu chí và số liệu của đợt cloud ngày 28/10 phía trên vẫn có hiệu lực.

Các protocol và số liệu bên dưới mô tả đợt thử nghiệm đã thực hiện. [Kế hoạch đợt đánh giá tiếp theo](#ke-hoach-tiep-theo) ở cuối tài liệu bổ sung kịch bản, cách đo và các phần runner cần hoàn thiện; chưa phải kết quả đã chạy.

## Câu hỏi và giả thuyết

H1: mất một Envoy/VM đầu vào không buộc client đổi địa chỉ. Cổng client là Floating IP của LB cloud; hai Envoy nằm trên hai VM khác nhau. Kết nối HTTP/2 đang đi qua VM bị mất có thể bị ngắt; HA không đồng nghĩa không có RPC lỗi.

H2: lookaside duy trì khả năng xử lý khi một backend hoặc một control plane mất. Static có health checking cũng phải được phép loại backend hỏng. Không dùng baseline cố tình thiếu health checking để làm xDS có vẻ tốt hơn.

H3: với cùng tải, chi phí đường gọi của hai cách discovery nằm trong mức chấp nhận được. Không giả định lookaside phải có throughput cao hơn static. Giá trị cần chứng minh của lookaside còn là cập nhật membership mà không sửa danh sách endpoint tại caller.

## Biến kiểm soát

Giữ nguyên placement, image ID, topology, workload, deadline 3 giây, giới hạn concurrency của service, Envoy và nền Consul/control plane đang chạy. Chỉ đổi `DISCOVERY_MODE` trên cả A1 và A2. Static dùng `StaticDependencyPool` với danh sách cấu hình, health Check/Watch và Round Robin trong Python; xDS dùng grpc-core Round Robin cùng discovery/control plane. Vì implementation channel/health khác nhau, đây là so sánh hai cấu hình hoàn chỉnh, không phải phép đo riêng chi phí một RPC tới control plane.

Hai workload light/heavy; hai mức tải cố định 50 và 150 request/giây; mỗi lượt 20 giây đo, 100 request warmup tuần tự trước đo. Hai lần lặp mỗi cặp workload/tải; thứ tự static→xDS rồi xDS→static. Tổng 16 lượt. Đây là thực nghiệm đồ án ngắn, không đủ để khẳng định khác biệt có ý nghĩa thống kê.

Sau khi cấu hình HA xong, chạy đối chứng xác nhận qua Floating IP trực tiếp: light/heavy ở 150 RPS, 20 giây/lượt, hai lần lặp và cùng thứ tự đảo. Tổng 8 lượt, 24.000 RPC. Trong bộ xác nhận này không chạy thêm probe kết nối hoặc fault; các phép thử nghiệm thu hoàn tất trước khi bắt đầu. Bộ 16 lượt ban đầu qua tunnel là pilot để kiểm tra dải tải và phát hiện nhiễu, được giữ riêng, không trộn median với bộ xác nhận.

Bộ chạy ban đầu dùng client Windows qua cùng một SSH tunnel/VPN tới Envoy VM1 cho cả hai mode. Kết quả chứa độ trễ Windows, VPN và SSH; không diễn giải thành latency mạng nội bộ thuần túy. Phép thử HA dùng Floating IP trực tiếp, không dùng tunnel phụ thuộc VM1. Không chạy fault đồng thời với đối chứng tải ổn định. Mọi kiểm tra kết nối phụ chạy trong thời gian benchmark đều cần ghi là nhiễu nền; số đo pilot không thay thế benchmark môi trường cô lập.

## Tiêu chí đạt định trước

| Tình huống | Tiêu chí | Bằng chứng cần lưu |
|---|---|---|
| Chạy bình thường | Đủ graph A→B/C, đúng request ID, cả hai replica mỗi service được gọi | Kết quả check graph, image ID, topology |
| Tải ổn định | Không drop do generator; error ≤1%; goodput theo cửa sổ offered ≥98% tải đặt; P99 thành công < deadline 3 giây | JSONL từng RPC và JSON tổng hợp từng lượt |
| Backend dừng | Có request thành công khi backend được xác nhận dừng; đạt cửa sổ 5 giây không lỗi trước khi khởi động lại backend | Mốc lệnh stop/hoàn tất, trace RPC và backend instance |
| Mất VM1 | Nova xác nhận SHUTOFF; cùng Floating IP vẫn có RPC thành công; gián đoạn RPC quan sát ≤30 giây; có cửa sổ ổn định 5 giây trước start | Nova status, timestamp dispatch/confirmed, trace RPC, danh sách instance còn sống |
| Mất một control plane | Caller mới và cập nhật health vẫn hoạt động | Cold-channel probe và ACK xDS |
| Mất Consul leader | Bầu leader mới, cập nhật health hội tụ, graph phục hồi | Raft membership và graph probe |
| Mất quorum quá lease | Hệ thống từ chối endpoint hết lease, sau khôi phục quorum phục hồi graph | UNAVAILABLE và graph sau khôi phục |
| Kết thúc demo | Cả ba VM ACTIVE, ba voter Consul, các container LoadBalance chạy, mode xDS; k3s node trở lại Ready | Snapshot sau restore và kiểm tra graph |

Ngưỡng 30 giây là mục tiêu đồ án, không phải cam kết SLA. Nếu lỗi do mạng quản trị làm cleanup không xác nhận được, toàn bài vẫn chưa đạt cho tới khi kiểm tra phục hồi riêng hoặc chạy lại; không sửa dữ liệu lỗi thành pass.

## Chỉ số và cách tính

`controlled_probe.py` tạo arrival theo lịch cố định, không đợi RPC trước kết thúc. Có giới hạn inflight 256; arrival vượt giới hạn được ghi `GENERATOR_OVERFLOW` và làm lượt đo không hợp lệ. Thời gian đo dùng monotonic clock; Unix timestamp để đối chiếu sự kiện bên ngoài. Từng mẫu chứa scheduled/start/completed, code, latency và instance đã trả kết quả. Không retry RPC ở application.

- Goodput theo offered window = số OK / thời lượng đặt, cho phép đợi các RPC đã phát kết thúc. Goodput thực tế = số OK / thời gian tới khi kết thúc toàn bộ RPC; báo cáo cần phân biệt hai mẫu số.
- RPC error rate = số RPC không OK / số đã gọi. Failure gồm generator drop dùng toàn bộ arrival làm mẫu số.
- P95/P99 theo nearest rank, ghi riêng trên OK và toàn bộ RPC. Thiếu mẫu trả null. Không dùng latency chỉ trên OK để che lỗi timeout.
- Gián đoạn RPC quan sát = từ request đầu tiên lỗi sau dispatch tới đầu cửa sổ 5 giây toàn OK sau lỗi cuối, chỉ xét trước khi khôi phục thành phần lỗi. Nếu không có lỗi, ghi 0 (không ghi nhận gián đoạn ở độ phân giải lấy mẫu), không kết luận failover tức thì. Không đủ cửa sổ quan sát trả null.
- Ghi thêm thời gian dispatch→ổn định và xác nhận fault→ổn định, với cửa sổ bắt đầu sau khi fault được xác nhận. Dispatch không phải thời điểm máy vật lý ngừng chạy; các metric này chứa độ trễ SSH/API. Không gọi chúng là độ trễ health monitor nội bộ. Phân biệt mốc điều khiển và thời gian gián đoạn trước khi so sánh hai mode.
- Lưu schedule lag P99 vì client Windows có thể phát theo cụm khi event loop chậm. Không xem việc đủ số request đặt là bằng chứng arrival hoàn toàn đều.

Bộ pilot/fault ban đầu dùng probe schema 1: timer asyncio trên Windows có thể thức dậy sớm khoảng một độ phân giải clock; raw trace giữ cả scheduled và started để kiểm tra. Bộ xác nhận qua Floating IP dùng schema 2, lặp kiểm tra monotonic trước khi dispatch để không phát sớm. Kiểm thử tích hợp thật với gRPC xác nhận mỗi arrival được ghi và không có started < scheduled. Vẫn báo độ trễ phát muộn; không trộn schema 1/2 thành một mẫu thống kê chung.

## Chạy lại

Trước hết làm theo `cloud-rebuild-2026-10-28.md`, kiểm tra graph và lưu resource snapshot. Chỉ chạy một bộ tải/fault tại một thời điểm. Dùng thư mục kết quả mới mỗi lần; công cụ từ chối ghi đè JSONL.

```powershell
.venv\Scripts\python.exe scripts/controlled_suite.py --address 127.0.0.1:17080 --mode-hook scripts/cloud_mode.py --output results/my-controlled-run --rates 50 150 --duration 20 --repetitions 2
.venv\Scripts\python.exe scripts/controlled_suite.py --address 192.168.120.109:80 --mode-hook scripts/cloud_mode.py --output results/my-confirmation-run --rates 150 --duration 20 --repetitions 2
.venv\Scripts\python.exe scripts/fault_probe.py --fault backend --mode static --output results/backend-static.json
.venv\Scripts\python.exe scripts/fault_probe.py --fault backend --mode xds --output results/backend-xds.json
.venv\Scripts\python.exe scripts/fault_probe.py --fault vm1 --mode xds --credentials C:/Users/Khoa/Downloads/clouds.yaml --output results/vm1-xds.json
.venv\Scripts\python.exe scripts/fault_probe.py --fault vm1 --mode xds --prime-vm1 --credentials C:/Users/Khoa/Downloads/clouds.yaml --output results/vm1-primed-xds.json
.venv\Scripts\python.exe scripts/report_controlled.py results/my-confirmation-run --output results/my-comparison.md
.venv\Scripts\python.exe scripts/report_faults.py results/k3s-faults --output results/my-fault-report.md
```

`cloud_mode.py` chờ health của cả A1/A2; warmup của probe kiểm tra toàn tuyến trước khi đo. Mode hook dùng inventory và topology hiện tại, không dùng IP cụm đã xóa. `controlled_suite.py` luôn thử trả mode xDS khi kết thúc hoặc lỗi. `fault_probe.py` luôn thử start lại backend/VM trong finally; operator vẫn phải kiểm tra trạng thái cloud và graph sau đó. Tắt VM1 cũng tạm dừng k3s control plane đang dùng chung VM, vì vậy chỉ chạy trong lab đã được cho phép.

`--prime-vm1` tạm vô hiệu hóa member Envoy VM2, mở và warmup kết nối qua Envoy VM1, rồi bật lại VM2 trước khi gây lỗi. Sau khi VM1 SHUTOFF, thử cả kết nối đang có và một kết nối mới. Bài này kiểm tra reconnect của đường ingress rõ hơn việc tình cờ mở kết nối sẵn qua VM2. Lưu trạng thái DEGRADED, member VM1 ERROR và VM2 ONLINE trong JSON sự kiện; sau restore phải trở lại ONLINE.

Nếu SSH reset khi đổi mode trước khi phát tải, có thể tiếp tục bằng `--resume`: chỉ dùng lại lượt đã hoàn tất có tham số khớp và JSONL tồn tại; không tự coi lượt lỗi quản trị là lỗi ứng dụng. Giữ bản `suite.interrupted.json` của lần bị gián đoạn. Lượt đo bị generator overflow không được tự bỏ qua để chọn kết quả đẹp.

## Giới hạn phải giữ trong báo cáo

Cùng availability zone, chưa có anti-affinity xác minh hypervisor; VM không đồng cấu hình CPU/RAM; chạy chung k3s; client qua VPN. TCP health chỉ kiểm tra listener đang nhận kết nối, không kiểm chứng ứng dụng đúng graph. Không có bằng chứng failover hạ tầng OVN khi một hypervisor/router cloud chết. Chưa phải nghiệm thu TLS/mTLS, multi-region, persistence nghiệp vụ hay AIOps tự động.

<a id="ke-hoach-tiep-theo"></a>

## Kế hoạch đợt đánh giá tiếp theo

Đợt tiếp theo **triển khai workload lên k3s rồi đánh giá ngay trên cụm đó**, giữ HPA và RL tắt khi đo baseline. Các số liệu Compose cũ phía trên là lịch sử, không gộp vào đối chứng mới. Sau baseline mới đánh giá HPA, sau đó RL với policy theo quy tắc và train/evaluation tách biệt.

### Tên gọi và giai đoạn chuẩn bị k3s

| Tên dùng trong sơ đồ/kế hoạch | Vai trò | Định danh trong code hiện tại |
|---|---|---|
| Service điều phối | Nhận request từ Envoy, gọi và tổng hợp các service xử lý | `service-a` |
| Service xử lý 1 | Nhóm backend mô phỏng thứ nhất | `service-b` |
| Service xử lý 2 | Nhóm backend mô phỏng thứ hai | `service-c` |

Mỗi service có nhiều pod/replica, không đồng nhất service với một server hay một VM. Hai service xử lý là hai đích gọi riêng, không có vai trò primary/backup. Tên theo vai trò dùng cho tài liệu; chưa đổi service ID trong code hoặc dữ liệu lịch sử. Trace mới phải giữ cả tên service và Pod UID để phân biệt pod cũ với pod thay thế.

**G0 — triển khai và nghiệm thu trên k3s trước khi đo chính:**

- Kiểm tra cluster, network, storage và tài nguyên thực có; ghi phiên bản k3s, các node và placement. Triển khai vào namespace riêng, dùng image cố định và khai báo requests/limits. HPA tắt, replica ban đầu cố định. Không coi ba node là bằng chứng control plane Kubernetes có HA.
- Lầu phụ trách workload điều phối/xử lý và Envoy: manifest, cấu hình, định danh/địa chỉ pod động, startup/readiness và giới hạn tài nguyên. Runtime hiện yêu cầu instance có sẵn trong topology nên phải tách cấu hình service khỏi danh sách pod trước khi scale tự do.
- Đạt phụ trách Consul/xDS: registry, health, lưu trữ cần thiết, discovery, bootstrap và drain/shutdown. Giữ luồng lookaside: gRPC client nhận địa chỉ từng pod xử lý từ xDS, không chỉ một ClusterIP chung che mất lựa chọn backend. Envoy cũng cần nhận danh sách pod điều phối; có thể dùng headless Service để resolve endpoint.
- Hai người thống nhất hợp đồng readiness/drain: pod chỉ được công bố khi sẵn sàng; pod dừng phải ngừng nhận RPC mới và có thời gian hoàn tất RPC đang chạy. Xác nhận bằng trace trên đường gọi thực, không chỉ Pod Ready hoặc ACK xDS.
- Với static baseline, chụp danh sách pod/IP tại đầu lượt và làm nóng channel trước đo; không tự cập nhật danh sách giữa lượt membership. Nếu pod bị thay thế trong bài tải ổn định, ghi sự kiện và chạy lại cả cặp bị nhiễu. Với xDS, xác nhận cập nhật endpoint mà caller không restart.
- Chọn và cố định vị trí load generator cho cả hai mode; nếu chạy trong cụm thì tách tài nguyên phát tải khỏi tài nguyên đo. Chuẩn bị thu log/metrics từng pod và sự kiện Kubernetes. CPU không mặc nhiên là metric autoscale phù hợp vì workload hiện mô phỏng bằng chờ bất đồng bộ.
- Nghiệm thu G0: graph gọi đúng; đủ pod nhận request; service xử lý scale 2→4→2 khi các pod điều phối vẫn chạy; pod mới/thay thế được nhận đúng; drain và deadline đúng; hai xDS replica phục vụ được caller mới khi mất một replica. Lưu kết quả thực, rồi mới bắt đầu E1–E8.

Đây là đầu việc chuẩn bị, chưa phải trạng thái đã triển khai. Lượt cập nhật tài liệu này không tạo workload hoặc thay đổi cụm k3s.

### Câu hỏi cần trả lời

1. Envoy phân phối request vào service điều phối và các caller service điều phối phân phối RPC xuống các service xử lý có đúng policy không?
2. Với tải và tài nguyên tương đương, static và xDS khác nhau thế nào về goodput, latency và lỗi?
3. Lookaside có cập nhật được membership khi caller vẫn chạy, và mất bao lâu để thay đổi thể hiện trên RPC thực?
4. Hệ thống phục hồi thế nào khi lỗi backend, ingress hoặc discovery? Trường hợp nào vẫn xử lý được nhưng dùng cấu hình cũ?

Không đặt giả thuyết xDS Round Robin tự tránh backend chậm nhưng vẫn healthy: baseline này chưa có policy thích nghi theo latency. Kịch bản backend không đồng đều nhằm đo giới hạn hiện tại và tạo cơ sở chọn bài toán cho RL.

### Thiết lập và cách chạy

- Giữ cùng image, placement, CPU/RAM, concurrency, deadline 3 giây, workload, health checking và đường truy cập. Consul/control plane xDS vẫn chạy khi đo static để giữ tải nền tương đương. Chỉ đổi discovery mode trên các caller; không thay nhiều yếu tố trong một phép đối chứng.
- Topology gốc: 2 pod điều phối, 2 pod xử lý của mỗi service; đo light và heavy riêng. Trong bài thay đổi service xử lý 1, giữ service xử lý 2 đủ năng lực xử lý và kiểm tra service điều phối/Envoy chưa thành nút thắt trước khi quy kết kết quả cho service xử lý 1.
- Khảo sát tải bằng bước tăng RPS để tìm mức cao nhất baseline còn đáp ứng tiêu chí tải ổn định: lỗi ≤1%, goodput offered-window ≥98% RPS đặt, P99 OK <3 giây, không generator drop. Gọi mức này là R_ref cho từng workload/topology; đây là mốc đo được, không phải công suất lý thuyết.
- Chốt các mức 30%, 60%, 90% và 110% R_ref trước lượt đối chứng. Dùng cùng RPS tuyệt đối cho static và xDS, không hiệu chỉnh tải riêng theo kết quả từng phương án. Nếu generator không đủ năng lực phát tải, sửa môi trường đo rồi chạy lại.
- Mỗi lượt dự kiến warmup theo thời gian 60 giây, đo 180 giây; sáu lần lặp mỗi phương án, ghép cặp và đảo thứ tự static→xDS / xDS→static. Ghi đầy đủ từng lượt, median và khoảng biến thiên; không chỉ chọn lượt đẹp nhất.
- Phát tải theo lịch độc lập với thời điểm hoàn tất RPC. Ghi offered RPS, thực tế dispatch, schedule lag, generator drop và request đang chờ; không dùng closed-loop để che backlog khi hệ thống chậm.
- Ghi phiên bản code/image, tham số, topology, placement và timestamp sự kiện vào thư mục kết quả mới. Dùng monotonic clock cho duration; đồng bộ clock giữa máy hoặc ghi rõ độ lệch khi ghép timeline.

### Ma trận kịch bản

| ID | Thiết lập và sự kiện trong 180 giây đo | Phép so sánh và kết quả cần lưu |
|---|---|---|
| E1 — tải ổn định | Mỗi mức 30/60/90/110% R_ref là một lượt riêng, backend đồng đều | Static và xDS: request/instance, goodput, p50/p95/p99, lỗi và backlog; mức 110% dùng để khảo sát quá tải |
| E2 — backend không đồng đều | Lượt thứ nhất tăng delay của một pod xử lý 1 lên 4 lần; lượt khác giảm concurrency một pod xử lý 1 còn 1/4, tối thiểu 1; giữ các yếu tố khác cố định | Đo latency và lỗi theo backend, ảnh hưởng lên service điều phối/toàn tuyến; không đồng nhất delay mô phỏng với hiệu năng CPU thực |
| E3 — burst | 0–60 giây: 60% R_ref; 60–120 giây: 110%; 120–180 giây: trở lại 60% | Độ tăng latency/lỗi và thời gian trở lại dải trước burst; ghi chưa phục hồi nếu hết lượt vẫn còn backlog |
| E4 — membership | Service xử lý 1 có 2 pod; tăng desired replicas lên 4 ở giây 60, giảm về 2 ở giây 120; các pod điều phối giữ nguyên UID | Đo từ scale/đăng ký/health đến RPC đầu chọn pod mới, và từ scale-in đến lần chọn cuối pod bị rút. Static giữ danh sách đầu lượt để kiểm tra giới hạn khám phá tự động; không dùng khác biệt số backend để kết luận xDS có thuật toán nhanh hơn |
| E5 — nhiều caller | Các lượt riêng với 1, 2, 4 pod điều phối; giữ hai service xử lý, tổng offered RPS và workload không đổi | Đo theo từng caller và gộp toàn hệ thống; bảo đảm Envoy biết đủ pod điều phối. Ghi requests/limits, node placement và tổng tài nguyên; tăng pod không đồng nghĩa tăng máy hoặc HPA |
| E6 — backend lỗi | Xóa một pod của service xử lý 1 ở giây 60; giữ desired replicas để k3s tạo pod thay thế | Đo chuyển tải sang pod còn sống, lỗi và thời gian pod thay thế Ready/nhận RPC. Ghi Pod UID cũ/mới và thời điểm tái tạo; tải không quá 60% năng lực đã đo của tập backend còn sống. Tách graceful termination khỏi thử crash/mất mạng |
| E7 — discovery lỗi | Các lượt riêng: giữ mất một replica xDS; mất toàn bộ xDS; mất Consul leader; mất quorum lâu hơn lease. Bắt đầu giây 60, khôi phục giây 120 | Runner phải duy trì sự cố dự kiến dù controller Kubernetes tự phục hồi; ghi replica/voter và thời điểm thực tế. Thử caller cũ/mới và cập nhật endpoint; traffic theo cache cũ không chứng minh discovery vẫn hoạt động |
| E8 — ingress lỗi | Buộc kết nối ban đầu qua một Envoy pod, gây lỗi pod đó ở giây 60 và theo dõi phục hồi/replacement | Giữ nguyên địa chỉ đầu vào HA; kiểm tra kết nối cũ/mới. Đo riêng chuyển sang Envoy còn sống và thời gian khôi phục số replica; không dùng việc pod mới xuất hiện làm bằng chứng không gián đoạn |

Trong E6/E8, Kubernetes có thể tạo pod thay thế trước giây 120. Đo theo mốc thực tế, không coi pod bị xóa là vẫn dừng tới mốc restore của runner Compose cũ. Chỉ kết luận failover nhờ replica còn sống nếu trace xác nhận request thành công trong khoảng pod thay thế chưa phục vụ; nếu khoảng này quá ngắn thì ghi giới hạn bằng chứng. Bài cố ý giữ mất backend lâu phải là biến thể riêng, ghi cách duy trì lỗi và phục hồi.

Trong E4, ghi rõ tổng tài nguyên thay đổi hay chỉ chia lại tài nguyên hiện có; không gọi lợi ích do thêm tài nguyên là lợi ích riêng của policy. Static nếu được cập nhật thủ công phải chạy thành lượt riêng và tính cả bước thao tác/cập nhật caller. Thay đổi membership là thao tác có kiểm soát, chưa phải HPA.

E7 khi mất quorum nhưng control plane xDS còn sống: endpoint phải bị rút sau freshness lease và nhịp xử lý; UNAVAILABLE lúc đó là hành vi fail-closed được mong đợi. Khi cả hai control plane xDS mất, lease phía control plane xDS không làm cấu hình đã cache ở caller tự hết hạn. Phải báo hai trường hợp này riêng. Những thay đổi endpoint phục vụ kiểm chứng discovery phải có timestamp và được ghi là một phần kịch bản E7.

### Chỉ số, tiêu chí và đầu ra

- **Phân phối:** đếm request vào từng service điều phối, RPC tới từng các service xử lý, phân nhóm theo caller/service/backend. Với backend đồng đều, so sánh tỷ lệ quan sát với phần chia kỳ vọng 1/N theo endpoint hợp lệ. Tách số được chọn, thành công và lỗi; response thành công không đại diện toàn bộ lần chọn backend.
- **Hiệu năng:** offered/dispatch RPS, goodput, p50/p95/p99 toàn tuyến và từng dependency, lỗi/timeout, request đang xử lý. Báo latency trên OK và trên toàn bộ RPC riêng; giữ đơn vị và cửa sổ đo thống nhất.
- **Cập nhật:** ghi mốc sự kiện được xác nhận, snapshot publish/ACK, RPC đầu tới endpoint mới và RPC cuối còn chọn endpoint đã rút. ACK không thay thế bằng chứng phân phối thực. Phân biệt RPC đã bắt đầu trước drain với RPC mới chọn endpoint sau drain.
- **Ổn định/phục hồi:** dựng timeline theo cửa sổ 1 giây; báo số lỗi trong giai đoạn sự cố và toàn lượt, thời gian tới cửa sổ 5 giây toàn OK. Với burst, so thêm goodput/latency với dải trước burst; cửa sổ không lỗi riêng lẻ chưa chứng minh hết backlog.
- **Tiêu chí:** dùng ngưỡng tải ổn định nêu trên cho các mức dưới bão hòa. Không áp ngưỡng không lỗi cho quá tải có chủ đích hoặc fail-closed. Với backend/ingress lỗi, mục tiêu đồ án là đạt cửa sổ ổn định trong 30 giây sau xác nhận lỗi, không cần can thiệp khôi phục thủ công; ghi riêng đóng góp của replica còn sống và pod được k3s tái tạo. Với bài duy trì sự cố có chủ đích, đo ổn định trước mốc restore. Đây là ngưỡng thử nghiệm, không phải SLA. Membership và discovery phải đáp ứng đúng hành vi trong ma trận và có thời gian cập nhật được đo, chưa đặt cam kết thời gian khi chưa có dữ liệu.
- **Bàn giao:** trace từng RPC, nhật ký sự kiện, cấu hình/môi trường, bảng kết quả từng lượt và biểu đồ theo thời gian. Kết luận theo từng kịch bản: cải thiện, tương đương, suy giảm hoặc chưa đủ bằng chứng; nêu cả chi phí và giới hạn của lookaside.

<a id="danh-gia-toi-uu"></a>

### Kết luận chất lượng hệ thống và tối ưu khi chưa đạt

Mỗi kịch bản phải có kết luận về chất lượng hệ thống, không dừng ở bảng số đo. Kết luận áp dụng cho workload, tải, topology và giới hạn tài nguyên đã thử; không dùng một điểm số tổng để che các tiêu chí chưa đạt.

**Đánh giá trên hai trục độc lập:**

| Trục | Kết luận phải ghi | Căn cứ |
|---|---|---|
| Đáp ứng yêu cầu | Đạt / chưa đạt / chưa đủ bằng chứng | Ngưỡng hoặc hành vi mong đợi đã chốt trước lượt đo chính; kết quả từng lượt và mức biến thiên |
| So với đối chứng | Cải thiện / tương đương / suy giảm / chưa đủ bằng chứng | Chênh lệch tuyệt đối và tương đối theo từng chỉ số trong các lượt ghép cặp, cùng điều kiện |

Đánh giá riêng phân phối tải, hiệu năng, cập nhật endpoint, phục hồi và độ ổn định. Với backend đồng đều, kiểm tra phân phối đúng Round Robin; với backend khác năng lực, đo nghẽn/latency/lỗi dù policy vẫn chia request đúng. Đúng policy chưa đủ kết luận chất lượng phục vụ tốt. Với các chỉ số chưa có ngưỡng (như sai lệch phân phối, độ biến thiên hoặc thời gian cập nhật), hai người phải chốt ngưỡng và cách tổng hợp sau pilot, trước bộ đo chính; nếu chưa chốt thì chỉ báo số đo, chưa đánh dấu đạt.

Nếu các chỉ số đổi theo hướng trái nhau, ghi rõ đánh đổi, ví dụ goodput tăng nhưng p99 hoặc lỗi tăng; không kết luận tốt hơn toàn diện. Không dùng median để bỏ qua các lượt vi phạm ngưỡng. Mức chênh lệch nằm trong nhiễu hoặc dữ liệu thiếu phải ghi chưa đủ bằng chứng; thiếu khác biệt rõ ràng chưa chứng minh hai phương án tương đương.

**Vòng xử lý bắt buộc khi chưa đạt:**

1. **Xác minh phép đo:** loại trừ lỗi phát tải, trace thiếu, sai clock, nhiễu nền và so sánh khác cấu hình. Giữ nguyên dữ liệu lỗi, ghi nguyên nhân rồi chạy lại phép đo hợp lệ.
2. **Tái lập và xác định nút thắt:** đối chiếu timeline, latency từng hop, lỗi, backlog và tài nguyên sử dụng. Phân biệt nghẽn Envoy/service điều phối và hai service xử lý với cập nhật health, publish xDS, áp dụng cấu hình hoặc reconnect chậm. Chỉ bổ sung đo/profile cần thiết để kiểm chứng nguyên nhân.
3. **Đề xuất phương án có giả thuyết:** ghi thay đổi, chỉ số kỳ vọng cải thiện và rủi ro. Ưu tiên sửa lỗi và chi phí dư thừa; thử cấu hình concurrency/admission, tái sử dụng channel, xử lý queue/cancellation hoặc nhịp probe/publish khi có bằng chứng liên quan. Thử từng yếu tố hoặc ghi rõ tổ hợp được chủ ý đánh giá.
4. **Đo trước–sau:** lưu baseline gốc, phiên bản candidate và cấu hình; chạy lại cùng workload, RPS, tài nguyên, deadline và protocol lặp. Không hạ tải mục tiêu, tăng deadline hay bỏ health để biến bài chưa đạt thành đạt. Nếu cần tăng tài nguyên, báo thành phương án mở rộng tài nguyên riêng.
5. **Kiểm tra đánh đổi:** đối chiếu goodput, tail latency, lỗi, phân phối tải và phục hồi. Điều chỉnh probe nhanh hơn phải kiểm tra tải control plane xDS/registry và endpoint bị loại nhầm; tăng concurrency phải kiểm tra quá tải và timeout. Bỏ hoặc sửa candidate gây suy giảm ngoài giới hạn đã chốt.
6. **Lặp và chọn phương án:** tiếp tục các giả thuyết tối ưu có cơ sở; giữ phương án tốt nhất đã tái lập, không dừng ở việc báo chưa đạt. Chạy lại các kịch bản chịu ảnh hưởng sau mỗi candidate; với phiên bản cuối, xác nhận lại toàn bộ E1–E8 theo protocol và kiểm tra chéo trước khi chốt.

"Tối ưu nhất có thể" ở đây là tìm phương án tốt nhất đã kiểm chứng trong ràng buộc tài nguyên và chức năng đã chốt. Khi các mục tiêu đạt và các phương án hợp lý đã thử không còn cải thiện vượt nhiễu hoặc gây đánh đổi không chấp nhận được, có thể chốt kèm bằng chứng. Nếu vẫn chưa đạt do giới hạn tài nguyên/thiết kế, phải ghi rõ giới hạn đo được, những cách đã thử và thay đổi cần thiết tiếp theo; trạng thái mục tiêu vẫn là chưa đạt, không đổi ngưỡng để báo hoàn thành.

Trong đợt này RL vẫn tắt. Nếu kết quả cho thấy cần policy thích nghi, ghi thành vấn đề cho giai đoạn RL; thử một policy mới phải là nhánh đối chứng riêng, giữ lại static/xDS Round Robin gốc để tránh đánh đồng tối ưu discovery với đổi thuật toán LB.

**Báo cáo cuối phải có:** kịch bản và điều kiện; mục tiêu; số đo trước/sau từng lượt; chênh lệch; kết luận hai trục; nguyên nhân; thay đổi được giữ hoặc loại; ảnh hưởng tới các kịch bản khác và giới hạn còn lại. Nhận định hệ thống tốt/chưa tốt phải gắn với các bằng chứng này.

### Công cụ hiện có và phần cần bổ sung trước khi chạy

`controlled_suite.py` đã hỗ trợ so sánh static/xDS, mức tải cố định, workload light/heavy và đảo thứ tự với số lần lặp chẵn. `controlled_probe.py` có trace và kiểm tra lịch phát tải; `fault_probe.py` có kịch bản backend/VM1 với timeline. Các runner nghiệm thu lookaside/HA đã kiểm tra membership và discovery, nhưng chưa phải bộ benchmark lặp đầy đủ theo ma trận mới.

Cần bổ sung runner cho warmup 60 giây theo thời gian, burst nhiều pha, thay đổi topology/caller, sự kiện control plane xDS/registry/Envoy theo lịch và thu số liệu từng hop/caller/backend kể cả lỗi. Các bài fault hiện có dùng thời lượng cố định khác kế hoạch này, nên không thể coi đổi tham số suite là đã hỗ trợ toàn bộ ma trận. Giữ riêng thời gian xử lý server và thời gian RPC từ caller; collector RL hiện tại chưa thay thế được telemetry đánh giá toàn tuyến.

Chỉ chạy bộ đo chính sau khi các phép đo bổ sung được kiểm tra bằng một lượt smoke có đối chiếu số request và timeline sự kiện. Phần này là kế hoạch công việc tiếp theo, không ghi nhận các runner mới hoặc kết quả E1–E8 đã hoàn thành.

<a id="phan-cong"></a>

## Phân công Lầu và Đạt

Phân công này áp dụng cho đợt đánh giá E1–E8 tiếp theo, không thay đổi lịch sử phân công M1. Cả hai cùng làm đối chứng static/xDS; RL chưa bật. Mỗi người phụ trách từ chuẩn bị công cụ đến chạy, phân tích và giải thích kết quả của nhóm kịch bản mình nhận.

| Người | Kịch bản chính | Người kiểm tra chéo |
|---|---|---|
| Lầu | E1 tải ổn định; E2 backend không đồng đều; E3 burst; E5 nhiều caller | Đạt |
| Đạt | E4 membership; E6 backend lỗi; E7 discovery lỗi; E8 ingress lỗi | Lầu |

### Lầu — hiệu năng và phân phối tải

1. Hoàn thiện bộ phát tải và telemetry dùng chung: warmup theo thời gian, tải cố định/burst, lịch phát độc lập và dữ liệu từng RPC. Phụ trách thay đổi trong `scripts/controlled_probe.py`, `scripts/controlled_suite.py` và `scripts/report_controlled.py`; thống nhất giao diện sự kiện với Đạt trước khi sửa.
2. Khảo sát R_ref theo workload/topology, chốt các mức RPS cho đối chứng. Đo thêm năng lực của tập backend còn sống để Đạt chọn tải cho E6, tránh nhầm quá tải với lỗi failover.
3. Chuẩn bị cấu hình backend chậm/giới hạn concurrency và 1/2/4 pod điều phối; chạy E1/E2/E3/E5 theo protocol. Khi thay số pod điều phối, cập nhật cả danh sách upstream Envoy và kiểm tra tất cả pod điều phối dự kiến đều nhận request.
4. Phân tích request theo caller/backend, goodput, p50/p95/p99, lỗi và backlog. Chỉ rõ nút thắt nằm ở ingress, service điều phối hay các service xử lý; phân biệt thời gian server xử lý với thời gian RPC/toàn tuyến.
5. Bàn giao cấu hình tái lập, trace/metadata, bảng từng lượt và đồ thị tải–goodput, tải–latency, phân phối request và timeline burst; viết kết luận cho E1/E2/E3/E5.
6. Chịu trách nhiệm tìm nguyên nhân và tối ưu các mục hiệu năng/phân phối tải chưa đạt; bàn giao phiên bản trước–sau, các phương án đã thử và xác nhận của Đạt về ảnh hưởng lên cập nhật/phục hồi.

### Đạt — cập nhật endpoint và phục hồi

1. Hoàn thiện runner sự kiện và khôi phục: thêm/drain/rút backend, dừng/khởi động service, control plane xDS, Consul và Envoy. Phụ trách `scripts/fault_probe.py`, `scripts/report_faults.py` và runner sự kiện bổ sung; tham khảo các bài `acceptance_lookaside*.py` hiện có.
2. Chuẩn bị cấu hình scale service xử lý 1 lên bốn pod, registration, drain và cấu hình ingress HA; kiểm tra service điều phối không restart trong E4. Tách rõ thay đổi số container với thay đổi tài nguyên máy.
3. Chạy E4/E6/E7/E8 bằng bộ phát tải chung. Ghi riêng thời điểm phát lệnh, xác nhận sự kiện và khôi phục; kiểm tra caller cũ/mới và thay đổi endpoint thực sự khi thử discovery.
4. Đo thời gian endpoint mới bắt đầu nhận RPC, endpoint bị rút ngừng được chọn, lỗi và thời gian ổn định. Ghi rõ replica còn sống hay pod thay thế phục vụ; với bài duy trì sự cố, đo trước restore. Phân biệt cache cũ, fail-closed và phục hồi thực sự.
5. Bàn giao runner/cấu hình, trace và nhật ký sự kiện, bảng thời gian cập nhật/phục hồi, biểu đồ timeline và kết luận E4/E6/E7/E8. Mỗi lượt phải có xác nhận hệ thống đã được khôi phục; nếu không, đánh dấu chưa hoàn tất.
6. Chịu trách nhiệm tìm nguyên nhân và tối ưu các mục cập nhật/phục hồi chưa đạt; bàn giao phiên bản trước–sau, các phương án đã thử và xác nhận của Lầu về ảnh hưởng lên hiệu năng/phân phối tải.

### Phối hợp và thứ tự thực hiện

1. **Chốt protocol cùng nhau:** một phiên bản code/image, cấu hình nền, quy ước run ID và schema dữ liệu. Mỗi lượt ghi scenario, mode, workload, RPS, lần lặp, topology/placement, thời gian, trạng thái RPC và định danh caller/backend khi quan sát được. Endpoint không xác định trên RPC lỗi phải ghi thiếu/unknown, không suy đoán từ request thành công.
2. **Làm công cụ song song:** Lầu phụ trách phát tải/telemetry/báo cáo hiệu năng; Đạt phụ trách sự kiện/khôi phục/báo cáo lỗi. Các thay đổi runtime để thu metric phải được cả hai kiểm tra và áp dụng giống nhau cho hai mode. Đạt tích hợp qua giao diện đã chốt, tránh hai người sửa độc lập cùng bộ phát tải.
3. **Kiểm tra chung trước bộ đo chính:** chạy một lượt smoke tải ổn định và một lượt dừng/phục hồi backend. Đối chiếu offered/dispatch/OK/error/drop, timeline và phân phối instance; sửa sai số đo trước khi chạy đủ lượt.
4. **Chạy theo nhóm đã nhận:** tuân thủ warmup 60 giây, đo 180 giây, sáu lần lặp/phương án và thứ tự đối chứng đã chốt. Nếu dùng cùng cụm ba VM, đặt lịch riêng: không chạy benchmark và thử lỗi đồng thời. Chỉ đo song song khi có hai môi trường độc lập và ghi rõ khác biệt tài nguyên.
5. **Kiểm tra chéo và ghép báo cáo:** Đạt tái lập một cặp static/xDS của E1 do Lầu chuẩn bị; Lầu tái lập một cặp E6 do Đạt chuẩn bị. Mỗi người viết kết luận nhóm mình; cả hai thống nhất kết luận chung và lựa chọn vấn đề cần thử RL.

### Điều kiện bàn giao

- Đủ các lượt dự kiến cho kịch bản được giao; lượt lỗi công cụ hoặc chưa phục hồi vẫn được lưu và giải thích, không loại để chọn số đẹp.
- Trace, metadata, cấu hình và nhật ký sự kiện đủ để người kia tái lập; không chỉ bàn giao ảnh biểu đồ hoặc số trung bình.
- Báo cáo dùng đúng tiêu chí của protocol, tách quá tải/fail-closed khỏi lỗi ngoài dự kiến và chỉ ra trường hợp lookaside tốt hơn, tương đương hoặc kém hơn.
- Mỗi mục chưa đạt có nguyên nhân được kiểm chứng và vòng tối ưu/đo lại; lưu cả phương án không hiệu quả. Bàn giao báo cáo không đồng nghĩa mục tiêu chất lượng đã đạt: các giới hạn chưa giải quyết vẫn phải được giữ trong danh sách công việc còn lại.
- Kết quả được kiểm tra chéo; môi trường trở lại cấu hình nền trước khi người tiếp theo chạy. Chỉ đánh dấu hoàn thành sau các bước này, không dựa vào việc đã viết xong runner.
