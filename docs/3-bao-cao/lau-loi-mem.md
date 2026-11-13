# Mục 5 — Lỗi mềm trên cụm

Người viết: Lầu. Việc cần làm: [phân công](../2-cong-viec/phan-cong.md#lầu--lỗi-mềm). Quy tắc ghi và viết: [README](README.md).

## Nhật ký lượt chạy

Mỗi lượt một dòng, kể cả lượt hỏng.

| Ngày | Mã | Lệnh (rút gọn) | Thư mục kết quả | `release` | Ghi chú |
|---|---|---|---|---|---|
| | L1 | | | | |
| | L2 | | | | |
| | L3 | | | | |

## 1. Dự đoán (commit trước khi đo)

Ở kịch bản `slow-node3` (node 3 thêm 50 ms):

| Phương án | Dự đoán phần b-2 (%) | Dự đoán P95 so với `even` | Dự đoán P99 so với `even` | Vì sao |
|---|---|---|---|---|
| `static` (Round Robin) | | | | |
| `static-lr` (Least Request) | | | | |
| `xds` (Round Robin) | | | | |
| `xds-beta` (β cố định) | | | | |
| `xds-rl` (RL) | | | | |

- Dự đoán điểm bão hòa (RPS) ở `even`:
- Khi node 3 thêm 100 ms độ trễ, health probe có rút b-2 và c-2 ra không? Vì sao:
- Khi node 3 rớt 20% gói thì sao:

## 2. Kết quả L1 — Round Robin và Least Request

- Thư mục kết quả:
- Giá trị `release`:
- Đã tự tính lại "phần b-2" của ô nào, có khớp bảng không:

Dán bảng từ `report.md`:

| Kịch bản | Phương án | RPS đặt | Goodput (RPS) | P95 OK (ms) | P99 OK (ms) | Lỗi (%) | Phần b-2 (%) | Đạt |
|---|---|---:|---:|---:|---:|---:|---:|---|
| | | | | | | | | |

Mức tải cao nhất còn đạt:

| Kịch bản | Phương án | RPS cao nhất đạt |
|---|---|---:|
| | | |

Nếu đã chạy thêm `--rates 600 800 1200`, ghi thư mục và dán bảng thứ hai ở đây.

## 3. Kết quả L2 — `delay-node3` và `loss-node3`

- Thư mục kết quả:
- Giá trị `release`:

| Tình huống | Lần lặp | Gián đoạn (s) | Lỗi trong lúc lỗi (%) | P95 OK (ms) | Phục hồi (s) | Instance còn phục vụ | Kết luận |
|---|---:|---:|---:|---:|---:|---|---|
| `delay-node3` | | | | | | | |
| `loss-node3` | | | | | | | |

## 4. Số liệu L3 — bài tải có RL (chạy hộ phần RL)

Chỉ ghi số liệu và quan sát; phần diễn giải RL do Khoa viết ở `khoa-rl.md`. Phần Least Request trong lượt này (`static-lr`) dùng được cho mục 5 bên dưới.

- Thư mục kết quả:
- Giá trị `release`:
- Đã dùng `--settle 90` chưa:

Dán bảng từ `report.md` (bốn phương án `xds`, `xds-beta`, `xds-rl`, `static-lr`):

| Kịch bản | Phương án | RPS đặt | Goodput (RPS) | P95 OK (ms) | P99 OK (ms) | Lỗi (%) | Phần b-2 (%) | Đạt |
|---|---|---:|---:|---:|---:|---:|---:|---|
| | | | | | | | | |

Điều thấy lạ khi chạy (lỗi, lượt bị ngắt, ô dao động mạnh):

## 5. Dự đoán so với số đo, và giải thích

| Ô | Dự đoán | Đo được | Lệch vì sao |
|---|---|---|---|
| | | | |

Các đoạn phải có:

- **Least Request có né backend chậm không:** `static-lr` so với `static` ở `slow-node3`, theo phần b-2, P95, P99 và goodput.
- **Điểm bão hòa:** ở mức tải nào, phương án nào chạm trước; kết quả né backend chậm ở trên đo ở mức tải nhẹ hay gần bão hòa.
- **Health probe với lỗi mềm:** từ L2, node 3 chậm hoặc rớt gói có bị rút ra không, và hệ quả với request.
- **`xds` so với `static`** (cùng Round Robin): chênh lệch do cách khám phá endpoint.

## 6. Cặp trước / sau

- Cấu hình hoặc ô chọn để đổi, và vì sao chọn:
- Giả thuyết:
- Thay đổi đã làm (tham số, hoặc nhánh và commit):
- Kết quả trước (thư mục, số đo):
- Kết quả sau (thư mục, `release`, số đo):
- Kết luận. Nếu không cải thiện: đã thử gì, vì sao dừng:

## 7. Giới hạn của phép đo

## 8. Trả lời câu hỏi của nhánh

Khi một backend chậm nhưng chưa chết, Round Robin hay Least Request né nó tốt hơn, và vì sao?
