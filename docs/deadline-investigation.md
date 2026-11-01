# Điều tra và xử lý deadline — 26/10/2026

## Kết luận

Đã tái hiện lỗi bằng một client và một backend độc lập, không cần chuỗi service hoặc Envoy. Trên môi trường Windows của dự án, lỗi phụ thuộc rõ vào runtime gRPC 1.71.2 và áp lực RPC bị từ chối; cả API đồng bộ và AsyncIO đều bị ảnh hưởng.

Nâng đồng bộ stack lên gRPC 1.84.0 đã loại bỏ response OK nhận sau ngân sách trong các bài tái hiện và bài nghiệm thu đầy đủ đã chạy. Không thay timeout 3 giây, không đổi nhãn OK đến muộn và không nới gate deadline. Chưa xác định chính xác commit/dòng C-Core gây lỗi; không tuyên bố mọi máy Windows dùng 1.71.2 đều gặp lỗi này.

Kết quả hiện tại: **81 test đạt**, `pip check` sạch và `results/acceptance-verdict.json` có **`overall_accepted: true`**. Đây là kết luận cho phạm vi prototype một host đã kiểm chứng, không phải chứng nhận production, đa host hoặc mọi mức tải.

## Cách khoanh vùng

Hai script mới:

- `scripts/deadline_probe.py`: RPC tối giản, không import logic ứng dụng, không logging từng request; một protobuf request/response qua gRPC chuẩn. Có client đồng bộ và AsyncIO dùng cùng request, deadline và cách đo.
- `scripts/investigate_deadline.py`: chạy ma trận platform/API/network path, lưu từng request và cleanup các process/container do nó tạo.

Ba profile:

| Profile | Hành vi | Mục đích |
|---|---|---|
| fast | Backend chờ 50 ms, timeout client 3 giây | Baseline thành công |
| slow | Backend chờ 200 ms, timeout client 50 ms | Kiểm tra báo quá hạn |
| burst | Chỉ hai tác vụ xử lý và hai chờ, còn lại RESOURCE_EXHAUSTED; timeout 3 giây | Tạo áp lực từ chối tải để tái hiện lỗi |

Đối chiếu Windows native trực tiếp, Windows → backend Linux trong WSL, Windows → Envoy → Linux, và hai đường tương ứng cho client Linux. Với Windows native, RPC không đi qua WSL hoặc Envoy; WSL có thể vẫn chạy nền trên máy, nên không suy rộng thành thí nghiệm tắt toàn bộ virtualization.

Mỗi request đo `perf_counter`, `monotonic`, wall clock, thời gian tạo call, SDK time_remaining khi nhận kết quả và thời gian tại backend qua trailer. Sai khác giữa các đồng hồ chỉ ở mức khoảng 15 ms, không giải thích được OK muộn nhiều giây. Thời gian tạo call cũng không giải thích toàn bộ độ trễ. Reproducer tách biệt được hiện tượng khỏi nghiệp vụ, topology và per-request logging.

## Bằng chứng A/B

Trong lần chạy đối chiếu cuối, cùng reproducer và protobuf runtime, chỉ đổi gRPC ở hai đầu Windows:

| Runtime client/server Windows | API | Request/profile burst | OK | OK nhận sau 3 giây |
|---|---|---:|---:|---:|
| 1.71.2 / 1.71.2 | AsyncIO | 1.000 | 52 | **48** |
| 1.71.2 / 1.71.2 | Đồng bộ | 1.000 | 22 | **14** |
| 1.84.0 / 1.84.0 | AsyncIO, lần 1 | 1.000 | 218 | **0** |
| 1.84.0 / 1.84.0 | AsyncIO, lần 2 | 1.000 | 270 | **0** |
| 1.84.0 / 1.84.0 | Đồng bộ, lần 1 | 1.000 | 304 | **0** |
| 1.84.0 / 1.84.0 | Đồng bộ, lần 2 | 1.000 | 358 | **0** |

Các request còn lại chủ yếu là từ chối tải hoặc quá hạn, được giữ nguyên mã RPC trong raw data. Không tính request lỗi thành OK để làm phép so sánh đẹp hơn.

Trước đó đã thử chỉ đổi client: 1.84.0 không còn late OK trên đường Windows → Linux, nhưng còn vài trường hợp khi backend Windows vẫn dùng 1.71.2. Vì vậy chọn đồng bộ runtime ở cả hai đầu thay vì chỉ sửa load generator. Các lần thử với runtime cũ được thực hiện cả trước và sau thí nghiệm runtime mới, không chỉ một lần cold start.

Raw data:

- `results/deadline-lab/matrix.json`: ma trận fast/slow ban đầu, 20 case.
- `results/deadline-lab-grpc171-pressure/matrix.json`: 1.71.2 với áp lực từ chối tải.
- `results/deadline-lab-grpc184-pressure/matrix.json`: client 1.84.0 đối chiếu với server cũ.
- `results/deadline-lab-grpc184-both/matrix.json`: hai đầu mới, giai đoạn thử cô lập.
- `results/deadline-final-baseline171/matrix.json`: đối chứng cuối với 1.71.2.
- `results/deadline-final-fixed184/matrix.json`: hai lần lặp mỗi API với stack mới.

Mỗi matrix trỏ tới JSON chứa từng request. Không dùng p99 của tất cả RPC (gồm lỗi) làm p99 latency thành công; các field được đặt tên phân biệt.

## Thay đổi được áp dụng

1. Pin `grpcio`, `grpcio-health-checking`, `grpcio-tools` cùng **1.84.0** trong pyproject và Dockerfile; protobuf runtime **7.36.2**, sinh lại cả hai proto bằng compiler đi kèm. Schema RPC không đổi.
2. Dùng namespace chính thức `grpc_health.v1` thay alias tương thích cũ `grpc.health.v1`.
3. Bỏ `use_oghttp2_codec: false`; Envoy chạy codec mặc định. Full test trên stack mới không cần workaround này.
4. Thêm regression test `tests/test_deadline_runtime.py`: hai process thật, một backend, 1.000 RPC burst/128 concurrency, bắt buộc có cả OK và từ chối tải, không có expired OK.
5. Sửa test cancellation M1 để đồng bộ bằng event sau khi thực sự lấy semaphore rồi gọi cancel. Timeout 10 ms cũ có thể hết trước khi handler bắt đầu, nên không kiểm tra đúng race mà test mô tả. Không nới assert và không đổi deadline của bài kiểm thử lỗi runtime.

Chọn phiên bản sau thí nghiệm cô lập và đối chiếu [release chính thức 1.84.0](https://github.com/grpc/grpc/releases/tag/v1.84.0). Release note không xác nhận riêng hiện tượng của máy này; bằng chứng sửa lỗi là A/B và regression thực tế, không phải suy đoán một issue upstream khác là cùng nguyên nhân.

## Kiểm chứng đầy đủ sau sửa

Sau khi dựng lại image mới, giữ cùng topology và timeout:

| Kịch bản | Kết quả |
|---|---|
| 1.000 request nhẹ, concurrency 32 | 1.000 OK; không late OK |
| 300 request nặng, concurrency 16 | 300 OK; không late OK |
| 5.000 request nhẹ, concurrency 64 | 5.000 OK; không late OK |
| Windows overload, 1.000 request/128 concurrency | 83 OK, 801 RESOURCE_EXHAUSTED, 116 UNAVAILABLE có marker circuit breaker; **0 late OK** |
| Linux overload cùng capacity | 53 OK, 574 RESOURCE_EXHAUSTED, 373 UNAVAILABLE có marker circuit breaker; **0 late OK** |
| 100 request deadline 1 ms | 100 DEADLINE_EXCEEDED |
| Dừng/khôi phục một instance và toàn bộ A/B | Đạt |
| Toàn bộ pytest | **81 passed** |
| Dependency check | Không có xung đột |

Overload Windows ghi nhận p99 của response OK khoảng 955 ms; trước sửa từng có OK nhận sau nhiều giây. Không dùng số liệu này làm tuyên bố throughput tốt hơn: benchmark trên một host chưa có SLO và chưa kiểm soát hết biến thiên tài nguyên.

### Tách lỗi benchmark khỏi lỗi runtime

Lần chạy đầu trên stack mới có 32 rejection được xác nhận là circuit breaker khi tạo channel mới cho bài đo. Test steady-state trước đây bao gồm cả thời gian mở kết nối/khởi tạo upstream pool, trong khi pending limit là 32.

Đã bổ sung warm-up bốn RPC nhẹ trên **chính channel được đo**, ghi riêng warm-up và bắt đầu đo sau đó. Không tăng pending limit, active limit hay timeout cho request được đo. Các bài all-backends-down bỏ warm-up để vẫn kiểm tra đúng lỗi. Đây là thay đổi phương pháp đo, không phải bản vá runtime; cold-start rejection vẫn là hành vi cần quan tâm khi triển khai thực và được giữ trong `results/runtime184-cold-channel-acceptance.json`.

Kết luận về runtime không chỉ dựa trên benchmark đã warm-up: reproducer Native Windows độc lập ở bảng A/B cũng tái hiện cũ và không tái hiện mới.

## Chạy lại

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe scripts/gen_proto.py
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/acceptance_microservices.py --wsl Ubuntu-22.04 --compose-binary /mnt/d/LoadBalance/.venv/acceptance-tools/docker-compose-linux-x86_64
```

Chạy reproducer hiện tại không cần Envoy, bằng hai terminal Windows:

```powershell
# Terminal 1
.\.venv\Scripts\python.exe scripts/deadline_probe.py --serve --address 127.0.0.1:50261
# Terminal 2
.\.venv\Scripts\python.exe scripts/deadline_probe.py --address 127.0.0.1:50261 --api aio --profile burst --requests 1000 --concurrency 128 --timeout 3
```

Đối chứng runtime cũ trong thư mục cô lập, không downgrade môi trường chính:

```powershell
.\.venv\Scripts\python.exe -m pip install --no-deps --target .venv/acceptance-tools/grpc171 grpcio==1.71.2
.\.venv\Scripts\python.exe scripts/investigate_deadline.py --output results/deadline-replay171 --client-grpc-path .venv/acceptance-tools/grpc171 --native-server-grpc-path .venv/acceptance-tools/grpc171 --profiles burst --paths windows-native-direct
.\.venv\Scripts\python.exe scripts/investigate_deadline.py --output results/deadline-replay184 --profiles burst --paths windows-native-direct --repetitions 2
```

Harness matrix dùng WSL/Compose để quản lý project lab và giữ môi trường sống; path native không gửi RPC qua WSL. Hai lệnh terminal đơn giản phía trên không cần WSL. Các binary/runtime cô lập trong `.venv` không được commit.

## Giới hạn còn lại

Không có bảo đảm hard real-time rằng callback sẽ chạy chính xác tại 50 ms trên OS bận. Bài sửa này kiểm tra và loại bỏ trường hợp **trả OK sau deadline 3 giây trong các kịch bản đã tái hiện**, không tuyên bố mọi thông báo lỗi đều được giao về đúng từng ms ở mọi tải.

Chưa xác định chính xác patch upstream gây khác biệt; chưa soak hàng giờ, chưa nghiệm thu đa host/HA hoặc mọi kiểu workload. Lookaside/xDS và AIOps vẫn chưa triển khai. Các giới hạn đó không được suy ra là đã đạt chỉ vì deadline test hiện xanh.
