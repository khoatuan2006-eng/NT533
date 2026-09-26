# Hướng đi đồ án: Cân bằng tải gRPC phía client với RL và Digital Twin

**Kế hoạch đề xuất:** 3 thành viên, 30 ngày. Sử dụng Python và grpc.aio; một tiến trình client tạo nhiều RPC đồng thời tới ba replica. Bộ chọn replica nằm trong ứng dụng client.

README mô tả định hướng và kiến trúc chung. Công việc, phân công và điều kiện hoàn thành mốc 1 nằm trong [M1.md](M1.md).

## 1. Các giai đoạn

| Giai đoạn | Mục tiêu |
|---|---|
| M1 — Nền tảng gRPC | Client cân bằng tải tới ba replica; đo và so sánh baseline |
| M2 — RL trên hệ thống thật | Q-learning học trọng số; cập nhật bộ chọn replica trong client theo chu kỳ |
| M3 — Digital Twin | Mô phỏng client, định tuyến và hàng đợi replica; hiệu chỉnh bằng dữ liệu thật |
| M4 — Twin hỗ trợ RL | Huấn luyện trong Twin, nạp policy vào client và đánh giá trên hệ thống thật |
| Hoàn thiện | Báo cáo, dữ liệu, hướng dẫn chạy và demo |

Kết quả cuối so sánh thuật toán truyền thống, RL học trực tiếp và RL học trong Twin trên cùng hệ thống. Không đặt trước yêu cầu RL phải tốt hơn.

## 2. Kiến trúc

Một client duy trì **ba channel grpc.aio, mỗi channel tới một replica**, cùng ba stub tương ứng. Channel được tạo khi khởi động và tái sử dụng cho nhiều RPC đồng thời.

Luồng mỗi request:

1. Bộ tạo tải chuyển request cho lớp điều phối trong client.
2. Bộ chọn chọn một replica đủ điều kiện theo policy hiện hành.
3. Client gọi unary RPC Execute qua stub/channel của replica đó.
4. Replica xử lý và trả kết quả trực tiếp về client.
5. Client ghi kết quả và cập nhật số RPC chưa hoàn thành của replica.

Bộ chọn Round Robin và Least Request đều được viết ở tầng ứng dụng, dùng chung ba channel, cơ chế theo dõi sức khỏe và đường gọi RPC.

| Thành phần | Vai trò |
|---|---|
| Bộ tạo tải | Phát request đồng thời với cấu hình tải xác định |
| Bộ chọn replica | Chọn backend theo Round Robin, Least Request hoặc trọng số ở mốc sau |
| Ba channel và stub | Gọi gRPC trực tiếp tới ba địa chỉ cố định |
| Theo dõi sức khỏe | Cập nhật backend có thể nhận request và phát hiện phục hồi |
| Ba server grpc.aio | Cùng cung cấp Execute; cấu hình thời gian xử lý và giới hạn đồng thời |
| Thu dữ liệu | Ghi latency, lỗi, phân phối request, thời gian chờ và thực thi |

Mỗi replica chạy trong container riêng; địa chỉ đề xuất là replica-1:50051, replica-2:50051 và replica-3:50051.

## 3. Phạm vi M1

- Một client tạo tải, ba replica cố định, unary RPC Execute.
- **Round Robin:** luân phiên các backend đủ điều kiện.
- **Least Request:** chọn backend có ít RPC chưa hoàn thành nhất; khi bằng nhau thì luân phiên.
- Bộ đếm chỉ phản ánh các RPC Execute do client này gửi và chưa kết thúc phía client.
- Cả hai policy dùng cùng workload, deadline và quy trình đo.
- Thử replica tương đương, một replica chậm, cùng tình huống dừng/phục hồi.
- Lưu cả request thành công, lỗi, timeout và hủy; đo thời gian chờ riêng với thời gian thực thi tại replica.

M1 tập trung vào hệ thống chạy lại được bằng cấu hình và script. Kubernetes, autoscaling và dashboard nằm ngoài phạm vi hiện tại.

**Chỉ chuyển sang M2 khi đạt các điều kiện nghiệm thu trong [M1.md](M1.md).**

## 4. Hướng phát triển sau M1

Ở M2, RL cập nhật trọng số theo chu kỳ; bộ chọn replica chỉ đọc trọng số hiện hành khi định tuyến. Việc huấn luyện nằm ngoài đường xử lý từng RPC.

Ở M3, Twin mô phỏng tải đến, lựa chọn replica, hàng đợi và thời gian xử lý. Dữ liệu M1 được dùng để hiệu chỉnh và kiểm tra mức độ khớp với hệ thống thật.

Ở M4, policy học trong Twin được đưa vào cùng lớp điều phối phía client để đánh giá trên hệ thống thật. Các baseline và RL phải được so sánh bằng cùng điều kiện đo.

## 5. Tài liệu tham khảo

| Nguồn | Nội dung áp dụng |
|---|---|
| [gRPC Python AsyncIO](https://grpc.github.io/grpc/python/grpc_asyncio.html) | API server, channel và gọi RPC bất đồng bộ |
| [gRPC Python Quick start](https://grpc.io/docs/languages/python/quickstart/) | Định nghĩa proto và sinh mã Python |
| [gRPC Health Checking](https://grpc.io/docs/guides/health-checking/) | Service báo trạng thái phục vụ; client dùng kết quả để cập nhật tập backend đủ điều kiện |
| [RILaaS — Tanwani và cộng sự, 2020](https://www.ajaytanwani.com/docs/Tanwani_RILaaS_RAL_CR_2020.pdf) | Hướng tham khảo về gRPC và RL chọn backend |
| [DT-assisted RL for Microservice Offloading — Chen và cộng sự, 2023](https://ira.lib.polyu.edu.hk/bitstream/10397/107541/1/Chen_Digital_Twin-assisted_Reinforcement.pdf) | Hướng tham khảo về kết hợp Twin với RL |

Các bài nghiên cứu cung cấp hướng tham khảo; thiết kế Twin và hiệu quả policy của đồ án cần được kiểm chứng trên hệ thống đã xây dựng.
