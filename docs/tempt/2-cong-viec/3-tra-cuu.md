# Tham chiếu thí nghiệm

Tài liệu này để **tra cứu, không đọc từ đầu đến cuối**: tình huống lỗi, phương án, tiêu chí đạt, tham số, vấn đề đã biết và sự cố thường gặp. Thứ tự làm việc từng bước nằm ở [2-huong-dan.md](2-huong-dan.md).

Mọi lệnh chạy trên máy Windows của bạn, từ thư mục gốc của repo, và điều khiển ba VM trên cloud UIT-IoT qua tùy chọn `--env configs/lab.cloud.json`.

**Trạng thái kiểm chứng (11/11/2026).** Hai bộ chạy đã chạy thật trên bản dựng một máy (đủ 10 tình huống lỗi, mỗi tình huống một lần, và một lượt tải ngắn). Phần điều khiển ba VM qua SSH đã qua kiểm thử logic nhưng **chưa chạy trên VM thật**. Các kết quả chạy thử trong `results/` chỉ để chứng minh công cụ hoạt động, không dùng cho báo cáo.

## Môi trường

| Node | VM (IP nội bộ) | Thành phần |
|---|---|---|
| node1 | 192.168.1.238 | consul-1, envoy-1, controlplane-1, a-1, b-1 |
| node2 | 192.168.1.172 | consul-2, envoy-2, controlplane-2, a-2, c-1, b-3 |
| node3 | 192.168.1.217 | consul-3, b-2, c-2 |

Bộ phát tải gửi request tới LB cloud (`192.168.120.109:80`), LB chia cho hai Envoy. SSH tới node2 và node3 đi qua node1. Mô tả môi trường nằm trong `configs/lab.cloud.json`, SSH trong `configs/ssh-config`; cả hai dùng chung cho nhóm, mỗi người chỉ cần đặt file khóa ở `~/.ssh/uitiot-lab.pem`. File mô tả ghi địa chỉ LB, file SSH, và với từng node là địa chỉ SSH, IP nội bộ, bảng tên service của lab → tên service Compose trên VM (ví dụ `envoy-1` → `ingress`).

Lệnh quản lý:

```powershell
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json check
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json status
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json up --variant xds
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json heal
```

Gây lỗi bằng tay:

```powershell
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json stop b-2
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json start b-2
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json stop-node node3
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json start-node node3
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json partition --a node3 --b node1 node2
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json degrade node3 --delay-ms 100
```

Cách mỗi thao tác được thực hiện trên VM:

| Thao tác | Trên VM |
|---|---|
| Dừng / chạy lại instance | `docker compose stop/start` trên VM chứa instance đó |
| Node chết | Dừng mọi container của lab trên VM đó (VM vẫn bật) |
| Phân mảnh | `iptables` chặn IP nội bộ của nhóm node bên kia |
| Độ trễ, rớt gói | `tc netem`, chỉ cho gói gửi tới các node khác của lab |
| Đổi phương án | Ghi `mode.json` cho service A, sửa chính sách trong `envoy.yaml`, tạo lại service liên quan |

An toàn:

- **Luật mạng tự hết hạn sau 15 phút**, nên mất phiên SSH giữa chừng cũng không cắt các VM khỏi nhau mãi.
- **Luật phân mảnh luôn đặt ở phía còn SSH tới được**: khi cô lập node3, luật nằm trên node1 và node2.
- **Độ trễ không ảnh hưởng SSH**, vì chỉ áp cho gói đi tới IP nội bộ của các node khác.
- **`down` bị chặn**, vì cụm dùng chung.
- **Khóa cụm:** mỗi lúc chỉ một người chạy. Hai bộ chạy tự giữ khóa trên node1; các lệnh gây lỗi bằng tay và lệnh triển khai bị từ chối khi người khác đang giữ. Xem bằng `lock-status`, gỡ khóa bị bỏ lại bằng `unlock` (hỏi người giữ trước).

## Phương án (`--variant`)

| Phương án | Caller A → B/C | Envoy → A |
|---|---|---|
| `xds` (mặc định) | xDS, Round Robin | Round Robin |
| `static` | Danh sách cố định có health Watch, Round Robin | Round Robin |
| `static-lr` | Danh sách cố định, Least Request | Round Robin |
| `xds-lr-ingress` | xDS, Round Robin | Least Request |
| `static-lr-ingress` | Danh sách cố định, Least Request | Least Request |
| `xds-rl` | xDS; trọng số A → B chia theo latency, bandit đóng băng (`configs/shape-model.json`) chọn mức mạnh tay β | Round Robin |
| `xds-beta` | xDS; cùng công thức và khung an toàn, β cố định (`FIXED_BETA` trong `labenv.py`, chọn bằng mô phỏng) | Round Robin |

Ở `xds-rl` và `xds-beta`, a-1 ghi telemetry cho controlplane-1, a-2 cho controlplane-2 (cùng node, qua một volume); hai control plane tự quyết độc lập. Mỗi quyết định in một dòng `rl_window` trong log control plane, mỗi lần quay về Round Robin in `rl_fallback`. C vẫn Round Robin, làm đối chứng ngay trong cùng lượt. Thiết kế: [thử nghiệm RL](../1-do-an/thu-nghiem-rl.md#thiết-kế).

**Giới hạn đã kiểm chứng:** gRPC Python 1.84 không có chính sách Least Request cho caller xDS. Control plane gửi cluster Least Request thì caller từ chối, kể cả khi bọc trong `wrr_locality`. Vì vậy Least Request ở tầng trong chỉ có với caller static; Envoy thì hỗ trợ bình thường. Hệ quả khi đọc kết quả: so `static-lr` với `static` cho thấy tác dụng của chính sách; so với `xds` thì lẫn thêm khác biệt về cách khám phá endpoint.

Phương án có Least Request ở tầng trong cần code triển khai sau ngày 28/10 (xem bước triển khai trong [hướng dẫn cho nhóm](2-huong-dan.md)).

### Quy tắc so sánh chiến lược

Các chiến lược của nhánh lỗi mềm chạy chung cụm và chung lượt `run_load.py`, nhưng không chung đường dữ liệu:

| Chiến lược | Đường A → B/C | Phương án | Mốc Round Robin cùng đường |
|---|---|---|---|
| Round Robin | static và xDS | `static`, `xds` | — |
| Least Request | static | `static-lr` | `static` |
| RL, β cố định | xDS (trọng số locality) | `xds-rl`, `xds-beta` | `xds` |

- **Chỉ kết luận hơn / kém trong cùng một dòng mốc**: Least Request so với `static`; RL và β cố định so với `xds`, và RL so với β cố định (đối thủ chính của RL: nếu RL không hơn nó thì phần học không đóng góp).
- **So Least Request với RL bằng mức cải thiện tương đối** so với mốc của từng bên, không bằng số tuyệt đối: khác đường dữ liệu nên chi phí khám phá endpoint khác nhau.
- So được **cơ chế**: Least Request phản ứng theo từng request bằng số request đang chờ; RL và β cố định phản ứng theo cửa sổ 10 giây bằng latency đo được, qua control plane.
- Có `xds-rl` hoặc `xds-beta` trong lượt thì thêm `--settle 90` (áp cho mọi phương án): mỗi cửa sổ trọng số dịch tối đa 10 điểm và bandit chờ hai cửa sổ cùng ngữ cảnh mới đổi β, nên ô đo 30 giây không đủ để chúng kịp phản ứng.
- `compare_rl_scenarios.py` (lab localhost, bên dưới) dùng để thử nhanh trước khi lên cụm; số của nó không đặt chung bảng với `run_load.py`.
- `xds` so với `static` (cùng Round Robin) cho biết chi phí của cách khám phá endpoint, không nói gì về chiến lược.

## Bài lỗi và phân mảnh — `run_faults.py`

```powershell
.\.venv\Scripts\python.exe scripts/run_faults.py --env configs/lab.cloud.json --repetitions 5
```

Mỗi lần lặp: 20 RPS, 10 giây bình thường, gây lỗi, giữ 40 giây, khôi phục, quan sát thêm 60 giây (`partition-node1` tự quan sát 150 giây). 10 tình huống × 5 lần ≈ 2 giờ. Tham số chính: `--scenarios` (chạy một phần), `--variants` (thêm đối chứng, ví dụ `xds static`), `--repetitions`, `--hold`, `--after`.

| Tình huống | Điều kiểm tra | Tiêu chí |
|---|---|---|
| `backend-stop` | Dừng b-2 | Gián đoạn ≤ 30 giây |
| `controlplane-stop` | Dừng controlplane-1 | Gián đoạn ≤ 30 giây |
| `consul-leader` | Dừng Consul leader hiện tại | Gián đoạn ≤ 30 giây |
| `quorum-loss` | Dừng hai trong ba Consul | Caller xDS trả `UNAVAILABLE`, phục hồi sau khôi phục |
| `node3-down` | Node 3 chết | Gián đoạn ≤ 30 giây |
| `node1-down` | Node 1 chết, kèm Envoy và control plane | Gián đoạn ≤ 30 giây |
| `partition-node3` | Cô lập node 3 | Gián đoạn ≤ 30 giây |
| `partition-node1` | Cô lập node có Envoy | Chỉ quan sát |
| `delay-node3` | Node 3 thêm 100 ms độ trễ | Gián đoạn ≤ 30 giây |
| `loss-node3` | Node 3 rớt 20% gói | Chỉ quan sát |

Tám tình huống đầu là **lỗi cứng**. `delay-node3` và `loss-node3` là **lỗi mềm**: thành phần vẫn trả lời nên health probe không rút nó ra; chúng nối sang bài tải, nơi đo xem chiến lược cân bằng tải có né được không.

**Gián đoạn** tính từ request lỗi đầu tiên đến đầu cửa sổ 5 giây toàn thành công, trong lúc lỗi còn hiệu lực. `quorum-loss` với phương án static chỉ quan sát, vì caller static không phụ thuộc Consul.

Kết quả ở `results/lab-faults/<thời điểm>/`. Dựng lại bảng từ trace có sẵn: `run_faults.py --report results/lab-faults/<thời điểm>`.

## Bài tải — `run_load.py`

```powershell
.\.venv\Scripts\python.exe scripts/run_load.py --env configs/lab.cloud.json --rates 50 100 200 400 --repetitions 3
```

Tự đổi phương án (mặc định `xds`, `static`, `static-lr`), chạy từng mức tải ở hai kịch bản, lặp lại; thứ tự phương án được xáo theo từng lần lặp. Mỗi ô đo 30 giây: mặc định ≈ 75 phút. Thêm `--variants xds-lr-ingress` để so Least Request ở Envoy.

Lượt cho RL: mỗi ô thêm 90 giây chạy tải không đo, khoảng 3 giờ. Cụm phải chạy code có hai phương án này và instance b-3 (triển khai lại nếu bản trên VM cũ hơn). Thử trước trên một máy bằng cách thay `--env configs/lab.cloud.json` bằng `--wsl Ubuntu-22.04` ([phụ lục](#phụ-lục-chạy-thử-trên-một-máy)).

```powershell
.\.venv\Scripts\python.exe scripts/run_load.py --env configs/lab.cloud.json --variants xds xds-beta xds-rl static-lr --rates 50 100 200 --repetitions 3 --settle 90
```

| Kịch bản | Điều kiện |
|---|---|
| `even` | Mọi node bình thường |
| `slow-node3` | Node 3 thêm 50 ms độ trễ (`--slow-ms`): b-2 và c-2 trả lời chậm |

Một ô (phương án, kịch bản, mức tải) **đạt** khi mọi lần lặp có lỗi ≤ 1%, goodput ≥ 98% tải đặt, P99 request thành công < 3 giây và bộ phát tải không bỏ request. Nếu mọi mức đều đạt thì chưa thấy điểm bão hòa: tăng `--rates`.

Bảng trả lời ba câu hỏi, theo thứ tự quan trọng với đồ án: (1) Least Request có né backend chậm không, tức câu hỏi của nhánh lỗi mềm (so `static-lr` với `static` ở `slow-node3`, cột "Phần b-2"; Round Robin ≈ 33% vì B có ba instance); (2) điểm bão hòa, dùng làm mốc để biết kết quả (1) đo ở mức tải nhẹ hay gần bão hòa; (3) static và xDS khác nhau thế nào ở cùng tải. Khi so sánh, theo [quy tắc so sánh chiến lược](#quy-tắc-so-sánh-chiến-lược). Dựng lại bảng: `run_load.py --report results/lab-load/<thời điểm>`.

Bộ phát tải đi qua VPN; cần ghi điều này khi diễn giải độ trễ.

## RL nhiều seed — `compare_rl_scenarios.py`

Chạy trên máy cục bộ, không dùng cloud: script tự dựng hai service và control plane trên localhost.

```powershell
.\.venv\Scripts\python.exe scripts/compare_rl_scenarios.py --model results/rl-convergence-balanced-20261101/model-7.json --output-dir results/rl-multiseed --seeds 41 42 43 44 45
```

Bốn kịch bản × ba policy × năm seed, khoảng 15 phút. `report.md` có median [min–max] giữa các seed và cột hiệu RL − policy theo từng seed. Chỉ kết luận RL tốt hơn khi nó nhanh hơn ở hầu hết seed và khoảng hiệu không chứa 0. Kết quả này chỉ so với Round Robin và quy tắc trong cùng lượt, không so với số Least Request đo trên cụm ([quy tắc so sánh chiến lược](#quy-tắc-so-sánh-chiến-lược)). Bối cảnh và các lượt trước: [thử nghiệm RL](../1-do-an/thu-nghiem-rl.md).

## RL phiên bản 2 — `train_shape_rl.py`, `compare_shape_rl.py`

Chạy trên máy cục bộ, không dùng cloud. Thiết kế: [thử nghiệm RL](../1-do-an/thu-nghiem-rl.md#phiên-bản-2-rl-chọn-mức-mạnh-tay).

**Bước 1, huấn luyện trong mô phỏng** (khoảng 35 phút; mặc định bandit có ngữ cảnh với 2–10 instance, thêm `--method q` để thử Q-learning): năm seed huấn luyện, chọn model bằng seed kiểm định, rồi chạy seed kiểm tra một lần cho model đã chọn. Số trong mục "Tập kiểm tra" của `simulation.md` là số dùng cho báo cáo; **đã đọc nó rồi thì không sửa thiết kế nữa**.

```powershell
.\.venv\Scripts\python.exe scripts/train_shape_rl.py --output-dir results/shape-rl
```

Mô phỏng tất định: lần chạy thử ngày 12/11 chọn seed 9, ra +0,281 so với Round Robin và +0,016 so với β cố định tốt nhất trên tập kiểm tra 5051. Nhóm chạy lại sẽ ra đúng các số đó. Chỉ cần model thì thêm `--seeds 9` (khoảng 7 phút, cùng model).

**Bước 2, so trên gRPC thật.** Chạy khi máy rảnh, mỗi lúc một lượt (xem lưu ý bên dưới).

Ba backend, năm kịch bản × bảy chính sách × ba lần lặp, khoảng 1 giờ 45 phút:

```powershell
.\.venv\Scripts\python.exe scripts/compare_shape_rl.py --model results/shape-rl/model.json --output-dir results/shape-compare
```

Mười backend, ba kịch bản × bảy chính sách × năm lần lặp, khoảng 1 giờ:

```powershell
.\.venv\Scripts\python.exe scripts/compare_shape_rl.py --model results/shape-rl/model.json --backends 10 --scenarios even uneven-capacity flapping --repetitions 5 --log-decisions --output-dir results/shape-compare-10
```

Dự đoán từ mô phỏng, để kiểm chứng: ở 3 backend RL gần hòa Round Robin ở `even` và thắng ở các kịch bản còn lại; ở 10 backend RL thắng ở `uneven-capacity` nhưng còn thua Round Robin ở `even` và `flapping`.

`report.md` có bảng mean/P95/P99/lỗi và bảng hiệu "RL − β cố định tốt nhất", "RL − lr", "RL − rr" theo từng lần lặp. Chỉ kết luận RL hơn một đối thủ ở một kịch bản khi hiệu âm ở **mọi** lần lặp. Chạy thử nhanh: thêm `--scenarios uneven-capacity --repetitions 1`.

- `--backends 10` chạy với 10 backend (mặc định 3). Tổng năng lực giữ nguyên, khoảng 30% số backend gặp sự cố, nên so được giữa các quy mô. Bước 1 đã huấn luyện cho 2 đến 10 instance.
- **Mỗi lúc chỉ chạy một lượt.** Một tiến trình Python chạy cả backend, control plane và bộ phát tải; chạy hai lượt song song làm máy nghẽn và latency tăng giả (đã gặp: 54 giây với timeout 2 giây). Lượt nào có event loop trễ quá 20 ms hoặc request gửi trễ lịch quá 50 ms bị loại khỏi bảng và được ghi tên trong `report.md`.
- Ở 10 backend, kết quả dao động mạnh giữa các lần chạy, kể cả Round Robin: dùng `--repetitions 5`. Thêm `--log-decisions` để có `decisions.log` (latency, trạng thái, β, trọng số từng cửa sổ), `--policies rl rr` để chạy riêng vài chính sách.

## Vấn đề đã biết

**Cả hai vấn đề dưới đây mới quan sát trên bản dựng một máy, mỗi tình huống một lần.** Lượt đó có 7/10 tình huống đạt; chưa ô nào được đo trên cụm. Việc đầu tiên trên cụm là chạy lại đúng tình huống đó với 5 lần lặp trên bản baseline: nếu tái hiện được thì nó là bảng "trước"; nếu không thì ghi lại là hiện tượng riêng của bản một máy và chọn ô không đạt khác từ bảng baseline. Với mỗi ô không đạt, phân loại trước khi sửa: lỗi của hệ thống, lỗi của công cụ đo, hay tiêu chí đặt chưa hợp lý.

**Phục hồi chậm sau phân mảnh dài** (`partition-node1`, lượt chạy thử 11/11). Sau khi gỡ phân mảnh, khoảng 50% request vẫn lỗi; a-1 khoảng 2 phút sau mới phục vụ lại. Manh mối từ log, chưa được kiểm chứng:

- Health probe của control plane đánh dấu b-1 hỏng 4 giây sau khi phân mảnh, nhưng 2 phút 3 giây sau khi gỡ mới đánh dấu khỏe lại. Hãy tìm hiểu cơ chế kết nối lại (reconnect backoff) của kênh gRPC.
- consul-1 trả lỗi cho đọc nhất quán thêm khoảng 35 giây sau khi gỡ.

Nếu tái hiện được trên cụm, đây là ô "không đạt" để nhóm luyện quy trình: đo trước, tìm nguyên nhân, sửa, đo lại.

**Đăng ký theo node.** Mỗi instance đăng ký qua Consul cùng node. Khi Consul của một node chết, instance cùng node có thể bị rút khỏi routing một thời gian dù vẫn khỏe (lượt chạy thử: b-1 vắng khoảng 20 giây khi consul-1 dừng).

## Sự cố thường gặp

- **`check` báo `false` cho một node**: đọc dòng lý do in bên dưới. Hay gặp: VPN tắt, file khóa không nằm ở `~/.ssh/uitiot-lab.pem`, thiếu `sudo` không mật khẩu.
- **`lab not ready`**: chưa đủ bảy instance trả lời. Xem `status`; instance dừng thì `start <tên>`.
- **Lượt chạy bị ngắt giữa chừng**: chạy `heal`, rồi `status` để kiểm tra trước khi chạy tiếp.
- **Build lỗi khi triển khai**: VM lỗi DNS, xem bước triển khai trong [hướng dẫn cho nhóm](2-huong-dan.md).

## Phụ lục: chạy thử trên một máy

Để thử thay đổi code mà chưa triển khai lên cloud, cùng 14 thành phần có thể chạy trên một máy Windows bằng Docker trong WSL (`deploy/compose.lab.yaml`). Thay `--env configs/lab.cloud.json` bằng `--wsl Ubuntu-22.04` trong mọi lệnh, và dùng `labenv.py --wsl Ubuntu-22.04 up --variant xds --build` để dựng. Hai lưu ý:

- WSL tự tắt khi rảnh: để `labenv.py --wsl Ubuntu-22.04 hold` chạy ở một terminal riêng; nếu gặp `Wsl/Service/E_UNEXPECTED`, chạy `wsl --shutdown` rồi thử lại.
- Các node chung CPU và không có độ trễ mạng thật: **số liệu từ cách này không dùng cho báo cáo**.
