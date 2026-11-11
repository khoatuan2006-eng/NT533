# Phần 5 — Lỗi mềm trên cụm

Người viết: Lầu. Điền từng mục theo thứ tự; quy tắc viết ở [README](README.md) của thư mục này.

## 1. Dự đoán (viết và commit trước khi đo)

| Kịch bản | Phương án | Dự đoán phần tải vào b-2 | Dự đoán P99 | Dự đoán mức tải cao nhất còn đạt |
|---|---|---|---|---|
| `even` | `static` | | | |
| `even` | `static-lr` | | | |
| `even` | `xds` | | | |
| `slow-node3` | `static` | | | |
| `slow-node3` | `static-lr` | | | |
| `slow-node3` | `xds` | | | |

Thêm dự đoán cho `delay-node3` và `loss-node3` của bài lỗi: health probe có rút node 3 ra không, vì sao.

## 2. Kết quả trên baseline

- Thư mục kết quả:
- Giá trị `release`:
- Lệnh đã chạy:

Dán bảng từ `report.md` (median [min–max], đạt / không đạt):

## 3. Dự đoán so với số đo

| Ô (phương án, kịch bản, mức tải) | Dự đoán | Đo được | Lệch vì sao |
|---|---|---|---|
| | | | |

## 4. Giải thích từng ô

Mỗi ô một đoạn: chiến lược chia tải thế nào, vì sao phần b-2 và P99 ra như vậy, mức tải đang nhẹ hay gần bão hòa. Chỉ so `static-lr` với `static`; `xds` so với `static` chỉ nói về cách khám phá endpoint.

## 5. Cặp trước / sau

Với bài tải, ô không đạt thường là hệ thống đã bão hòa chứ không phải bug. Cặp trước / sau ở đây là **một thay đổi cấu hình đo được**, ví dụ bật Least Request ở Envoy (`--variants xds-lr-ingress`).

- Ô chọn để sửa, và vì sao chọn:
- Phân loại: lỗi hệ thống, lỗi công cụ đo, hay tiêu chí chưa hợp lý:
- Giả thuyết và bằng chứng (log, trace):
- Thay đổi đã làm (nhánh, commit):
- Kết quả trước (thư mục, số đo):
- Kết quả sau (thư mục, số đo):
- Kết luận. Nếu không sửa được: đã thử gì, vì sao dừng:

## 6. Giới hạn của phép đo

## 7. Trả lời câu hỏi của nhánh

Khi một backend chậm nhưng chưa chết, Least Request có né nó tốt hơn Round Robin không, và vì sao? Đường xDS trên cụm có cơ chế nào né backend chậm không?
