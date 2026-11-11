# Việc của tôi

Trang này trả lời: **ai làm gì, theo thứ tự nào, hạn khi nào**. Tìm tên mình, làm từ trên xuống. Đánh dấu `[x]` khi xong và commit file này để cả nhóm thấy tiến độ.

- Cách làm từng việc: [2-huong-dan.md](2-huong-dan.md). "Mục N" bên dưới là mục trong file đó; chỉ mở khi làm tới.
- Viết kết quả vào đâu: [thư mục báo cáo](../3-bao-cao/README.md), mỗi người một file.

## Phân công

| Người | Nhánh | Phần đứng tên | Lệnh chính | File báo cáo |
|---|---|---|---|---|
| Đạt | Lỗi cứng: thành phần chết, mạng phân mảnh | Tám tình huống lỗi cứng: gián đoạn, phục hồi, fail closed | `run_faults.py` | `phan-dat-loi-cung.md` |
| Lầu | Lỗi mềm: backend chậm, trên cụm | Round Robin so với Least Request; điểm bão hòa; `delay-node3`, `loss-node3`; mục 1–3 của báo cáo và ghép báo cáo | `run_load.py`, `run_faults.py` | `phan-lau-loi-mem.md` |
| Khoa | Lỗi mềm (mở rộng), lab cục bộ | Bước 0; RL so với Round Robin và quy tắc | `compare_rl_scenarios.py` | `phan-khoa-rl.md` |

Khoa đã xây phần lớn hệ thống và công cụ, nên trong tuần này chỉ làm bước 0 và phần RL; phần chung của báo cáo và việc ghép do Lầu làm.

## Mốc thời gian (11–18/11/2026)

| Ngày | Chặng | Xong khi |
|---|---|---|
| 11–12/11 | Bước 0 (Khoa); Lầu và Đạt chuẩn bị máy, ghi dự đoán | Khoa báo nhóm "cụm sẵn sàng", có tag `lab-baseline` |
| 13–14/11 | Đo bảng "trước" trên baseline | Mỗi phần có một thư mục kết quả đủ số lần lặp |
| 15–16/11 | Kiểm tra, đánh giá, chẩn đoán và sửa một ô | Có cặp trước / sau, hoặc bản ghi "đã thử gì, vì sao dừng" |
| 17/11 | Hoàn thành file báo cáo của mình | Mọi mục trong file đã điền |
| 18/11 | Lầu ghép báo cáo, cả nhóm đọc lại | Báo cáo trả lời được câu hỏi chính ở cả hai nhánh |

## Bước 0 — Khoa (cả nhóm chờ bước này để dùng cụm)

- [x] Push nhánh `Khoa`
- [ ] Gộp `Khoa` vào `main`
- [ ] Gửi file khóa SSH cho Lầu và Đạt
- [ ] Triển khai bản `main` lên cụm (mục 5, bước 5)
- [ ] Chạy `check`, `status` và hai bài ngắn (mục 4); lỗi công cụ thì báo theo mục 6
- [ ] Gắn tag `lab-baseline`, báo nhóm giá trị `release` (mục 4, Baseline chung)

**Dấu hiệu Lầu và Đạt được dùng cụm:** Khoa báo trong nhóm, và `git fetch --tags` rồi `git tag -l lab-baseline` in ra tên tag.

## Việc không cần cụm — Lầu và Đạt làm ngay, không chờ bước 0

- [ ] `git pull` nhánh `main`, cài môi trường, `pytest` đạt (mục 1)
- [ ] Đọc "Câu hỏi của đồ án" và "Kiến trúc" trong README ở thư mục gốc
- [ ] Đọc bảng tình huống và tiêu chí của phần mình trong `3-tra-cuu.md`
- [ ] Ghi dự đoán vào file báo cáo của mình và commit (mục 2)

## Đạt — lỗi cứng

- [ ] Đặt file khóa, bật VPN, `check` báo `true` cả ba node (mục 1)
- [ ] Thử tay: dừng b-2, cô lập node 3, đọc log control plane (mục 2)
- [ ] Hẹn giờ dùng cụm, chạy tám tình huống lỗi cứng trên baseline: `run_faults.py --repetitions 5 --scenarios backend-stop controlplane-stop consul-leader quorum-loss node3-down node1-down partition-node3 partition-node1` (mục 3, 4)
- [ ] Tự đếm lại một lần lặp từ `*.jsonl` và `*.events.json` (mục 4, Kiểm tra)
- [ ] Viết đánh giá, đặt cạnh dự đoán (mục 4, Đánh giá): giải thích kỹ bốn tình huống `backend-stop`, `node1-down`, `quorum-loss`, `partition-node1`; bốn tình huống còn lại mỗi cái vài dòng
- [ ] Xem trong kết quả trên: lỗi phục hồi chậm sau `partition-node1` có tái hiện trên cụm không
- [ ] Chẩn đoán và sửa một ô không đạt, trong giới hạn một buổi (mục 5)
- [ ] Chạy một bài tải ngắn của phần Lầu để hiểu bảng của bạn ấy

## Lầu — lỗi mềm trên cụm

- [ ] Đặt file khóa, bật VPN, `check` báo `true` cả ba node (mục 1)
- [ ] Thử tay: `degrade node3 --delay-ms 100`, quan sát rồi `heal` (mục 2)
- [ ] Hẹn giờ dùng cụm, chạy `run_load.py --rates 50 100 200 400 --repetitions 3` trên baseline (mục 3, 4)
- [ ] Nếu mọi mức đều đạt, chạy thêm `--rates 600 800 1200` để tìm điểm bão hòa
- [ ] Tự tính lại "phần b-2" của một ô từ `*.jsonl` (mục 4, Kiểm tra)
- [ ] Chạy hai tình huống lỗi mềm của bài lỗi: `run_faults.py --repetitions 5 --scenarios delay-node3 loss-node3`
- [ ] Viết đánh giá: `static-lr` so với `static` ở `slow-node3`; health probe có rút node 3 ra khi nó chậm không (mục 4, Đánh giá)
- [ ] Cặp trước / sau: đổi một cấu hình rồi đo lại, ví dụ thêm `--variants xds-lr-ingress` để bật Least Request ở Envoy; trong giới hạn một buổi
- [ ] Chạy một bài lỗi ngắn của phần Đạt để hiểu bảng của bạn ấy
- [ ] Viết mục 1–3 của báo cáo (giới thiệu, kiến trúc, phương pháp đo), lấy từ README và `3-tra-cuu.md`
- [ ] Ngày 18/11: ghép ba phần và mục 7 thành bản nộp

## Khoa — bước 0 và RL

- [ ] Xong bước 0
- [ ] Ghi dự đoán cho bốn kịch bản RL vào file báo cáo và commit (mục 2)
- [ ] Chạy `compare_rl_scenarios.py` năm seed trên máy cục bộ (mục 4)
- [ ] Viết đánh giá: RL so với Round Robin và quy tắc; ghi đúng nếu RL không hơn quy tắc
- [ ] Kiểm tra RL quay về Round Robin khi telemetry cũ hoặc thiếu mẫu
- [ ] Ngày 18/11: đọc lại bản ghép, sửa chỗ mô tả sai về hệ thống
