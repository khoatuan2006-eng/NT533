# Hướng dẫn cho nhóm

Hệ thống chạy trên **ba VM của cloud UIT-IoT** (node1, node2, node3), mỗi VM chạy phần của mình bằng Docker Compose. Các script chạy trên **máy của bạn**: bộ phát tải gửi request qua VPN tới LB cloud, còn các thao tác gây lỗi được gửi tới VM qua SSH.

```text
Máy của bạn                                Cloud UIT-IoT
┌───────────────────────┐   VPN    ┌──────────────────────────────┐
│ run_faults / run_load │ ───────► │ LB 192.168.120.109:80        │
│  - bộ phát tải        │          │   → Envoy node1 / node2      │
│  - gây lỗi qua SSH    │ ───────► │ node1, node2, node3 (Docker) │
└───────────────────────┘   SSH    └──────────────────────────────┘
```

Mỗi thành viên tự làm đủ bốn việc: **chạy → kiểm tra → đánh giá → sửa để cải thiện**. Cả nhóm dùng chung một cụm, nên đọc kỹ mục 3 trước khi chạy.

Mọi lệnh chạy từ thư mục gốc của repo trong PowerShell. Kết quả mỗi lượt nằm trong một thư mục mới dưới `results/`; **không xóa hay sửa** thư mục kết quả, kể cả lần không đạt.

## 1. Chuẩn bị máy (mỗi người, một lần)

```powershell
git pull
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```

Kết quả mong đợi: tất cả test đạt.

Kết nối cloud:

1. Bật VPN UIT-IoT.
2. Chép file khóa SSH của nhóm thành `C:\Users\<tên-bạn>\.ssh\uitiot-lab.pem`. Không cần sửa file cấu hình nào: `configs/ssh-config` và `configs/lab.cloud.json` dùng chung cho cả nhóm và đã trỏ tới đường dẫn này.
3. Kiểm tra:

```powershell
ssh -F configs/ssh-config lb-node1 hostname
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json check
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json status
```

Kết quả mong đợi: `check` báo `true` cho cả ba node; `status` liệt kê 13 thành phần đều `running`. Nếu một node `false`, lý do được in ngay bên dưới (hay gặp: VPN tắt, file khóa sai chỗ).

node2 và node3 không có IP trong mạng VPN; SSH tới chúng tự đi qua node1.

## 2. Hiểu hệ thống trước khi đo

Đọc phần kiến trúc trong [README](../README.md), rồi thử tay. Mỗi bước, quan sát rồi mới chuyển bước sau:

```powershell
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json stop b-2
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json start b-2
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json partition --a node3 --b node1 node2
.\.venv\Scripts\python.exe scripts/labenv.py --env configs/lab.cloud.json heal
```

Xem log control plane để thấy nó phát hiện và rút endpoint:

```powershell
ssh -F configs/ssh-config lb-node1 "sudo docker compose -p lb-cloud -f /opt/loadbalance/cluster/node1/compose.json logs --tail 20 controlplane"
```

Câu hỏi nên tự trả lời được: khi b-2 dừng, ai phát hiện, sau bao lâu, và service A biết bằng cách nào?

## 3. Dùng chung cụm

- **Mỗi lúc chỉ một người chạy.** `run_faults.py` và `run_load.py` tự giữ khóa cụm trong suốt lượt chạy; người khác chạy cùng lúc sẽ bị từ chối và được báo ai đang dùng. Các lệnh thử tay ở mục 2 cũng bị từ chối khi có người đang chạy.
- Xem ai đang dùng: `labenv.py --env configs/lab.cloud.json lock-status`.
- Nếu khóa bị bỏ lại (máy người chạy bị tắt giữa chừng), **hỏi người đó trước**, rồi mới `labenv.py --env configs/lab.cloud.json unlock` và `heal`.
- Lượt dài (bài lỗi đầy đủ khoảng 2 giờ) nên hẹn giờ trong nhóm, ví dụ chạy buổi tối.
- **Báo nhóm trước khi triển khai code mới** (mục 5): triển khai khởi động lại toàn bộ cụm. Lệnh triển khai tự từ chối khi có người đang chạy.

## 4. Chạy, kiểm tra, đánh giá

### Chạy

Lần đầu, chạy hai bài ngắn để chắc mọi thứ hoạt động:

```powershell
.\.venv\Scripts\python.exe scripts/run_faults.py --env configs/lab.cloud.json --scenarios backend-stop --repetitions 1
.\.venv\Scripts\python.exe scripts/run_faults.py --env configs/lab.cloud.json --scenarios partition-node3 --repetitions 1
```

Sau đó chạy đầy đủ:

```powershell
.\.venv\Scripts\python.exe scripts/run_faults.py --env configs/lab.cloud.json --repetitions 5
.\.venv\Scripts\python.exe scripts/run_load.py --env configs/lab.cloud.json --rates 50 100 200 400 --repetitions 3
```

Bài lỗi mất khoảng 2 giờ, bài tải khoảng 75 phút. Nếu mọi mức tải đều đạt thì chưa thấy điểm bão hòa: chạy thêm `--rates 600 800 1200`. Danh sách tình huống, phương án và tiêu chí: [tham chiếu thí nghiệm](running.md).

RL nhiều seed chạy trên máy của bạn, không dùng cụm:

```powershell
.\.venv\Scripts\python.exe scripts/compare_rl_scenarios.py --model results/rl-convergence-balanced-20261101/model-7.json --output-dir results/rl-multiseed --seeds 41 42 43 44 45
```

Nếu lượt chạy bị ngắt giữa chừng: `labenv.py --env configs/lab.cloud.json heal`. Dù quên, luật mạng cũng tự hết hạn sau 15 phút và khóa cụm được nhả khi script thoát.

### Kiểm tra

Mỗi lượt tạo một thư mục, ví dụ `results/lab-faults/20261112-210000/`:

| File | Nội dung |
|---|---|
| `report.md` | Bảng tổng hợp và kết luận đạt / không đạt |
| `report.json` | Số liệu từng lần lặp |
| `*.jsonl` | Từng request: thời điểm, mã kết quả, latency, instance đã phục vụ |
| `*.events.json` | Mốc gây lỗi và khôi phục |
| `run.json` | Tham số, commit git, cấu hình từng VM |

Đừng chỉ đọc `report.md`: chọn một lần lặp, mở `*.jsonl` và `*.events.json`, tự đếm lại số request lỗi quanh mốc gây lỗi để chắc bảng tính đúng. Dựng lại bảng từ dữ liệu có sẵn, không chạy lại:

```powershell
.\.venv\Scripts\python.exe scripts/run_faults.py --report results/lab-faults/<thư-mục>
.\.venv\Scripts\python.exe scripts/run_load.py --report results/lab-load/<thư-mục>
```

### Đánh giá

Với mỗi ô trong bảng, viết ra: đạt hay không theo tiêu chí; con số chính (gián đoạn, điểm bão hòa, phần b-2); và **vì sao** hệ thống hành xử như vậy, dựa vào kiến trúc. Bảng bài lỗi: **gián đoạn** là thời gian từ request lỗi đầu tiên tới khi ổn định lại; **instance còn phục vụ** cho biết tải đã chuyển sang đâu. Bảng bài tải: **mức tải cao nhất còn đạt** là điểm bão hòa; **phần b-2** cho biết có né backend chậm không (Round Robin ≈ 50%).

## 5. Sửa để cải thiện

1. **Xác định hiện tượng**: trong `report.json`, xem `error_codes_in_fault` (loại lỗi) và `instances_serving_in_fault` (ai còn phục vụ).
2. **Tìm thời điểm**: trong `*.jsonl`, lọc request lỗi theo `scheduled_s`; so với mốc trong `*.events.json`.
3. **Đọc log** thành phần liên quan quanh thời điểm đó (lệnh `logs` như mục 2, đổi node và tên service).
4. **Đặt giả thuyết, sửa một thứ** trên nhánh git riêng, chạy `pytest` trên máy mình.
5. **Triển khai** bản sửa lên cụm (báo nhóm trước):

```powershell
.\.venv\Scripts\python.exe scripts/multihost.py render --nodes 192.168.1.238 192.168.1.172 192.168.1.217 --ingress-port 18080 --output results/cloud-cluster
.\.venv\Scripts\python.exe scripts/multihost.py bundle --cluster results/cloud-cluster --output results/loadbalance-release.tar.gz
.\.venv\Scripts\python.exe scripts/multihost.py deploy --inventory configs/cloud-inventory.json --ssh-config configs/ssh-config --key $HOME\.ssh\uitiot-lab.pem --archive results/loadbalance-release.tar.gz
```

Lệnh `deploy` build image trên từng VM nên mất vài phút. Nếu lỗi vì không tải được gói Python, VM đó đang lỗi DNS: `ssh -F configs/ssh-config <IP> "sudo resolvectl dns ens3 1.1.1.1"` rồi chạy lại.

6. **Chạy lại riêng tình huống đó** (`--scenarios <tên>`) với cùng số lần lặp. So với kết quả trước khi sửa.
7. **Ghi cặp trước / sau** vào báo cáo, giữ cả hai thư mục kết quả. Nếu bản sửa tốt hơn, mở pull request vào `main`; nếu không, triển khai lại bản `main` để cụm trở về trạng thái chung.

Ô đã biết để luyện: sau khi gỡ phân mảnh node1, a-1 mất khoảng 2 phút mới phục vụ lại. Manh mối ở mục "Vấn đề đã biết" của [tham chiếu thí nghiệm](running.md#vấn-đề-đã-biết).

**Lưu ý lần triển khai đầu tiên:** cụm đang chạy bản ngày 28/10, chưa có phương án Least Request cho caller và việc gán lỗi theo endpoint. Một người cần triển khai bản `main` hiện tại (bước 5) trước khi cả nhóm bắt đầu đo.

## 6. Khi gặp lỗi công cụ, báo kèm

- Lệnh đã chạy, nguyên văn.
- Phần in ra cuối cùng (traceback hoặc thông báo lỗi).
- Đường dẫn thư mục kết quả nếu đã tạo.
- Kết quả của `labenv.py --env configs/lab.cloud.json check`.

## Phụ lục: thử code trên máy cá nhân

Khi muốn thử một thay đổi mà chưa muốn chiếm cụm, có thể dựng cùng 13 thành phần trên một máy Windows bằng Docker trong WSL: thay `--env configs/lab.cloud.json` bằng `--wsl Ubuntu-22.04` (chi tiết ở [tham chiếu thí nghiệm](running.md#phụ-lục-chạy-thử-trên-một-máy)). Số liệu từ cách này **không dùng cho báo cáo**.
