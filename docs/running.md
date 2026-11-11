# Tham chiếu thí nghiệm

Tài liệu tra cứu: tình huống lỗi, phương án, tiêu chí đạt, tham số và các vấn đề đã biết. Thứ tự làm việc từng bước nằm ở [hướng dẫn cho nhóm](team-guide.md); đọc tài liệu đó trước.

Mọi lệnh chạy trên máy Windows của bạn, từ thư mục gốc của repo, và điều khiển ba VM trên cloud UIT-IoT qua tùy chọn `--env configs/lab.cloud.json`.

**Trạng thái kiểm chứng (11/11/2026).** Hai bộ chạy đã chạy thật trên bản dựng một máy (đủ 10 tình huống lỗi, mỗi tình huống một lần, và một lượt tải ngắn). Phần điều khiển ba VM qua SSH đã qua kiểm thử logic nhưng **chưa chạy trên VM thật**. Các kết quả chạy thử trong `results/` chỉ để chứng minh công cụ hoạt động, không dùng cho báo cáo.

## Môi trường

| Node | VM (IP nội bộ) | Thành phần |
|---|---|---|
| node1 | 192.168.1.238 | consul-1, envoy-1, controlplane-1, a-1, b-1 |
| node2 | 192.168.1.172 | consul-2, envoy-2, controlplane-2, a-2, c-1 |
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

**Giới hạn đã kiểm chứng:** gRPC Python 1.84 không có chính sách Least Request cho caller xDS. Control plane gửi cluster Least Request thì caller từ chối, kể cả khi bọc trong `wrr_locality`. Vì vậy Least Request ở tầng trong chỉ có với caller static; Envoy thì hỗ trợ bình thường. Hệ quả khi đọc kết quả: so `static-lr` với `static` cho thấy tác dụng của chính sách; so với `xds` thì lẫn thêm khác biệt về cách khám phá endpoint.

Phương án có Least Request ở tầng trong cần code triển khai sau ngày 28/10 (xem bước triển khai trong [hướng dẫn cho nhóm](team-guide.md)).

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

**Gián đoạn** tính từ request lỗi đầu tiên đến đầu cửa sổ 5 giây toàn thành công, trong lúc lỗi còn hiệu lực. `quorum-loss` với phương án static chỉ quan sát, vì caller static không phụ thuộc Consul.

Kết quả ở `results/lab-faults/<thời điểm>/`. Dựng lại bảng từ trace có sẵn: `run_faults.py --report results/lab-faults/<thời điểm>`.

## Bài tải — `run_load.py`

```powershell
.\.venv\Scripts\python.exe scripts/run_load.py --env configs/lab.cloud.json --rates 50 100 200 400 --repetitions 3
```

Tự đổi phương án (mặc định `xds`, `static`, `static-lr`), chạy từng mức tải ở hai kịch bản, lặp lại; thứ tự phương án được xáo theo từng lần lặp. Mỗi ô đo 30 giây: mặc định ≈ 75 phút. Thêm `--variants xds-lr-ingress` để so Least Request ở Envoy.

| Kịch bản | Điều kiện |
|---|---|
| `even` | Mọi node bình thường |
| `slow-node3` | Node 3 thêm 50 ms độ trễ (`--slow-ms`): b-2 và c-2 trả lời chậm |

Một ô (phương án, kịch bản, mức tải) **đạt** khi mọi lần lặp có lỗi ≤ 1%, goodput ≥ 98% tải đặt, P99 request thành công < 3 giây và bộ phát tải không bỏ request. Nếu mọi mức đều đạt thì chưa thấy điểm bão hòa: tăng `--rates`.

Bảng trả lời ba câu hỏi: (1) static và xDS khác nhau thế nào ở cùng tải; (2) Least Request có né backend chậm không (so `static-lr` với `static` ở `slow-node3`, cột "Phần b-2"; Round Robin ≈ 50%); (3) điểm bão hòa. Dựng lại bảng: `run_load.py --report results/lab-load/<thời điểm>`.

Bộ phát tải đi qua VPN, và ba VM còn cài sẵn k3s (đồ án không dùng nhưng nó vẫn chiếm một phần CPU, RAM); cần ghi hai điều này khi diễn giải độ trễ.

## RL nhiều seed — `compare_rl_scenarios.py`

Chạy trên máy cục bộ, không dùng cloud: script tự dựng hai service và control plane trên localhost.

```powershell
.\.venv\Scripts\python.exe scripts/compare_rl_scenarios.py --model results/rl-convergence-balanced-20261101/model-7.json --output-dir results/rl-multiseed --seeds 41 42 43 44 45
```

Bốn kịch bản × ba policy × năm seed, khoảng 15 phút. `report.md` có median [min–max] giữa các seed và cột hiệu RL − policy theo từng seed. Chỉ kết luận RL tốt hơn khi nó nhanh hơn ở hầu hết seed và khoảng hiệu không chứa 0. Bối cảnh và các lượt trước: [thử nghiệm RL](rl-lookaside-experiment.md).

## Vấn đề đã biết

**Phục hồi chậm sau phân mảnh dài** (`partition-node1`, lượt chạy thử 11/11). Sau khi gỡ phân mảnh, khoảng 50% request vẫn lỗi; a-1 khoảng 2 phút sau mới phục vụ lại. Manh mối từ log, chưa được kiểm chứng:

- Health probe của control plane đánh dấu b-1 hỏng 4 giây sau khi phân mảnh, nhưng 2 phút 3 giây sau khi gỡ mới đánh dấu khỏe lại. Hãy tìm hiểu cơ chế kết nối lại (reconnect backoff) của kênh gRPC.
- consul-1 trả lỗi cho đọc nhất quán thêm khoảng 35 giây sau khi gỡ.

Đây là một ô "không đạt" để nhóm luyện quy trình: đo trước, tìm nguyên nhân, sửa, đo lại.

**Đăng ký theo node.** Mỗi instance đăng ký qua Consul cùng node. Khi Consul của một node chết, instance cùng node có thể bị rút khỏi routing một thời gian dù vẫn khỏe (lượt chạy thử: b-1 vắng khoảng 20 giây khi consul-1 dừng).

## Sự cố thường gặp

- **`check` báo `false` cho một node**: đọc dòng lý do in bên dưới. Hay gặp: VPN tắt, file khóa không nằm ở `~/.ssh/uitiot-lab.pem`, thiếu `sudo` không mật khẩu.
- **`lab not ready`**: chưa đủ sáu instance trả lời. Xem `status`; instance dừng thì `start <tên>`.
- **Lượt chạy bị ngắt giữa chừng**: chạy `heal`, rồi `status` để kiểm tra trước khi chạy tiếp.
- **Build lỗi khi triển khai**: VM lỗi DNS, xem bước triển khai trong [hướng dẫn cho nhóm](team-guide.md).

## Phụ lục: chạy thử trên một máy

Để thử thay đổi code mà chưa triển khai lên cloud, cùng 13 thành phần có thể chạy trên một máy Windows bằng Docker trong WSL (`deploy/compose.lab.yaml`). Thay `--env configs/lab.cloud.json` bằng `--wsl Ubuntu-22.04` trong mọi lệnh, và dùng `labenv.py --wsl Ubuntu-22.04 up --variant xds --build` để dựng. Hai lưu ý:

- WSL tự tắt khi rảnh: để `labenv.py --wsl Ubuntu-22.04 hold` chạy ở một terminal riêng; nếu gặp `Wsl/Service/E_UNEXPECTED`, chạy `wsl --shutdown` rồi thử lại.
- Các node chung CPU và không có độ trễ mạng thật: **số liệu từ cách này không dùng cho báo cáo**.
