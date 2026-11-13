# Mục 6 — Lỗi mềm: RL

Người viết: Khoa. Việc cần làm: [phân công](../2-cong-viec/phan-cong.md#khoa). Thiết kế: [rl.md](../1-do-an/rl.md). Quy tắc ghi và viết: [README](README.md).

Số liệu đầu vào của mục này đến từ ba nguồn:

| Nguồn | Ai chạy | Lấy số ở |
|---|---|---|
| Mô phỏng (K2) | Khoa | Mục 1 bên dưới |
| gRPC thật, máy cục bộ (D2) | Đạt | `dat-loi-cung.md` mục 6 |
| Cụm ba VM (L3) | Lầu | `lau-loi-mem.md` mục 4 |

## Nhật ký lượt chạy

| Ngày | Mã | Lệnh (rút gọn) | Thư mục kết quả | Ghi chú |
|---|---|---|---|---|
| | K2 | | | |

## 1. Mô phỏng (K2)

Chạy lại một lần để chốt số (khoảng 35 phút, kết quả tất định):

```powershell
.\.venv\Scripts\python.exe scripts/train_shape_rl.py --output-dir results/shape-rl-final
```

- Thư mục kết quả:
- Model sinh ra có trùng `configs/shape-model.json` không (nếu không: triển khai model nào, vì sao):
- Seed huấn luyện, seed kiểm định, seed kiểm tra:

Dán bảng "Tập kiểm tra" từ `simulation.md`:

RL − β cố định tốt nhất trên tập kiểm tra:

## 2. Dự đoán cho D2 và L3 (commit trước khi có số)

| Kịch bản | Dự đoán RL so với Round Robin | Dự đoán RL so với β cố định | Vì sao |
|---|---|---|---|
| Cụm, `even` | | | |
| Cụm, `slow-node3` | | | |
| gRPC cục bộ, `even` | | | |
| gRPC cục bộ, `processing-slow` | | | |
| gRPC cục bộ, `uneven-capacity` | | | |
| gRPC cục bộ, `flapping` | | | |
| gRPC cục bộ, `load-step` | | | |

## 3. Kết quả và giải thích

### Trên cụm (từ L3)

| Kịch bản | RPS | Phần b-2: `xds` / `xds-beta` / `xds-rl` | P95: `xds` / `xds-beta` / `xds-rl` | Lỗi (%) | RL hơn β cố định ở mọi lần lặp? |
|---|---:|---|---|---|---|
| | | | | | |

So với Least Request, bằng mức cải thiện tương đối so với mốc cùng đường:

| Kịch bản | RPS | `static-lr` so với `static` | `xds-rl` so với `xds` |
|---|---:|---|---|
| | | | |

### Trên gRPC thật, máy cục bộ (từ D2)

| Kịch bản | RL − Round Robin | RL − β cố định tốt nhất | RL − Least Request | Hiệu âm ở mọi lần lặp? |
|---|---|---|---|---|
| | | | | |

### Dự đoán so với số đo

| Kịch bản | Dự đoán | Đo được | Lệch vì sao |
|---|---|---|---|
| | | | |

Giải thích: RL đổi trọng số thế nào (trích dòng `rl_window` trong log), vì sao P95 thay đổi hoặc không, mô phỏng có dự đoán đúng không.

## 4. An toàn

| Tình huống | Kỳ vọng | Bằng chứng (thư mục, dòng log `rl_fallback`) |
|---|---|---|
| Một instance B dừng | Quay về Round Robin | |
| Telemetry cũ hoặc thiếu mẫu | Quay về Round Robin | |
| Một control plane dừng | Bên còn lại vẫn điều khiển | |

## 5. Giới hạn của phép đo

## 6. Trả lời câu hỏi của nhánh

RL có né backend chậm tốt hơn Round Robin, Least Request và β cố định không; và có an toàn khi chính nó thiếu dữ liệu không?
