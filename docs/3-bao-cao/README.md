# Báo cáo

Thư mục này là **nơi duy nhất để ghi số liệu và viết báo cáo**. Mỗi người điền file của mình; Lầu ghép thành bản nộp ngày 18/11/2026.

## Ai viết file nào

| File | Người viết | Thành mục nào của báo cáo |
|---|---|---|
| [phan-chung.md](phan-chung.md) | Khoa (mục 1–3), cả nhóm (mục 7) | 1–3 và 7 |
| [dat-loi-cung.md](dat-loi-cung.md) | Đạt | 4. Lỗi cứng |
| [lau-loi-mem.md](lau-loi-mem.md) | Lầu | 5. Lỗi mềm trên cụm |
| [khoa-rl.md](khoa-rl.md) | Khoa | 6. Lỗi mềm: RL |

Chỉ sửa file của mình, để không xung đột khi gộp nhánh. Số liệu của hai lượt chạy hộ phần RL (D2 của Đạt, L3 của Lầu) được ghi trong file của người chạy; Khoa đọc từ đó để viết mục 6.

## Dàn ý bản nộp

| Mục | Nội dung | Người viết |
|---|---|---|
| 1 | Giới thiệu và câu hỏi của đồ án | Khoa |
| 2 | Kiến trúc: hai tầng cân bằng tải, bố trí ba node | Khoa |
| 3 | Phương pháp đo: môi trường, công cụ, chỉ số, tiêu chí | Khoa |
| 4 | Lỗi cứng | Đạt |
| 5 | Lỗi mềm trên cụm: Round Robin và Least Request | Lầu |
| 6 | Lỗi mềm: RL | Khoa |
| 7 | Kết luận và hướng phát triển | Mỗi người một đoạn cho nhánh mình; Lầu ghép |

Định dạng bản nộp (Word, PDF hay LaTeX) do Lầu chọn khi ghép; nội dung viết ở đây trước.

## Hai câu báo cáo phải trả lời được

- **Lỗi cứng:** khi backend, node, control plane, Consul hoặc mạng gặp sự cố, hệ thống gián đoạn bao lâu và tự phục hồi thế nào?
- **Lỗi mềm:** khi một backend chậm nhưng chưa chết, chiến lược nào (Round Robin, Least Request, RL) né nó tốt hơn, và vì sao?

## Quy tắc ghi số liệu

1. **Mỗi lượt chạy ghi một dòng vào bảng "Nhật ký lượt chạy"** của file mình: ngày, lệnh, thư mục kết quả, giá trị `release`, ghi chú. Lượt hỏng hoặc bị ngắt cũng ghi.
2. **Dán nguyên bảng từ `report.md`**, không gõ lại số.
3. **Không dùng số của một lần chạy.** Ghi median kèm khoảng [min–max]: 5 lần lặp cho bài lỗi, 3 lần cho bài tải.
4. **Số từ bản dựng một máy không dùng cho báo cáo.**

## Quy tắc viết

1. **Kết luận đạt / không đạt theo tiêu chí** trong [tra cứu](../2-cong-viec/tra-cuu.md), kèm lời giải thích dựa trên kiến trúc: ai phát hiện, theo cơ chế nào, vì sao ra con số đó.
2. **Đặt số đo cạnh dự đoán.** Ô nào lệch thì giải thích vì sao lệch.
3. **Nhìn cả request lỗi:** goodput, tỷ lệ lỗi, P95/P99, thời gian gián đoạn. Không kết luận "tốt hơn" chỉ từ latency của request thành công.
4. **So sánh chiến lược theo [quy tắc so sánh](../2-cong-viec/tra-cuu.md#quy-tắc-so-sánh).**
5. **Trung thực về giới hạn:** VM dùng chung tài nguyên, đo qua VPN, "node chết" là dừng container chứ không tắt máy. Nếu RL không hơn β cố định thì ghi đúng như vậy.
