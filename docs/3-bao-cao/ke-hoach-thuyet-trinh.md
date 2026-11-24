# Kịch bản thuyết trình

Bộ slide: Canva, 21 trang (https://canva.link/ycu86bchk8avxo5). Mỗi người trình bày phần mình đã làm, nên khi bị hỏi cũng là người trả lời phần đó.

| Người | Phần | Slide | Thời gian |
|---|---|---|---|
| **Đạt** | Giới thiệu, kiến trúc, chịu lỗi | 1–9 | khoảng 5 phút |
| **Khoa** | Chia tải theo độ trễ, β và RL | 10–13 | khoảng 4 phút |
| **Lầu** | Thực nghiệm, kết quả, Grafana | 14–18 | khoảng 4 phút |
| Cả ba | Demo trực tiếp ([kịch bản demo](kich-ban-demo.md)) | 19 | khoảng 8 phút |
| **Khoa** | Kết luận, mời hỏi đáp | 20–21 | khoảng 1 phút |

Tổng khoảng 22 phút. Nếu bị giới hạn thời gian, rút demo trước: bỏ bước 4 (RL) và bước 6 (tắt b-3), còn khoảng 5 phút.

## Đạt: slide 1–9

| Slide | Ý chính phải nói |
|---|---|
| 1. Bìa | Chào thầy, giới thiệu nhóm và tên đề tài |
| 2. Nội dung | Đọc nhanh năm phần |
| 3. Ngăn cách 01 | Không dừng lâu |
| 4. Vấn đề bản sao chậm | b-2 chậm thêm 100 ms nhưng vẫn qua kiểm tra sức khỏe; Round Robin vẫn gửi 25% vào nó |
| 5. Mục tiêu | Ba mục tiêu: triển khai hai tầng, chia tải theo độ trễ, kiểm chứng RL |
| 6. Ngăn cách 02 | |
| 7. Kiến trúc hai tầng | Envoy chọn A; A tự chọn B theo trọng số control plane gửi qua xDS; vì sao dùng gRPC (chia theo từng request) |
| 8. Bốn máy ảo | Mỗi thành phần có bản sao trên node khác nhau |
| 9. Chịu lỗi | Consul 3 voter (Raft); mất quorum thì từ chối thay vì dùng dữ liệu cũ; 11.000/11.000 request thành công khi dừng leader |

Chuyển lời: *"Hệ thống đã chạy và chịu lỗi được. Câu hỏi còn lại là chia tải sao cho né được bản sao chậm. Phần này Khoa trình bày."*

## Khoa: slide 10–13

| Slide | Ý chính phải nói |
|---|---|
| 10. Ngăn cách 03 | |
| 11. Trọng số theo độ trễ | Giải thích công thức theo ba bước: chậm hơn bao nhiêu lần, nhân hàm mũ, chuẩn hóa với sàn 5%. β = 0 là Round Robin |
| 12. Bộ chọn β bằng RL | RL chỉ chọn β; 12 ngữ cảnh, 5 mức β; học trước trong mô phỏng, đóng băng khi chạy; vì sao là bandit |
| 13. Thuật toán đối chứng | Đối thủ quan trọng nhất là β = 2: cùng công thức, chỉ khác không học |

Chuyển lời: *"Để biết RL có giúp gì không, nhóm đo trên cụm thật. Lầu trình bày kết quả."*

## Lầu: slide 14–18

| Slide | Ý chính phải nói |
|---|---|
| 14. Ngăn cách 04 | |
| 15. Thiết kế thực nghiệm | 36 lượt: 4 thuật toán × 3 kịch bản × 3 lần; tải 200 → 800 RPC/s; chỉ số là phần tải vào bản sao chậm |
| 16. Kết quả | 25% → 8,6% (Least Request) → 5,5% (β = 2) → 5,4% (RL); hai bản chậm: 50% → 11,3% |
| 17. RL so với β cố định | Ngang nhau vì bản chậm khoảng 20 lần, mọi β > 0 đều chạm sàn; RL chỉ có lợi khi chậm nhẹ hoặc thay đổi |
| 18. Grafana | Tải thực tế đi theo trọng số control plane |

Chuyển lời: *"Bây giờ nhóm trình diễn trực tiếp trên cụm."*

## Demo: slide 19

| Vai | Người | Lý do |
|---|---|---|
| Người nói | **Lầu** | Đã chạy các lượt đo trên cụm, quen Grafana |
| Người gõ lệnh | **Khoa** | Đã triển khai cụm, xử lý được khi lệnh lỗi |
| Người chỉ màn hình | **Đạt** | Bước 6 (tắt b-3) là phần chịu lỗi, Đạt nói thêm một câu ở bước này |

## Khoa: slide 20–21

Đọc ba kết luận và hướng phát triển, cảm ơn thầy, mời hỏi đáp.

## Ai trả lời câu hỏi nào

| Chủ đề câu hỏi | Người trả lời |
|---|---|
| Kiến trúc, gRPC, Consul, Raft, xDS, chịu lỗi | Đạt |
| Công thức β, RL, bandit, phần thưởng, code bộ điều khiển | Khoa |
| Cách đo, số liệu, Grafana, vì sao RL ngang β = 2 | Lầu |

Người không phụ trách phần đó chỉ bổ sung khi người trả lời ra hiệu.
