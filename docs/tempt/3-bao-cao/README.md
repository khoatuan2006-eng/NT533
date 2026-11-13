# Báo cáo

Thư mục này là **nơi duy nhất để viết báo cáo**. Mỗi người điền một file; Lầu ghép thành bản nộp ngày 18/11.

## Ai viết file nào

| File | Người viết | Thành mục nào của báo cáo |
|---|---|---|
| [phan-dat-loi-cung.md](phan-dat-loi-cung.md) | Đạt | 4. Lỗi cứng |
| [phan-lau-loi-mem.md](phan-lau-loi-mem.md) | Lầu | 5. Lỗi mềm trên cụm |
| [phan-khoa-rl.md](phan-khoa-rl.md) | Khoa | 6. Lỗi mềm: RL |
| [phan-chung.md](phan-chung.md) | Lầu | 1–3 và 7 |

Chỉ sửa file của mình, để không xung đột khi gộp nhánh. Mỗi file đã có sẵn các mục cần điền, theo đúng thứ tự làm việc: dự đoán, kết quả, giải thích, cặp trước / sau, giới hạn.

## Dàn ý báo cáo

| Mục | Nội dung | Người viết | Lấy từ đâu |
|---|---|---|---|
| 1 | Giới thiệu và câu hỏi của đồ án (hai nhánh lỗi cứng, lỗi mềm) | Lầu | README ở thư mục gốc |
| 2 | Kiến trúc: hai tầng cân bằng tải, bố trí ba node | Lầu | README, `docs/1-do-an/` |
| 3 | Phương pháp đo: môi trường, công cụ, chỉ số, tiêu chí, giới hạn chung | Lầu | `docs/2-cong-viec/3-tra-cuu.md` |
| 4 | Lỗi cứng | Đạt | `phan-dat-loi-cung.md` |
| 5 | Lỗi mềm trên cụm | Lầu | `phan-lau-loi-mem.md` |
| 6 | Lỗi mềm: RL | Khoa | `phan-khoa-rl.md` |
| 7 | Kết luận: trả lời câu hỏi chính ở từng nhánh; hướng phát triển | Mỗi người viết câu trả lời của nhánh mình; Lầu ghép | Mục 7 của ba file; "Ngoài phạm vi" trong README |

Mục 1–3 phần lớn lấy lại từ README và `3-tra-cuu.md`, Lầu viết vào file `phan-chung.md` trong thư mục này. Khoa đọc lại bản ghép để sửa chỗ mô tả sai về hệ thống. Định dạng bản nộp (Word, PDF hay LaTeX) do Lầu chọn khi ghép; nội dung viết ở đây trước.

## Hai câu báo cáo phải trả lời được

- **Lỗi cứng:** khi backend, node, control plane, Consul hoặc mạng gặp sự cố, hệ thống gián đoạn bao lâu và tự phục hồi thế nào?
- **Lỗi mềm:** khi một backend chậm nhưng chưa chết, chiến lược nào (Round Robin, Least Request, RL) né nó tốt hơn, và vì sao?

## Quy tắc viết

1. **Không dùng số liệu một lần chạy.** Ghi median kèm khoảng [min–max]: 5 lần lặp cho bài lỗi, 3 lần cho bài tải, 5 seed cho RL.
2. **Mỗi con số đi kèm thư mục kết quả** và giá trị `release` (bản code chạy trên cụm lúc đo).
3. **Kết luận đạt / không đạt theo tiêu chí** trong `docs/2-cong-viec/3-tra-cuu.md`, kèm lời giải thích dựa trên kiến trúc: ai phát hiện, theo cơ chế nào, vì sao ra con số đó.
4. **Đặt số đo cạnh dự đoán.** Ô nào lệch thì giải thích vì sao lệch.
5. **Chỉ số phải nhìn cả request lỗi:** goodput, tỷ lệ lỗi và timeout, P50/P95/P99, thời gian gián đoạn và phục hồi. Không kết luận "tốt hơn" chỉ từ latency của request thành công.
6. **So sánh chiến lược trong cùng một đường dữ liệu.** Least Request so với Round Robin static trên cụm; RL so với Round Robin trong cùng lượt ở lab cục bộ. Không xếp hạng Least Request với RL bằng số tuyệt đối.
7. **Trung thực về giới hạn:** VM dùng chung tài nguyên, đo qua VPN, "node chết" là dừng container chứ không tắt máy, RL đo trên Windows cục bộ. Nếu RL không hơn quy tắc đơn giản thì ghi đúng như vậy.
8. **Số liệu từ bản dựng một máy không dùng cho báo cáo.**
