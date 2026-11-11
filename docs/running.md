# Hướng dẫn chạy thí nghiệm

Mỗi việc trong [kế hoạch một tuần](../README.md#kế-hoạch-một-tuần-1118112026) có một lệnh ở đây. Mọi lệnh chạy từ thư mục gốc của repo.

> **Trạng thái kiểm chứng (11/11/2026).** Phần RL nhiều seed đã chạy thử thành công. Lab ba node đã chạy thật trên Docker trong WSL: đủ 10 tình huống, mỗi tình huống một lần, kết quả ở `results/lab-faults/smoke-full/`. Một vấn đề đã biết: sau khi gỡ phân mảnh node 1, a-1 mất khoảng hai phút mới phục vụ lại (xem [Vấn đề đã biết](#vấn-đề-đã-biết)).

## Chuẩn bị

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```

Lab cần Docker Engine và Docker Compose. Trên Windows, Docker chạy trong WSL; mọi lệnh lab tự gọi qua `wsl` với quyền root. Nếu máy có nhiều bản WSL, thêm `--wsl Ubuntu-22.04` ngay sau tên script. Nếu Compose không phải plugin `docker compose`, chỉ đường dẫn bằng `--compose` hoặc biến `LAB_COMPOSE`.

Trên Linux, các thao tác mạng cần `sudo` không hỏi mật khẩu cho `nsenter`.

**WSL tự tắt khi rảnh.** Vài giây sau khi phiên WSL cuối cùng đóng, Windows dừng bản phân phối: toàn bộ container khởi động lại và cổng `localhost` chuyển tiếp bị đứt. `run_faults.py` tự giữ một phiên trong suốt lượt chạy. Khi thao tác tay (`up`, `partition`, `controlled_probe.py`...), mở thêm một terminal và để lệnh này chạy:

```powershell
.\.venv\Scripts\python.exe scripts/labenv.py hold
```

## Lab ba node

Ba node là ba nhóm container cùng nhãn `lab.node` trên một máy:

| Node | Thành phần |
|---|---|
| node1 | consul-1, envoy-1, controlplane-1, a-1, b-1 |
| node2 | consul-2, envoy-2, controlplane-2, a-2, c-1 |
| node3 | consul-3, b-2, c-2 |

Bộ phát tải chạy ngoài ba node và gọi hai Envoy qua `localhost:50050` và `localhost:50060` trên cùng một channel gRPC; khi Envoy đang dùng mất kết nối, channel chuyển sang Envoy còn lại.

```powershell
.\.venv\Scripts\python.exe scripts/labenv.py check              # công cụ cần có trên máy Docker
.\.venv\Scripts\python.exe scripts/labenv.py up --mode xds --build
.\.venv\Scripts\python.exe scripts/labenv.py status
.\.venv\Scripts\python.exe scripts/labenv.py down
```

`check` phải báo `true` cho `docker`, `compose`, `nsenter`, `iptables`, `tc`. Nếu `netem` là `false`, hai bài `delay-node3` và `loss-node3` không chạy được trên máy đó; các bài còn lại không bị ảnh hưởng.

Gây lỗi thủ công khi cần demo:

```powershell
.\.venv\Scripts\python.exe scripts/labenv.py stop-node node3
.\.venv\Scripts\python.exe scripts/labenv.py start-node node3
.\.venv\Scripts\python.exe scripts/labenv.py partition --a node3 --b node1 node2
.\.venv\Scripts\python.exe scripts/labenv.py degrade node3 --delay-ms 100
.\.venv\Scripts\python.exe scripts/labenv.py heal                # gỡ mọi phân mảnh và độ trễ
```

## Việc 1 và 2 — thử lỗi và phân mảnh mạng (Đạt)

```powershell
.\.venv\Scripts\python.exe scripts/run_faults.py --repetitions 5
```

Lệnh chạy 10 tình huống × 5 lần, mỗi lần 80 giây ở 20 RPS (10 giây bình thường, 40 giây giữ lỗi, 30 giây sau khôi phục): khoảng 70 phút. Chạy một phần bằng `--scenarios`, thêm đối chứng static bằng `--modes static xds`.

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

Kết quả nằm ở `results/lab-faults/<thời điểm>/`: `report.md` (bảng median [min–max] và kết luận), `report.json`, và trace gốc của từng lần lặp (`.jsonl`, `.events.json`). Dựng lại bảng từ trace có sẵn:

```powershell
.\.venv\Scripts\python.exe scripts/run_faults.py --report results/lab-faults/<thời điểm>
```

`partition-node1` không có tiêu chí đạt: Envoy ở phía thiểu số vẫn nhận kết nối nhưng không tới được service C, nên client đang dùng Envoy đó sẽ thấy lỗi suốt thời gian phân mảnh. Ghi hành vi này vào báo cáo như một giới hạn của thiết kế.

**Khi không đạt:** đọc `error_codes_in_fault` và `instances_serving_in_fault` trong `report.json`, xem log bằng `docker logs` trong WSL, sửa, rồi chạy lại riêng tình huống đó. Giữ nguyên thư mục kết quả không đạt.

## Việc 3 — tải ổn định và bão hòa (Lầu)

Với lab đang chạy ở chế độ cần đo:

```powershell
.\.venv\Scripts\python.exe scripts/labenv.py up --mode xds
.\.venv\Scripts\python.exe scripts/controlled_probe.py --address "ipv4:127.0.0.1:50050,127.0.0.1:50060" --mode xds --rate 150 --duration 180 --output results/lab-load/xds-150-r1.json
```

Lặp lại với `--mode static` (sau `labenv.py up --mode static`) và các mức `--rate` tăng dần đến khi goodput không còn tăng. Chưa có bộ chạy tự động nhiều mức và nhiều lần lặp cho lab; `controlled_suite.py` hiện chỉ đổi chế độ được trên cụm cloud.

Bộ phát tải chạy trên Windows có độ phân giải timer thô, và các node chung CPU. Dùng số liệu này để so sánh tương đối giữa hai chế độ, không làm số tuyệt đối.

## Việc 4 — RL nhiều seed (Khoa)

Không cần Docker; script tự dựng hai service và control plane trên localhost.

```powershell
.\.venv\Scripts\python.exe scripts/compare_rl_scenarios.py --model results/rl-convergence-balanced-20261101/model-7.json --output-dir results/rl-multiseed --seeds 41 42 43 44 45
```

Bốn kịch bản × ba policy × năm seed, khoảng 15 phút. `report.md` có median [min–max] giữa các seed và cột hiệu RL − policy theo từng seed. Chỉ kết luận RL tốt hơn khi nó nhanh hơn ở hầu hết seed và khoảng hiệu không chứa 0.

## Lần chạy đầu

Lab chưa từng chạy với Docker thật, nên lần đầu chạy theo thứ tự này và dừng ở bước đầu tiên lỗi:

1. `labenv.py check` — đủ công cụ.
2. `labenv.py up --mode xds --build` rồi `labenv.py status` — đủ 13 container `running`.
3. `run_faults.py --scenarios backend-stop --repetitions 1` — bài đơn giản nhất, xác nhận bộ phát tải tới được Envoy và báo cáo sinh ra.
4. `run_faults.py --scenarios partition-node3 --repetitions 1` — xác nhận `nsenter`/`iptables` hoạt động.
5. Toàn bộ bộ thử lỗi.

## Vấn đề đã biết

**Phục hồi chậm sau phân mảnh dài** (`partition-node1`, lượt 11/11). Sau khi gỡ phân mảnh, khoảng 50% request vẫn lỗi trong suốt 30 giây quan sát; a-1 chỉ phục vụ lại khoảng 2 phút sau. Theo log, nguyên nhân có khả năng nhất:

1. Kênh gRPC tới endpoint không liên lạc được bị đẩy vào backoff kết nối lại, mặc định tối đa 120 giây. Health probe của control plane đánh dấu b-1 hỏng 4 giây sau khi phân mảnh, nhưng 2 phút 3 giây sau khi gỡ mới đánh dấu khỏe lại. Kênh của a-1 tới B và C nhiều khả năng bị cùng hiện tượng.
2. consul-1 trả lỗi cho đọc nhất quán thêm khoảng 35 giây sau khi gỡ, nên controlplane-1 tiếp tục phát danh sách endpoint rỗng cho a-1.

Hướng sửa dự kiến: giới hạn backoff kết nối lại (`grpc.max_reconnect_backoff_ms`) cho kênh của caller và health probe, rồi chạy lại `partition-node1` với `--after 60` để đo trước và sau.

## Sự cố thường gặp

- **`Wsl/Service/E_UNEXPECTED`**: dịch vụ WSL đang lỗi. Chạy `wsl --shutdown` rồi thử lại; nếu vẫn lỗi, khởi động lại Windows.
- **`lab not ready`**: chưa đủ sáu instance trả lời. Xem `labenv.py status`; container dừng thì `labenv.py start <tên>`.
- **Lab kẹt sau khi script bị ngắt**: `labenv.py heal`, rồi `labenv.py up`.
- **Cổng 50050 hoặc 50060 đã bị chiếm**: tắt stack cũ (`loadbalance-microservices`) trước khi `up`.
