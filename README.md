# grpc-lb

Hệ thống cân bằng tải hai tầng cho các service gRPC, kèm bộ công cụ gây lỗi và đo đạc trên cụm ba node.

- **Tầng ngoài:** Envoy chia request vào service điều phối A.
- **Tầng trong:** A tự chọn instance của service xử lý B, C (mô hình lookaside); danh sách instance do control plane phát qua gRPC xDS, lấy từ Consul.
- **Chiến lược:** Round Robin, Least Request, và trọng số theo latency (β cố định hoặc RL chọn β).

![Kiến trúc logic: client gọi hai Envoy, Envoy chia tải vào service A, A gọi trực tiếp service B và C; Consul và control plane phát endpoint cho A qua xDS](docs/images/architecture-logical.svg)

## Cấu trúc repo

| Thư mục | Nội dung |
|---|---|
| `src/grpc_lb/` | Service A/B/C: xử lý request, deadline, client phụ thuộc, đăng ký Consul |
| `src/grpc_lb/controlplane/` | Control plane xDS: đọc Consul, probe health, phát endpoint và trọng số |
| `api/` | Định nghĩa protobuf |
| `scripts/` | Điều khiển cụm, gây lỗi, phát tải, triển khai, huấn luyện và so sánh RL |
| `configs/` | Sơ đồ service, mô tả cụm, cấu hình SSH, model RL |
| `deploy/` | Dockerfile, Compose và cấu hình Envoy |
| `tests/` | Kiểm thử |
| `results/` | Kết quả các lượt chạy; mỗi lượt một thư mục |
| `docs/` | Tài liệu |

## Yêu cầu

- Python 3.11 trở lên, Windows với PowerShell.
- Để chạy trên cụm: VPN UIT-IoT và file khóa SSH của nhóm.
- Để dựng bản một máy: Docker trong WSL.

## Cài đặt

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Kiểm tra

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/check_quality.py
```

`check_quality.py` chạy lint, kiểm tra định dạng và toàn bộ test.

## Chạy

Cụm gồm ba VM, mỗi VM chạy phần của mình bằng Docker Compose. Script chạy trên máy cá nhân và điều khiển cụm qua SSH.

| Việc | Lệnh |
|---|---|
| Kiểm tra kết nối tới cụm | `scripts/labenv.py --env configs/lab.cloud.json check` |
| Xem trạng thái các thành phần | `scripts/labenv.py --env configs/lab.cloud.json status` |
| Chạy tình huống lỗi | `scripts/run_faults.py --env configs/lab.cloud.json` |
| Quét mức tải theo chiến lược | `scripts/run_load.py --env configs/lab.cloud.json` |
| Triển khai code lên cụm | `scripts/multihost.py render`, `bundle`, `deploy` |
| So RL trên máy cục bộ | `scripts/compare_shape_rl.py --model configs/shape-model.json --output-dir <thư-mục>` |

Mọi lệnh gọi bằng `.\.venv\Scripts\python.exe`. Tham số đầy đủ và thứ tự chạy: [hướng dẫn chạy](docs/2-cong-viec/huong-dan-chay.md).

## Tài liệu

| Bạn cần | Mở |
|---|---|
| Đề tài: câu hỏi, phạm vi, tình trạng | [docs/1-do-an/de-tai.md](docs/1-do-an/de-tai.md) |
| Kiến trúc và mã nguồn | [docs/1-do-an/kien-truc.md](docs/1-do-an/kien-truc.md) |
| Phần RL | [docs/1-do-an/rl.md](docs/1-do-an/rl.md) |
| Phân công và hạn | [docs/2-cong-viec/phan-cong.md](docs/2-cong-viec/phan-cong.md) |
| Chạy từng bước | [docs/2-cong-viec/huong-dan-chay.md](docs/2-cong-viec/huong-dan-chay.md) |
| Tình huống, phương án, tiêu chí, sự cố | [docs/2-cong-viec/tra-cuu.md](docs/2-cong-viec/tra-cuu.md) |
| Ghi số liệu và viết báo cáo | [docs/3-bao-cao/](docs/3-bao-cao/README.md) |

`docs/tempt/` chứa tài liệu của các giai đoạn trước, không dùng để làm việc.
