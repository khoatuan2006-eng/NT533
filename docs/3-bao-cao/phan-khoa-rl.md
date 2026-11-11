# Phần 6 — Lỗi mềm: RL

Người viết: Khoa. Điền từng mục theo thứ tự; quy tắc viết ở [README](README.md) của thư mục này.

## 1. Dự đoán (viết và commit trước khi đo)

| Kịch bản | Dự đoán trọng số RL chọn | Dự đoán RL so với Round Robin | Dự đoán RL so với quy tắc |
|---|---|---|---|
| Hai backend bằng nhau | | | |
| Một backend chậm | | | |
| Đổi backend chậm giữa lượt | | | |
| Giới hạn xử lý mỗi backend | | | |

## 2. Kết quả trên baseline

- Thư mục kết quả:
- Giá trị `release`:
- Lệnh đã chạy:

Dán bảng từ `report.md` (median [min–max], đạt / không đạt):

## 3. Dự đoán so với số đo

| Kịch bản | Dự đoán | Đo được | Lệch vì sao |
|---|---|---|---|
| | | | |

## 4. Giải thích từng ô

Mỗi kịch bản một đoạn: RL đổi trọng số thế nào, hiệu RL − Round Robin và RL − quy tắc theo từng seed, khoảng hiệu có chứa 0 không. Thêm một đoạn về an toàn: RL có quay về Round Robin khi telemetry cũ, thiếu mẫu hoặc lỗi vượt 10% không.

## 5. Cặp trước / sau

- Ô chọn để sửa, và vì sao chọn:
- Phân loại: lỗi hệ thống, lỗi công cụ đo, hay tiêu chí chưa hợp lý:
- Giả thuyết và bằng chứng (log, trace):
- Thay đổi đã làm (nhánh, commit):
- Kết quả trước (thư mục, số đo):
- Kết quả sau (thư mục, số đo):
- Kết luận. Nếu không sửa được: đã thử gì, vì sao dừng:

## 6. Giới hạn của phép đo

## 7. Trả lời câu hỏi của nhánh

RL có né backend chậm tốt hơn Round Robin và quy tắc đơn giản không, và có an toàn khi chính nó lỗi hoặc thiếu dữ liệu không?
