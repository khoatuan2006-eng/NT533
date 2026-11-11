# Thử nghiệm RL cho lookaside

## Vị trí trong đồ án

RL thuộc **nhánh lỗi mềm** của câu hỏi chính (xem [README](../../README.md#câu-hỏi-của-đồ-án)): một backend vẫn trả lời nhưng chậm, health probe không rút nó ra, nên việc né nó phụ thuộc vào chiến lược cân bằng tải. RL là một chiến lược như vậy: đọc latency theo endpoint và đổi trọng số mà control plane phát qua xDS.

Phần này cần trả lời hai điều:

1. **Có né được backend chậm không**, so với Round Robin và một quy tắc đơn giản, trên cùng đường xDS và trong cùng lượt chạy.
2. **Có an toàn khi chính nó lỗi không**: telemetry cũ, thiếu mẫu hoặc lỗi vượt 10% thì phải quay về Round Robin.

Tiêu chí an toàn và kết luận:

- RL không nằm trên đường xử lý request; khi telemetry cũ, sai hoặc thiếu mẫu thì quay về Round Robin.
- Trọng số mỗi endpoint giới hạn 30–70%; không chạy exploration trên cấu hình mặc định.
- Chỉ kết luận RL tốt hơn khi vượt policy theo quy tắc trên nhiều seed. Kết quả hiện tại chưa cho thấy điều đó.

Giới hạn khi so sánh: RL chạy trong lab cục bộ với một caller và hai backend, còn Least Request đo trên cụm ba VM với caller static. Hai bên khác đường dữ liệu và khác môi trường, nên không xếp hạng RL với Least Request bằng số tuyệt đối; chỉ so mỗi bên với Round Robin cùng đường ([quy tắc so sánh chiến lược](../2-cong-viec/3-tra-cuu.md#quy-tắc-so-sánh-chiến-lược)). Nếu RL không hơn quy tắc đơn giản thì báo cáo ghi đúng như vậy.

Lệnh dùng cho báo cáo là lượt nhiều seed trong [tham chiếu thí nghiệm](../2-cong-viec/3-tra-cuu.md#rl-nhiều-seed--compare_rl_scenariospy). Phần còn lại của tài liệu này là nhật ký các lượt thử trước.

Agent Q-learning trong `src/grpc_lb/rl.py` có hai đường thử nghiệm. `scripts/train_lookaside_rl.py` chạy mô phỏng hai backend; state mô phỏng chứa danh tính backend chậm và mức tải lý tưởng, không đại diện cho telemetry thật. Model JSON của script này không được control plane đọc.

`scripts/experiment_rl_lookaside.py` chạy vòng Q-learning trực tiếp trong lab localhost: RPC qua gRPC xDS đến hai service, đo latency theo instance, đưa telemetry vào `SnapshotPublisher` và `RLController` thực, phát trọng số, đo reward và cập nhật Q. Đây là học online có exploration, chỉ dành cho lab; ADS và service được script tạo và đóng riêng. Độ trễ 1 giây sau publish là giả định, không xác nhận ACK và không đảm bảo mọi RPC đã nhận policy mới. Mẫu nhỏ và tải tuần tự không chứng minh RL cải thiện hiệu năng.

```powershell
.\.venv\Scripts\python.exe scripts/probe_weighted_xds.py
.\.venv\Scripts\python.exe scripts/train_lookaside_rl.py --episodes 1000 --seed 42 --output results/toy-rl-policy.json
.\.venv\Scripts\python.exe scripts/experiment_rl_lookaside.py --windows 20 --requests 50 --output results/rl-lookaside-local.json
.\.venv\Scripts\python.exe scripts/experiment_rl_lookaside.py --baseline --windows 20 --requests 50 --output results/rl-baseline-local.json
.\.venv\Scripts\python.exe scripts/compare_lookaside_rl.py --train-windows 6 --eval-windows 6 --requests 40 --seeds 31 32 --output-dir results/rl-comparison
```

Bộ so sánh chạy tuần tự, đổi thứ tự RL/Round Robin theo seed; RL ngừng exploration và cập nhật Q trong các cửa sổ evaluation. Dữ liệu từng request thành công được giữ trong file từng lượt, lỗi được đếm riêng. Cùng số cửa sổ warmup/train, số request và điều kiện workload; tải tuần tự không đại diện tải mở hay HA. Đây là đối chứng cục bộ để quan sát kết quả, không phải bằng chứng ưu việt tổng quát.

## Đánh giá lại 01/11/2026

### Hội tụ và các trường hợp tải — lượt bổ sung

`scripts/audit_rl_convergence.py` train Q-learning trong mô phỏng tĩnh, state gồm ba nhóm tương quan latency và 5 mức trọng số 30–70%. Reward thêm phạt lệch khi hai backend tương đương. Ba seed 7/19/31, 300.000 cập nhật/seed: policy trên 15 state giống nhau ở cả ba seed và không đổi giữa các mốc 50.000 đến 300.000; thay đổi Q lớn nhất trong 50.000 cập nhật cuối tương ứng 0,0163/0,0183/0,0176. Đây là hội tụ policy quan sát được trong mô hình tổng hợp tĩnh, không phải chứng minh hội tụ toán học hoặc hội tụ trong hệ thống thực. Dữ liệu: `results/rl-convergence-balanced-20261101/convergence.json`.

Trên gRPC, telemetry caller trước đây bị nhiễu và policy dao động hoặc không thích nghi trong thử nghiệm đổi backend chậm. Controller hiện đòi hai cửa sổ cùng nhóm trước khi đổi trọng số; collector ưu tiên thời gian server từ trailer. Khi đảo instance chậm giữa lượt 10 cửa sổ, 80 RPC/cửa sổ, trọng số (theo thứ tự địa chỉ) đổi 50/50 → 60/40 → 70/30, sau đảo trở về 60/40 → 50/50 → 40/60. Dữ liệu: `results/rl-scenarios-hysteresis-20261101/swap-server-telemetry.json`. Đây chỉ là một lượt, không chứng minh SLA hội tụ sau sự cố.

Đối chứng gRPC thật mới nhất `results/rl-scenarios-server-metric-20261101/report.json`: một lượt mỗi phương án, 8 cửa sổ, hai cửa sổ đầu warmup, 40 RPC/cửa sổ, đồng thời 8. Trung bình/P95 theo từng lượt trên 240 RPC đánh giá, ms:

| Kịch bản | Round Robin | RL | Quy tắc |
|---|---|---|---|
| Hai backend bằng nhau | 31,56 / 63 | 33,26 / 47 | 30,10 / 47 |
| Một backend chậm | 34,71 / 62 | 33,58 / 62 | 30,42 / 47 |
| Đổi backend chậm giữa lượt | 33,10 / 47 | 33,83 / 62 | 33,40 / 47 |
| Giới hạn xử lý mỗi backend còn 2 | 44,60 / 94 | 33,20 / 47 | 32,67 / 47 |

Không có lỗi trong các cửa sổ đánh giá lượt này, không có fallback. Chỉ giảm concurrency backend không chứng minh đã bão hòa: lượt này không quan sát được rejection. Trong thử nghiệm trước dùng telemetry caller, `results/rl-scenarios-20261101/report.json`, cùng kịch bản giới hạn backend có 1/4/6 lỗi tương ứng RR/RL/rule. Các lượt không lặp, không ngẫu nhiên thứ tự, timer Windows thô và thời gian có thể nhiễu; không đủ bằng chứng RL tốt hơn rule hay ổn định trên mọi mức tải. Khi hai backend bằng nhau, cả ba giữ 50/50 trong lượt mới. Trong lượt đổi backend, RL chỉ về 50/50 trước khi kết thúc 8 cửa sổ; lượt dài hơn mới thấy đảo tỷ lệ. Controller không kiểm soát được độ trễ lan truyền policy hay ACK và dùng telemetry của một caller, nên không tuyên bố ổn định trên production hoặc lúc nhiều caller/HA.

```powershell
.\.venv\Scripts\python.exe scripts/audit_rl_convergence.py --updates 300000 --checkpoint 50000 --output-dir results/rl-convergence-balanced-20261101
.\.venv\Scripts\python.exe scripts/compare_rl_scenarios.py --model results/rl-convergence-balanced-20261101/model-7.json --output-dir results/rl-scenarios-server-metric-20261101 --windows 8 --requests 40 --concurrency 8
```

Reward cũ chỉ phạt latency vượt 50 ms, nên không phân biệt phần lớn cửa sổ tốt/xấu. Bổ sung thành phần latency trung bình và giảm phạt đổi trọng số; giới hạn trọng số mỗi endpoint 30–70% để giữ mẫu quan sát. `train_observed_rl.py` huấn luyện Q-table trên mô phỏng có hai mức độ trễ và trạng thái phân nhóm tỷ lệ latency; kịch bản mô phỏng chọn nhóm dựa trên mức trễ giả định, chưa mô phỏng đầy đủ lỗi đo, queue hay mạng. Dùng model `results/rl-observed-model.json` đã đóng băng qua `EXPERIMENTAL_RL_MODEL`. Không xem policy này là đã chứng minh an toàn hoặc tổng quát.

Đối chứng gRPC xDS localhost, hai seed 41/42 lần lượt đảo instance chậm, độ trễ mô phỏng 2/30 ms, 4 cửa sổ warmup và 5 cửa sổ đánh giá, 40 RPC/cửa sổ. Agent đóng băng khi đánh giá; RR và RL chạy tuần tự theo thứ tự đảo. Có thêm chính sách quy tắc ưu tiên instance nhanh, chạy riêng sau cặp RL/RR; do chạy sau, điều kiện thời gian không được cân bằng như cặp chính. Số liệu trung bình/P95/P99, đơn vị ms:

| Seed | RL | Round Robin | Quy tắc |
|---|---|---|---|
| 41 | 23,75 / 47 / 63 | 35,16 / 63 / 125 | 24,38 / 47 / 62 |
| 42 | 27,73 / 62 / 63 | 29,14 / 47 / 62 | 24,22 / 47 / 62 |

Mỗi ô 200 request thành công, không lỗi; không có fallback trong các lượt hoàn thành. Lượt đầu trước khi giới hạn trọng số bị fallback do thiếu mẫu, được giữ tại `results/rl-observed-compare-20261101/`. Dữ liệu các lượt hoàn thành nằm tại `results/rl-observed-compare-v2-20261101/`. RL cải thiện trung bình trong cả hai lượt, nhưng P95 xấu hơn ở seed 42, còn quy tắc đạt tương đương hoặc tốt hơn RL. Các lượt ngắn trên Windows có độ phân giải timer thô và không đủ để kết luận có ý nghĩa thống kê hoặc khẳng định RL vượt quy tắc. Tình huống tối ưu là ưu tiên backend nhanh; đây không phải bằng chứng riêng cho ưu thế của RL. Cần phép thử dài và lặp trên Linux/đa máy với tải mở, thay đổi trạng thái, độ trễ xDS, nhiều caller, lỗi và đối chứng rule trước khi kết luận hữu ích.

```powershell
.\.venv\Scripts\python.exe scripts/train_observed_rl.py --episodes 12000 --seed 17 --output results/rl-observed-model.json
.\.venv\Scripts\python.exe scripts/compare_lookaside_rl.py --train-windows 4 --eval-windows 5 --requests 40 --seeds 41 42 --model results/rl-observed-model.json --output-dir results/rl-observed-compare
.\.venv\Scripts\python.exe scripts/experiment_rl_lookaside.py --rule --windows 9 --train-windows 4 --requests 40 --seed 41 --slow two --output results/rl-observed-compare/rule-41.json
.\.venv\Scripts\python.exe scripts/experiment_rl_lookaside.py --rule --windows 9 --train-windows 4 --requests 40 --seed 42 --slow one --output results/rl-observed-compare/rule-42.json
```

Kết quả localhost ngày 31/10/2026: hai seed 31/32, mỗi lượt 6 cửa sổ train/warmup rồi 6 cửa sổ đánh giá, 40 RPC/cửa sổ, hai backend delay mô phỏng 2 ms và 15 ms. RL seed 31/32 có latency trung bình 24,61/24,93 ms; Round Robin tương ứng 22,65/21,68 ms. P95 cả bốn lượt là 32 ms; P99 RL 47/47 ms, baseline 32/32 ms. Tất cả 960 RPC đánh giá thành công. Trong thử nghiệm ngắn này RL **không tốt hơn**; kết quả không chứng minh hiệu quả trên workload khác. Chi tiết và dữ liệu từng request: `results/rl-collector-comparison-20261031/report.json` và các file cùng thư mục. Windows có độ phân giải hẹn giờ thô nên nhiều latency rơi vào bội số gần 16 ms.

Stack Compose một host cũ có overlay `deploy/compose.lookaside-rl-lab.yaml` (đã xóa khỏi cây thư mục, còn trong lịch sử git; xem `docs/history/README.md`), thêm vào hai file microservices và lookaside. Overlay chỉ cho A-1 ghi telemetry của service-b vào volume chung với control plane; A-2 không ghi, do đó dữ liệu không đại diện toàn bộ caller. Docker chưa được xác minh trên máy hiện tại.

Probe khởi động hai service và ADS riêng trên localhost, phát hai locality 8:2 rồi 2:8 và đếm 200 RPC mỗi lượt. Nó không kiểm chứng HA, nhiều caller, membership thay đổi hay thời gian hội tụ; không dùng probe này làm bằng chứng production. Helper `weighted_snapshot` yêu cầu trọng số khớp chính xác membership đã khám phá, không tự thêm endpoint.

Control plane có thể bật thử nghiệm bằng `EXPERIMENTAL_RL_TELEMETRY` trỏ tới file JSON window và `EXPERIMENTAL_RL_SERVICE` là tên service. Cả hai phải được thiết lập cùng nhau. Một caller xDS có thể thu RPC thật bằng `EXPERIMENTAL_RL_TELEMETRY_OUTPUT` và cùng `EXPERIMENTAL_RL_SERVICE`; cần mount thư mục chung giữa caller và control plane và chỉ cho một caller ghi file. Collector dùng `instance_id` trong response và topology để ánh xạ địa chỉ; mỗi tên phải resolve duy nhất một IP. Từ 01/11, collector ưu tiên thời gian xử lý server trong trailer gRPC, nếu không có trailer hợp lệ dùng thời gian RPC từ caller; do đó hai loại đo có thể bị trộn và đây không phải end-to-end latency toàn hệ thống. Từ 11/11, service gắn trailer `x-instance-id` cả khi trả lỗi; collector ghi lỗi theo endpoint vào trường `endpoint_errors`, và controller tính latency hiệu dụng = median / tỷ lệ thành công. Lỗi không có trailer (mất kết nối, hết deadline phía caller) vẫn không quy được cho endpoint. Khi tổng lỗi vượt 10% controller vẫn quay về Round Robin. Window mặc định 10 giây. File cần `timestamp` Unix giây, `sequence` tăng dần, `service`, `offered`, `errors`, `endpoints` ánh xạ địa chỉ IP:port đã resolve sang danh sách latency ms. Cần ít nhất ba mẫu thành công cho mỗi endpoint; dữ liệu quá 15 giây, lỗi vượt 10%, membership không khớp hoặc không đủ hai endpoint khiến snapshot trở về Round Robin. Writer ghi file tạm rồi đổi tên nguyên tử. Q-table trong bộ nhớ mất khi restart; các replica control plane không đồng bộ. Chỉ bật trên một control plane và một caller lab, không bật trong HA hoặc production.

Trước khi dùng RL với traffic thực, cần thay state lý tưởng bằng telemetry có timestamp và số mẫu; thiết kế reward theo offered/goodput/timeout; train và đánh giá theo seed/workload tách biệt với Round Robin và policy theo ngưỡng; kiểm tra xDS ACK và thời điểm caller thực sự áp dụng; thêm policy controller có giới hạn, lease, xử lý membership, rollback và cơ chế phối hợp giữa các replica control plane. Tuyệt đối không chạy exploration trên deployment mặc định.

Nguồn tham khảo: [gRPC A27](https://github.com/grpc/proposal/blob/master/A27-xds-global-load-balancing.md) mô tả trọng số locality và việc bỏ qua trọng số endpoint; [LoadBalancerAutoScaler-DRL](https://github.com/MartinFarres/LoadBalancerAutoScaler-DRL) tham khảo cách phân tách môi trường, reward và đánh giá (HAProxy, không phải gRPC). Implementation Q-learning ở đây được viết riêng, không chép code của các nguồn đó.
