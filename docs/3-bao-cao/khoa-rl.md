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
| 18/11/2026 | K2 | `train_shape_rl.py --output-dir results/shape-rl-final` | [shape-rl-final](../../results/shape-rl-final/simulation.md) | 5 seed, chọn seed 9, model trùng bản đang dùng |
| 18/11/2026 | K0.5 | `run_faults.py --env configs/lab.aws.json --scenarios backend-stop partition-node3 --repetitions 1` | [aws-k0-20261118](../../results/lab-faults/aws-k0-20261118/report.md) | Release `12389101c90bbb83`, smoke xDS qua tunnel SSH tới Envoy node1 |
| 18/11/2026 | K0.6 | `run_load.py --env configs/lab.aws.json --variants xds-rl --scenarios even --rates 50 --repetitions 1 --settle 90` | [aws-k0-rl-20261118](../../results/lab-load/aws-k0-rl-20261118/report.md) | Release `12389101c90bbb83`, 1.500/1.500 OK |
| 18/11/2026 | K5 bổ sung | `run_faults.py --env configs/lab.aws.json --scenarios backend-stop controlplane-stop --variants xds-rl --repetitions 1 --rate 20 --before 15 --hold 30 --after 30` | [aws-k5-rl-20261118](../../results/lab-faults/aws-k5-rl-20261118/report.md) | Release `12389101c90bbb83`, 1 lần lặp để đọc log |

## 1. Mô phỏng (K2)

Chạy lại một lần để chốt số (khoảng 35 phút, kết quả tất định):

```powershell
.\.venv\Scripts\python.exe scripts/train_shape_rl.py --output-dir results/shape-rl-final
```

- Huấn luyện 20.000 kịch bản cho mỗi seed trong 7, 8, 9, 10, 11. Dùng seed 1007 để kiểm định và chọn seed huấn luyện 9. Tập kiểm tra gồm 600 kịch bản độc lập với seed 5051 và 2, 3, 4, 6, 8, 10 instance.
- `results/shape-rl-final/model.json` trùng từng byte với `configs/shape-model.json`. Model trên cả ba EC2 có cùng SHA-256 `e66363ddad91d71871d592f65e40f42aabe0ecd0e51379fff61118330cfd651e`. [Bảng tập kiểm tra](../../results/shape-rl-final/simulation.md#tập-kiểm-tra-số-dùng-cho-báo-cáo)

| Chính sách | even | path-slow | processing-slow | uneven-capacity | flapping | load-step | Tất cả |
|---|---:|---:|---:|---:|---:|---:|---:|
| beta=0 | -0.001 | 0.000 | 0.004 | 0.001 | -0.004 | -0.007 | -0.001 |
| beta=0.5 | -0.103 | 0.143 | 0.439 | 0.565 | 0.125 | 0.178 | 0.228 |
| beta=1 | -0.107 | 0.083 | 0.474 | 0.608 | 0.142 | 0.191 | 0.236 |
| beta=2 | -0.143 | 0.086 | 0.533 | 0.666 | 0.140 | 0.176 | 0.248 |
| beta=4 (β cố định tốt nhất) | -0.169 | 0.104 | 0.568 | 0.710 | 0.137 | 0.205 | 0.264 |
| rl | -0.088 | 0.116 | 0.555 | 0.697 | 0.164 | 0.216 | 0.281 |
| oracle (cận trên: biết loại kịch bản, chọn β tốt nhất của nó) | -0.001 | 0.143 | 0.568 | 0.710 | 0.142 | 0.205 | 0.297 |

Chênh lệch RL so với β cố định tốt nhất là **+0,016** tính theo điểm chưa làm tròn. Điểm tính mức cải thiện độ trễ so với Round Robin và trừ thêm khi có lỗi. Lợi thế trung bình nhỏ và phụ thuộc kịch bản. Ở `processing-slow` và `uneven-capacity`, β=4 có điểm cao hơn RL.

## 2. Dự đoán cho D2 và L3 (commit trước khi có số)

| Kịch bản | Dự đoán RL so với Round Robin | Dự đoán RL so với β cố định | Vì sao |
|---|---|---|---|
| Cụm, `even` | Xấp xỉ Round Robin, sai số nhỏ có thể làm P95 tăng | Xấp xỉ hoặc ít lệch tải hơn | Backend tương đương, bộ lọc nhiễu cần giữ trọng số gần đều |
| Cụm, `slow-node3` | Phần `b-2` giảm, P95 có thể vẫn cao | Phần `b-2` có thể tương đương | Cả hai dùng cùng công thức trọng số và vẫn giữ một phần tải ở backend chậm |
| gRPC cục bộ, `even` | Xấp xỉ Round Robin | Ít nguy cơ lệch tải do nhiễu | Chia đều phù hợp khi năng lực backend giống nhau |
| gRPC cục bộ, `processing-slow` | Giảm tải lên backend xử lý chậm | Có thể gần β cố định tốt nhất | Chênh lệch ổn định giúp nhiều β lớn chọn gần giống nhau |
| gRPC cục bộ, `uneven-capacity` | Có thể giảm hàng đợi tại backend yếu hơn | Phụ thuộc mức tải | Tín hiệu hàng đợi giúp điều chỉnh độ mạnh khi nhu cầu đổi |
| gRPC cục bộ, `flapping` | Lợi ích có thể giảm nếu chậm thay đổi nhanh | Chưa rõ chiều | Thời gian giữ β và giới hạn đổi trọng số có thể làm phản ứng chậm |
| gRPC cục bộ, `load-step` | Có thể tốt hơn sau khi ổn định | Chưa rõ trong pha chuyển tiếp | Telemetry cần đủ cửa sổ sau bước đổi tải |

## 3. Kết quả và giải thích

### Trên cụm (từ L3)

Chưa có bảng L3 của Lầu. Bảng này cần kết quả của các phương án ở cùng mức tải và cùng `release`, với ba lần lặp cho mỗi ô.

Ô kiểm tra K0.6 trên AWS đạt 50 goodput RPS, 0% lỗi và phần `b-2` khoảng 32%. Trong một cửa sổ trên node1, controller chọn β=4 và phát trọng số 34/33/33 khi ba backend có latency gần nhau. Một ô `even` xác nhận RL chạy được và không đủ để kết luận về khả năng né backend chậm. [Báo cáo K0.6](../../results/lab-load/aws-k0-rl-20261118/report.md)

| Kịch bản | RPS | Phần b-2: `xds` / `xds-beta` / `xds-rl` | P95: `xds` / `xds-beta` / `xds-rl` | Lỗi (%) | RL hơn β cố định ở mọi lần lặp? |
|---|---:|---|---|---|---|
| | | | | | |

So với Least Request, bằng mức cải thiện tương đối so với mốc cùng đường:

| Kịch bản | RPS | `static-lr` so với `static` | `xds-rl` so với `xds` |
|---|---:|---|---|
| | | | |

### Trên gRPC thật, máy cục bộ (từ D2)

Chưa có báo cáo D2 của Đạt. Lượt thử cũ trong `results/shape-compare` thiếu báo cáo theo lần lặp nên chưa đủ làm số liệu D2.

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
| Một instance B dừng | Quay về Round Robin | Cả hai control plane ghi `telemetry does not match membership` lúc 03:38:40 UTC sau khi phát lệnh dừng `b-2`. Lượt ghi 1.500/1.500 OK. |
| Telemetry cũ hoặc thiếu mẫu | Quay về Round Robin | Log ghi `invalid endpoint latency samples` lúc 03:39:12 UTC trên cả hai control plane. Dòng này xác nhận rào về mẫu không hợp lệ. |
| Một control plane dừng | Bên còn lại vẫn điều khiển | Trong lúc control plane 1 dừng, control plane 2 ghi ba `rl_window` lúc 03:40:34, 03:40:44 và 03:40:54 UTC. Gián đoạn 8,75 s, với 175 deadline trong lượt. |

Các dòng trên lấy từ lượt AWS bổ sung. Kết quả control plane 2 tiếp tục ra quyết định đi kèm lỗi request trong thời gian chuyển tiếp. Cần đối chiếu lại với log D1 hoặc L3 khi có. [Log node1](../../results/lab-faults/aws-k5-rl-20261118/node1-rl.log) · [Log node2](../../results/lab-faults/aws-k5-rl-20261118/node2-rl.log)

## 5. Giới hạn của phép đo

Mô phỏng xấp xỉ backend bằng hàng đợi và dùng các kịch bản được sinh từ seed cố định. Trên AWS, phép đo qua tunnel SSH chứa độ trễ từ máy phát tải tới Sydney. Nó phù hợp để xác nhận một đường vào còn phục vụ trong bài ngắn. So sánh chiến lược cần các lượt cùng đường vào, cùng tải, cùng cấu hình và đủ lần lặp. Kết quả qua Envoy node1 kiểm tra các chặng A → B/C và đường điều khiển, còn failover giữa hai Envoy cần một đường vào có thể chuyển sang Envoy node2.

## 6. Trả lời câu hỏi của nhánh

RL có né backend chậm tốt hơn Round Robin, Least Request và β cố định không; và có an toàn khi chính nó thiếu dữ liệu không?

Trong mô phỏng với model đang triển khai, RL nhỉnh hơn β cố định tốt nhất trên toàn bộ tập kiểm tra, nhưng điểm thấp hơn Round Robin ở kịch bản `even`. Hai control plane kích hoạt fallback khi thành viên B thay đổi hoặc mẫu latency không hợp lệ. Lượt dừng một control plane cho thấy bên còn lại tiếp tục ra quyết định, cùng lúc một phần RPC chạm deadline trong pha chuyển tiếp. Các kết luận về khả năng né backend chậm trên gRPC thật cần D2 và L3.
