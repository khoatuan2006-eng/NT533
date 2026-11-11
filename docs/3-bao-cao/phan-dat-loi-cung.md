# Phần 4 — Lỗi cứng

Người viết: Đạt. Điền từng mục theo thứ tự; quy tắc viết ở [README](README.md) của thư mục này.

## 1. Dự đoán (viết và commit trước khi đo)

| Tình huống | Ai phát hiện | Theo cơ chế / timeout nào | Dự đoán gián đoạn | Request lỗi mang mã gì |
|---|---|---|---|---|
| `backend-stop` | | | | |
| `controlplane-stop` | | | | |
| `consul-leader` | | | | |
| `quorum-loss` | | | | |
| `node3-down` | | | | |
| `node1-down` | | | | |
| `partition-node3` | | | | |
| `partition-node1` | | | | |

## 2. Kết quả trên baseline

- Thư mục kết quả:
- Giá trị `release`:
- Lệnh đã chạy:

Dán bảng từ `report.md` (median [min–max], đạt / không đạt):

## 3. Dự đoán so với số đo

| Tình huống | Dự đoán | Đo được | Lệch vì sao |
|---|---|---|---|
| | | | |

## 4. Giải thích từng ô

Giải thích kỹ bốn tình huống `backend-stop`, `node1-down`, `quorum-loss`, `partition-node1`; bốn tình huống còn lại mỗi cái vài dòng. Mỗi tình huống: ai phát hiện lỗi, theo cơ chế nào, tải chuyển sang đâu, vì sao gián đoạn dài chừng đó.

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

Khi backend, node, control plane, Consul hoặc mạng gặp sự cố, hệ thống gián đoạn bao lâu và tự phục hồi thế nào?
