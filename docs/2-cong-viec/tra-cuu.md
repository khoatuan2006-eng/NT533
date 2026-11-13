# Tra cứu

Trang này để tra, không đọc từ đầu đến cuối. Thứ tự làm việc nằm ở [huong-dan-chay.md](huong-dan-chay.md).

## Môi trường

| Node | IP nội bộ | Thành phần |
|---|---|---|
| node1 | 192.168.1.238 | consul-1, envoy-1, controlplane-1, a-1, b-1 |
| node2 | 192.168.1.172 | consul-2, envoy-2, controlplane-2, a-2, c-1, b-3 |
| node3 | 192.168.1.217 | consul-3, b-2, c-2 |

- Bộ phát tải gửi tới LB của cloud, `192.168.120.109:80`.
- SSH tới node2 và node3 tự đi qua node1.
- Mô tả cụm: `configs/lab.cloud.json`; SSH: `configs/ssh-config`. Dùng chung cho cả nhóm.

## Lệnh điều khiển cụm

Tất cả có dạng `.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json <lệnh>`.

| Lệnh | Tác dụng | Thực hiện trên VM bằng |
|---|---|---|
| `check` | Kiểm tra kết nối tới ba node | SSH |
| `status` | Liệt kê 14 thành phần và trạng thái | `docker compose ps` |
| `stop <tên>` / `start <tên>` | Dừng / chạy lại một instance, ví dụ `b-2` | `docker compose stop/start` |
| `stop-node <node>` / `start-node <node>` | Node chết / sống lại | Dừng mọi container của lab trên VM đó; VM vẫn bật |
| `partition --a <node…> --b <node…>` | Phân mảnh mạng giữa hai nhóm | `iptables` chặn IP nội bộ của nhóm bên kia |
| `degrade <node> --delay-ms N` | Thêm độ trễ | `tc netem`, chỉ cho gói gửi tới node khác của lab |
| `degrade <node> --loss-percent N` | Rớt gói | `tc netem` |
| `heal` | Gỡ mọi luật mạng | — |
| `up --variant <tên>` | Đổi phương án | Ghi lại cấu hình, tạo lại service liên quan |
| `lock-status` / `unlock` | Xem / gỡ khóa cụm | — |

An toàn đã có sẵn: luật mạng tự hết hạn sau 15 phút; luật phân mảnh luôn đặt ở phía còn SSH tới được; độ trễ không ảnh hưởng SSH; lệnh `down` bị chặn vì cụm dùng chung.

## Phương án (`--variants`)

| Phương án | A → B/C | Envoy → A |
|---|---|---|
| `xds` | xDS, Round Robin | Round Robin |
| `static` | Danh sách cố định, Round Robin | Round Robin |
| `static-lr` | Danh sách cố định, Least Request | Round Robin |
| `xds-lr-ingress` | xDS, Round Robin | Least Request |
| `static-lr-ingress` | Danh sách cố định, Least Request | Least Request |
| `xds-beta` | xDS; trọng số A → B theo latency, β = 2 cố định | Round Robin |
| `xds-rl` | xDS; trọng số A → B theo latency, bandit chọn β | Round Robin |

Ở `xds-beta` và `xds-rl`, chặng A → C vẫn Round Robin và làm đối chứng trong cùng lượt.

## Quy tắc so sánh

Least Request chỉ chạy trên đường static, RL chỉ chạy trên đường xDS. Vì vậy:

| Chiến lược | Phương án | Mốc Round Robin cùng đường |
|---|---|---|
| Least Request | `static-lr` | `static` |
| β cố định, RL | `xds-beta`, `xds-rl` | `xds` |

- **Chỉ kết luận hơn / kém so với mốc cùng đường.**
- RL so trực tiếp với β cố định (cùng đường, cùng công thức). Đây là phép so quan trọng nhất của phần RL.
- Least Request và RL chỉ so với nhau bằng **mức cải thiện tương đối** so với mốc của từng bên, không bằng số tuyệt đối.
- `xds` so với `static` cho biết chi phí của cách khám phá endpoint, không nói gì về chiến lược.

## Bài lỗi — `run_faults.py`

Mỗi lần lặp: 20 RPS, 10 giây bình thường, gây lỗi, giữ 40 giây, khôi phục, quan sát 60 giây.

| Tình huống | Gây lỗi | Nhánh | Tiêu chí |
|---|---|---|---|
| `backend-stop` | Dừng b-2 | Cứng | Gián đoạn ≤ 30 giây |
| `controlplane-stop` | Dừng controlplane-1 | Cứng | Gián đoạn ≤ 30 giây |
| `consul-leader` | Dừng Consul leader hiện tại | Cứng | Gián đoạn ≤ 30 giây |
| `quorum-loss` | Dừng hai trong ba Consul | Cứng | A trả `UNAVAILABLE`, phục hồi sau khi khôi phục |
| `node3-down` | Node 3 chết | Cứng | Gián đoạn ≤ 30 giây |
| `node1-down` | Node 1 chết (có Envoy và control plane) | Cứng | Gián đoạn ≤ 30 giây |
| `partition-node3` | Cô lập node 3 | Cứng | Gián đoạn ≤ 30 giây |
| `partition-node1` | Cô lập node 1 (có Envoy) | Cứng | Chỉ quan sát |
| `delay-node3` | Node 3 thêm 100 ms độ trễ | Mềm | Gián đoạn ≤ 30 giây |
| `loss-node3` | Node 3 rớt 20% gói | Mềm | Chỉ quan sát |

Cột trong `report.md`:

| Cột | Nghĩa |
|---|---|
| Gián đoạn (s) | Từ request lỗi đầu tiên tới đầu cửa sổ 5 giây toàn thành công, trong lúc lỗi còn hiệu lực. "Chưa hội tụ" là không có cửa sổ nào như vậy |
| Lỗi trong lúc lỗi (%) | Tỷ lệ request lỗi trong 40 giây giữ lỗi |
| Phục hồi sau khôi phục (s) | Từ lúc gỡ lỗi tới khi ổn định lại |
| Instance còn phục vụ | Instance có request thành công sau 5 giây đầu của lỗi |

Tham số: `--scenarios`, `--variants` (mặc định `xds`), `--repetitions` (5), `--rate` (20), `--hold` (40), `--after` (60), `--report <thư-mục>`.

## Bài tải — `run_load.py`

Mỗi ô (phương án, kịch bản, mức tải) đo 30 giây; thứ tự phương án được xáo theo từng lần lặp.

| Kịch bản | Điều kiện |
|---|---|
| `even` | Mọi node bình thường |
| `slow-node3` | Node 3 thêm 50 ms độ trễ: b-2 và c-2 trả lời chậm |

Một ô **đạt** khi mọi lần lặp có: lỗi ≤ 1%, goodput ≥ 98% tải đặt, P99 của request thành công < 3 giây, và bộ phát tải không bỏ request. **Điểm bão hòa** là mức tải cao nhất còn đạt; nếu mọi mức đều đạt thì chưa thấy điểm bão hòa.

**Phần b-2** là tỷ lệ request B do b-2 (trên node 3) phục vụ. B có ba instance nên Round Robin cho khoảng 33%; chiến lược né được backend chậm cho số thấp hơn ở `slow-node3`.

Tham số: `--variants` (mặc định `xds static static-lr`), `--scenarios`, `--rates` (50 100 200 400), `--repetitions` (3), `--duration` (30), `--settle` (0; dùng 90 khi có RL), `--slow-ms` (50), `--report <thư-mục>`.

## RL trên gRPC thật — `compare_shape_rl.py`

Chạy trên máy cục bộ: script tự dựng backend, control plane và bộ phát tải.

| Kịch bản | Điều kiện |
|---|---|
| `even` | Các backend giống nhau |
| `processing-slow` | Một phần backend xử lý chậm |
| `uneven-capacity` | Năng lực các backend lệch nhau |
| `flapping` | Độ chậm bật tắt |
| `load-step` | Tải nhảy giữa chừng |

Chính sách được so: Round Robin, Least Request, β cố định (0,5; 1; 2; 4) và RL. `report.md` có bảng mean/P95/P99/lỗi và bảng hiệu RL trừ từng đối thủ theo từng lần lặp. **Chỉ kết luận RL hơn một đối thủ ở một kịch bản khi hiệu âm ở mọi lần lặp.**

Lượt nào máy đo bị nghẽn (request gửi trễ lịch) sẽ bị loại khỏi bảng và được ghi tên trong `report.md`; ghi lại số lượt bị loại.

Tham số: `--model`, `--output-dir`, `--backends` (3), `--scenarios`, `--repetitions` (3), `--policies`, `--log-decisions`.

## Sự cố thường gặp

| Hiện tượng | Xử lý |
|---|---|
| `check` báo `false` cho một node | Đọc dòng lý do in bên dưới. Hay gặp: VPN tắt, file khóa không nằm ở `~/.ssh/uitiot-lab.pem` |
| `lab not ready` | Chưa đủ bảy instance service trả lời. Xem `status`; instance dừng thì `start <tên>` |
| Bị từ chối vì cụm đang khóa | `lock-status` để xem ai đang chạy; chờ hoặc hỏi người đó |
| Lượt chạy bị ngắt giữa chừng | `heal`, rồi `status` trước khi chạy tiếp |
| `deploy` lỗi vì không tải được gói Python | VM lỗi DNS: `ssh -F configs/ssh-config <IP> "sudo resolvectl dns ens3 1.1.1.1"` rồi chạy lại |
