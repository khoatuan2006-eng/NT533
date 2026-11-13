# Phân công

Trang này trả lời: **ai làm gì, theo thứ tự nào, hạn khi nào**. Tìm tên mình, làm từ trên xuống, đánh dấu `[x]` khi xong và commit file này để cả nhóm thấy tiến độ.

- Cách làm từng việc: [huong-dan-chay.md](huong-dan-chay.md).
- Ghi số liệu vào đâu: cột "Ghi vào" bên dưới, các file trong [docs/3-bao-cao/](../3-bao-cao/README.md).

## Tổng quan

| Người | Vai trò | Lượt chạy | File của mình |
|---|---|---|---|
| **Khoa** | Đưa bản hiện tại lên cụm; viết phần chung và phần RL | Không chạy lượt đo trên cụm | `khoa-rl.md`, `phan-chung.md` |
| **Đạt** | Nhánh lỗi cứng | D1 trên cụm, D2 trên máy mình | `dat-loi-cung.md` |
| **Lầu** | Nhánh lỗi mềm; ghép báo cáo | L1, L2, L3 trên cụm | `lau-loi-mem.md` |

## Các lượt chạy còn thiếu

| Mã | Người | Nội dung | Chạy ở | Thời lượng | Ghi vào |
|---|---|---|---|---|---|
| D1 | Đạt | Tám tình huống lỗi cứng, 5 lần lặp | Cụm | ≈ 1 giờ 40 phút | `dat-loi-cung.md` mục 2 |
| D2 | Đạt | RL trên gRPC thật, 3 backend, 3 lần lặp | Máy của Đạt | ≈ 1 giờ 45 phút | `dat-loi-cung.md` mục 6 |
| L1 | Lầu | Bài tải: Round Robin và Least Request, 4 mức tải, 3 lần lặp | Cụm | ≈ 75 phút | `lau-loi-mem.md` mục 2 |
| L2 | Lầu | Hai tình huống lỗi mềm (`delay-node3`, `loss-node3`), 5 lần lặp | Cụm | ≈ 25 phút | `lau-loi-mem.md` mục 3 |
| L3 | Lầu | Bài tải có RL và β cố định, 3 mức tải, 3 lần lặp | Cụm | ≈ 3 giờ | `lau-loi-mem.md` mục 4 |

Lệnh của từng lượt nằm ở [huong-dan-chay.md, mục 4](huong-dan-chay.md#4-các-lượt-chạy). D2 không dùng cụm, nên Đạt chạy được ngay cả khi Lầu đang giữ cụm; không chạy D2 cùng lúc với việc khác trên máy.

## Mốc thời gian (12–18/11/2026)

| Ngày | Việc | Xong khi |
|---|---|---|
| 12–13/11 | Khoa làm bước 0. Đạt và Lầu chuẩn bị máy, ghi dự đoán. Đạt chạy D2 | Có tag `lab-baseline` và giá trị `release`; dự đoán đã commit |
| 13/11 tối | Đạt chạy D1 | Có thư mục kết quả D1 |
| 14/11 | Lầu chạy L1, L2; tối chạy L3 | Có ba thư mục kết quả |
| 15–16/11 | Mỗi người kiểm tra số, viết đánh giá, sửa một ô | Có cặp trước / sau hoặc bản ghi "đã thử gì, vì sao dừng" |
| 17/11 | Hoàn thành file báo cáo của mình | Mọi mục đã điền |
| 18/11 | Lầu ghép, cả nhóm đọc lại | Báo cáo trả lời được câu hỏi ở cả hai nhánh |

Lịch dùng cụm ở trên là đề xuất; đổi được, miễn mỗi lúc chỉ một người chạy và báo trong nhóm.

## Khoa

### Bước 0 — cả nhóm chờ bước này để dùng cụm

- [ ] K0.1 Gộp nhánh `Khoa` vào `main`
- [ ] K0.2 Gửi file khóa SSH cho Đạt và Lầu
- [ ] K0.3 Triển khai bản `main` lên ba VM ([mục 6](huong-dan-chay.md#6-triển-khai-code-lên-cụm))
- [ ] K0.4 `check` báo `true` cả ba node, `status` đủ 14 thành phần `running`
- [ ] K0.5 Chạy hai bài ngắn ([mục 3](huong-dan-chay.md#3-chạy-thử)); lỗi công cụ thì sửa ngay
- [ ] K0.6 Chạy thử một ô với `xds-rl` để chắc phương án RL lên được trên cụm
- [ ] K0.7 Gắn tag `lab-baseline`, báo nhóm giá trị `release`

**Dấu hiệu Đạt và Lầu được dùng cụm:** Khoa báo trong nhóm, và `git fetch --tags` rồi `git tag -l lab-baseline` in ra tên tag.

### Sau bước 0

- [ ] K1 Hỗ trợ lỗi công cụ khi Đạt và Lầu chạy
- [ ] K2 Chạy lại huấn luyện mô phỏng một lần, xác nhận model trùng `configs/shape-model.json`, ghi số tập kiểm tra vào `khoa-rl.md` mục 1
- [ ] K3 Viết `phan-chung.md` mục 1–3 (giới thiệu, kiến trúc, phương pháp đo)
- [ ] K4 Viết `khoa-rl.md` từ số của D2 và L3: RL so với Round Robin, β cố định, Least Request
- [ ] K5 Kiểm tra an toàn: đọc `rl_fallback` trong log control plane của lượt D1 hoặc L3, ghi vào `khoa-rl.md` mục 4
- [ ] K6 Ngày 18/11: đọc bản ghép, sửa chỗ mô tả sai về hệ thống

## Đạt — lỗi cứng

Làm ngay, không chờ bước 0:

- [ ] Cài môi trường, `pytest` đạt ([mục 1](huong-dan-chay.md#1-chuẩn-bị-máy))
- [ ] Đọc [de-tai.md](../1-do-an/de-tai.md) và [kien-truc.md](../1-do-an/kien-truc.md)
- [ ] Ghi dự đoán cho tám tình huống vào `dat-loi-cung.md` mục 1, commit **trước khi đo**
- [ ] **D2**: chạy RL trên gRPC thật, điền `dat-loi-cung.md` mục 6

Sau khi có tag `lab-baseline`:

- [ ] Đặt file khóa, bật VPN, `check` báo `true` cả ba node
- [ ] Thử tay: dừng b-2, cô lập node 3, đọc log control plane ([mục 2](huong-dan-chay.md#2-thử-tay-để-hiểu-hệ-thống))
- [ ] **D1**: chạy tám tình huống lỗi cứng, điền `dat-loi-cung.md` mục 2
- [ ] Tự đếm lại một lần lặp từ `*.jsonl` và `*.events.json` ([mục 5](huong-dan-chay.md#5-kiểm-tra-và-đánh-giá))
- [ ] Viết đánh giá từng tình huống cạnh dự đoán (mục 3). Giải thích kỹ `backend-stop`, `node1-down`, `quorum-loss`, `partition-node1`; bốn tình huống còn lại mỗi cái vài dòng
- [ ] Xem kỹ hai ô đáng ngờ: `consul-leader` (có hội tụ trong lúc lỗi không) và `partition-node1` (phục hồi sau khi gỡ mất bao lâu)
- [ ] Chọn một ô không đạt, chẩn đoán và sửa trong giới hạn một buổi; điền mục 4
- [ ] Hoàn thành mục 5 và 7

## Lầu — lỗi mềm

Làm ngay, không chờ bước 0:

- [ ] Cài môi trường, `pytest` đạt ([mục 1](huong-dan-chay.md#1-chuẩn-bị-máy))
- [ ] Đọc [de-tai.md](../1-do-an/de-tai.md), [kien-truc.md](../1-do-an/kien-truc.md) và [rl.md](../1-do-an/rl.md)
- [ ] Ghi dự đoán vào `lau-loi-mem.md` mục 1, commit **trước khi đo**

Sau khi có tag `lab-baseline`:

- [ ] Đặt file khóa, bật VPN, `check` báo `true` cả ba node
- [ ] Thử tay: `degrade node3 --delay-ms 100`, quan sát, rồi `heal` ([mục 2](huong-dan-chay.md#2-thử-tay-để-hiểu-hệ-thống))
- [ ] **L1**: chạy bài tải, điền `lau-loi-mem.md` mục 2. Nếu mọi mức đều đạt, chạy thêm `--rates 600 800 1200` để tìm điểm bão hòa
- [ ] **L2**: chạy hai tình huống lỗi mềm, điền mục 3
- [ ] **L3**: chạy bài tải có RL, điền mục 4; báo Khoa thư mục kết quả
- [ ] Tự tính lại "phần b-2" của một ô từ `*.jsonl` ([mục 5](huong-dan-chay.md#5-kiểm-tra-và-đánh-giá))
- [ ] Viết đánh giá (mục 5): `static-lr` so với `static` ở `slow-node3`; health probe có rút node 3 ra khi nó chậm không
- [ ] Cặp trước / sau (mục 6): đổi một cấu hình rồi đo lại, ví dụ thêm `--variants xds-lr-ingress`; trong giới hạn một buổi
- [ ] Hoàn thành mục 7 và 8
- [ ] Ngày 18/11: ghép `phan-chung.md` và ba phần thành bản nộp
