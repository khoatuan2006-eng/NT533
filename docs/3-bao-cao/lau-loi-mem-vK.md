# Mục 5 — Lỗi mềm trên cụm (vK)

Bản vK do Khoa chạy trên ba EC2 mới và ghi số liệu độc lập. Bản gốc của Lầu nằm tại [lau-loi-mem.md](lau-loi-mem.md).

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
| `static` (Round Robin) | Khoảng 33% | Tăng | Tăng | Lượt gọi vẫn xoay đều khi b-2 còn khỏe |
| `static-lr` (Least Request) | Dưới 33% khi đủ tải | Tăng ít hơn Round Robin | Vẫn tăng | Request trên b-2 tồn đọng lâu hơn |
| `xds` (Round Robin) | Khoảng 33% | Tăng | Tăng | Health probe có thể vẫn giữ backend chậm trong tập endpoint |
| `xds-beta` (β cố định) | Dưới 33% | Có thể giảm so với xds | Vẫn tăng | Trọng số phản ứng theo latency và luôn giữ một phần tải cho b-2 |
| `xds-rl` (RL) | Dưới 33% | Có thể giảm so với xds | Vẫn tăng | RL chọn β theo cửa sổ telemetry; phần tải còn lại đi qua backend chậm |

- Dự đoán ở `even`: 50 và 100 RPS còn dư; mức 400 RPS có khả năng chạm giới hạn trước trên t3.small.
- Với 100 ms trễ thêm, dự đoán probe vẫn giữ b-2 và c-2 nếu chúng đáp trong timeout, còn độ trễ RPC tăng.
- Với rớt gói 20%, retransmission có thể làm P99 và số deadline tăng; endpoint có thể bị rút tạm nếu probe thất bại liên tiếp.

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
