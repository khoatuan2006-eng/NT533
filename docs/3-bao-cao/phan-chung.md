# Mục 1–3 và 7 — Phần chung

Mục 1–3 do Khoa viết. Mục 7 mỗi người viết một đoạn cho nhánh mình; Lầu ghép.

## 1. Giới thiệu và câu hỏi của đồ án

Đồ án đánh giá một hệ gRPC cân bằng tải hai tầng khi service, control plane, registry hoặc đường mạng gặp sự cố. Nhánh lỗi cứng đo thời gian gián đoạn, xác định thành phần phát hiện lỗi và quan sát cách hệ thống phục hồi. Nhánh lỗi mềm xét trường hợp backend vẫn trả lời nhưng chậm. Khi đó, nhóm đo xem Round Robin, Least Request và trọng số do RL chọn phân phối tải thế nào, đồng thời kiểm tra số request thành công và độ trễ.

Ba Consul voter minh họa đồng thuận và quorum. Hai control plane minh họa khám phá dịch vụ và phân phối cấu hình. Ở đường gọi, các bản sao cho phép chuyển request sang instance còn phục vụ và deadline giới hạn thời gian của toàn bộ chuỗi gọi. [Đề tài](../1-do-an/de-tai.md)

## 2. Kiến trúc

Ở tầng ngoài, Envoy kiểm tra sức khỏe và chọn một instance của A. Ở tầng trong, A chọn endpoint B và C rồi gọi trực tiếp từng endpoint. A truyền phần deadline còn lại cho các chặng phụ thuộc. Với đường xDS, hai control plane đọc registry Consul, tự probe sức khỏe backend và phát danh sách endpoint cho A. Nếu Consul mất quorum quá hạn lease 15 giây, đường xDS trả `UNAVAILABLE` để tránh dùng danh sách đã hết hạn.

| Node | Thành phần |
|---|---|
| node1 | Consul, control plane, Envoy, `a-1`, `b-1` |
| node2 | Consul, control plane, Envoy, `a-2`, `b-3`, `c-1` |
| node3 | Consul, `b-2`, `c-2` |

Consul giữ được quorum khi còn hai voter. Hai Envoy và hai control plane nằm ở hai node khác nhau. Để đo failover ở tầng ngoài, bộ phát tải cần một đường vào vẫn kết nối được tới Envoy còn sống khi một Envoy dừng. [Kiến trúc và mã nguồn](../1-do-an/kien-truc.md)

## 3. Phương pháp đo

Các lượt kiểm tra của Khoa dùng ba EC2 Ubuntu chạy Docker Compose, mỗi máy 2 vCPU và khoảng 2 GiB RAM. Bộ đo phát gRPC theo tốc độ cố định. Một lượt thử có thể đi qua tunnel SSH tới Envoy trên node1 hoặc phát ngay trên EC2. Phải ghi rõ đường đi khi so sánh độ trễ, vì đường truyền từ máy người chạy tới AWS có thể chi phối kết quả. Cổng vào qua một Envoy dùng được cho lỗi backend và cô lập node3. Bài mất Envoy cần kiểm tra bằng đường vào có thể chuyển sang Envoy còn lại.

- **Bài lỗi.** `run_faults.py` phát 20 request/giây, ghi từng mã gRPC và mốc gây lỗi, khôi phục. Thời gian gián đoạn tính từ request lỗi đầu tiên tới đầu cửa sổ 5 giây chỉ gồm request thành công trong thời gian giữ lỗi. Với tình huống cần tiếp tục phục vụ, mỗi lượt phải phục hồi và gián đoạn tối đa 30 giây. Kịch bản mất quorum được đánh giá bằng hành vi trả `UNAVAILABLE` và phục hồi sau khi gỡ lỗi. Thao tác “node chết” dừng toàn bộ container lab trên node đó.
- **Bài tải.** `run_load.py` đo goodput, tỷ lệ lỗi, P95/P99 và phần request B do `b-2` xử lý. Một ô đạt khi mọi lần lặp có lỗi tối đa 1%, goodput ít nhất 98% tải đặt, P99 thành công dưới 3 giây và bộ phát tải không bỏ request. Kết quả chính thức dùng median và khoảng [min–max] của 5 lần lặp cho bài lỗi hoặc 3 lần cho bài tải.
- **So chiến lược.** So `xds-rl` với `xds-beta` và `xds` trên cùng đường xDS. So Least Request với Round Robin trên đường static. Khi đối chiếu RL với Least Request, dùng mức cải thiện tương đối so với mốc Round Robin của mỗi đường. [Tiêu chí và quy tắc so sánh](../2-cong-viec/tra-cuu.md)

Mã `release` đọc từ cả ba EC2 trong lượt kiểm tra AWS là `12389101c90bbb83`. Bản đóng gói có 45 tệp runtime, cấu hình model và định nghĩa API đã đối chiếu với `origin/main` tại `201d8fd`. Tag `lab-baseline` trên origin trỏ tới commit này. Khi ghép số liệu AWS, đối chiếu mã `release` trong từng `run.json`.

## 7. Kết luận và hướng phát triển

### Lỗi cứng (Đạt)

### Lỗi mềm: Round Robin và Least Request (Lầu)

### Lỗi mềm: RL (Khoa)

Trong 600 kịch bản mô phỏng, model đưa lên cụm có điểm tổng hợp cao hơn β cố định tốt nhất 0,016 điểm. Lượt ngắn trên ba EC2 xác nhận hai control plane phát quyết định và xử lý được 1.500/1.500 RPC ở 50 RPS trong kịch bản đều. Cần D2 và L3 để biết lợi thế mô phỏng có lặp lại khi backend chậm trên gRPC thật hay không. [Phần RL](khoa-rl.md)

### Hướng phát triển

Nguồn: mục "Ngoài phạm vi" trong [de-tai.md](../1-do-an/de-tai.md) và mục "Giới hạn" của từng phần.
