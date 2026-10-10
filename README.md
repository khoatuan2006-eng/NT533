# Hướng đi đồ án: Cân bằng tải gRPC phía client với RL và Digital Twin

Sử dụng Python và grpc.aio; một tiến trình client tạo nhiều RPC đồng thời tới các replica. Bộ chọn replica nằm trong ứng dụng client. Nhóm gồm Khoa, Lầu và Đạt; cấu hình ban đầu có ba replica.

README mô tả định hướng và kiến trúc chung. Phân công và tiến độ nền tảng nằm trong [M1.md](M1.md); kế hoạch tích hợp Q-learning và Gemini nằm trong [M2.md](M2.md).

## 1. Các giai đoạn

| Giai đoạn | Mục tiêu |
|---|---|
| M1 — Nền tảng gRPC | Client cân bằng tải tới ba replica; đo và so sánh baseline |
| M2 — RL và LLM | Q-learning cập nhật trọng số phân tải; Gemini qua API đề xuất chế độ reward khi có sự kiện kéo dài |
| M3 — Digital Twin | Mô phỏng client, định tuyến và hàng đợi replica; hiệu chỉnh bằng dữ liệu thật |
| M4 — Twin hỗ trợ RL | Huấn luyện trong Twin, nạp policy vào client và đánh giá trên hệ thống thật |
| Hoàn thiện | Đo lường so sánh, đóng gói Docker, báo cáo, dữ liệu và hướng dẫn chạy |

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

Trong giai đoạn phát triển, client và các replica chạy trực tiếp bằng Python trên một máy, mỗi replica có cổng riêng. Docker được hoàn thiện ở bước đóng gói cuối.

## 3. Phạm vi M1

- Một client tạo tải, ba replica cố định, unary RPC Execute.
- **Round Robin:** luân phiên các backend đủ điều kiện.
- **Least Request:** chọn backend có ít RPC chưa hoàn thành nhất; khi bằng nhau thì luân phiên.
- Bộ đếm chỉ phản ánh các RPC Execute do client này gửi và chưa kết thúc phía client.
- Cả hai policy dùng cùng workload, deadline và quy trình đo.
- Thử replica tương đương, một replica chậm, cùng tình huống dừng/phục hồi.
- Lưu cả request thành công, lỗi, timeout và hủy; đo thời gian chờ riêng với thời gian thực thi tại replica.

M1 tập trung vào hệ thống chạy lại được bằng cấu hình và script. Kubernetes, autoscaling và dashboard nằm ngoài phạm vi hiện tại.

Nền tảng M1 đã chạy được bằng Python, gồm hai policy, health checking và log client/server. Các mục đã kiểm tra được đánh dấu trong [M1.md](M1.md). Nhóm tiếp tục tích hợp M2; nghiệm thu đo lường đầy đủ thực hiện sau khi tích hợp.

## 4. Hướng phát triển sau M1

Ở M2, Q-learning cập nhật trọng số theo chu kỳ; bộ chọn replica đọc trọng số hiện hành khi định tuyến. Gemini chạy qua API ở tác vụ nền khi có sự kiện kéo dài, đề xuất một chế độ reward trong tập đã định nghĩa. Khi API lỗi, hệ thống giữ cấu hình reward hiện tại. Chi tiết nằm trong [M2.md](M2.md).

Phân công M2: **Đạt** phụ trách Q-learning và client phân tải; **Lầu** phụ trách reward và Gemini; **Khoa** phụ trách controller, dữ liệu, phát hiện sự kiện và tích hợp demo web. M2 hiện là kế hoạch, chưa triển khai RL/LLM.

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
