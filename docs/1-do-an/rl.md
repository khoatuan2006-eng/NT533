# RL trong nhánh lỗi mềm

## Bài toán

Một backend chậm nhưng vẫn trả lời thì health probe không rút nó ra. Muốn né nó, A phải gửi ít tải hơn cho nó. RL ở đây là một bộ điều khiển trọng số: đọc latency theo instance mà caller đo được, rồi đổi trọng số A → B mà control plane phát qua xDS.

Phần này phải trả lời hai điều:

1. RL có né backend chậm tốt hơn Round Robin, Least Request và **β cố định** không?
2. RL có an toàn khi chính nó thiếu dữ liệu hoặc dữ liệu sai không?

## Thiết kế

RL không chọn trọng số từng instance. Nó chọn **β**, mức mạnh tay của một công thức chia tải theo latency:

```text
wᵢ ∝ exp(−β · (lᵢ / l_min − 1))        β = 0 là Round Robin
```

| Thành phần | Giá trị |
|---|---|
| Tín hiệu | Latency mỗi instance đo ở caller, theo cửa sổ 10 giây; áp lực hàng đợi lấy từ trailer `x-server-queue-ms` |
| Lọc nhiễu | Chênh lệch latency nhỏ hơn sai số đo thì coi như bằng 0, để không dồn tải vào instance "trông nhanh" do may rủi |
| Ngữ cảnh | Độ chênh latency (4 mức) × áp lực hàng đợi (3 mức) = 12 ngữ cảnh |
| Hành động | Chọn β trong {0; 0,5; 1; 2; 4}, giữ 5 cửa sổ |
| Thuật toán | Bandit có ngữ cảnh, học ngoại tuyến trong mô phỏng, **đóng băng khi chạy** |
| Model đang dùng | `configs/shape-model.json` |

### Rào an toàn

- Mỗi instance giữ ít nhất 5% tải; mỗi cửa sổ trọng số đổi tối đa 10 điểm phần trăm.
- Quay về Round Robin khi telemetry cũ quá 15 giây, thiếu mẫu, danh sách instance không khớp, hoặc lỗi không quy được cho instance nào vượt 10%.
- Không có exploration trên cụm.
- Mỗi control plane đọc telemetry của caller cùng node và quyết định độc lập; một bên hỏng thì bên kia vẫn chạy.

Mỗi quyết định in một dòng `rl_window` trong log control plane; mỗi lần quay về Round Robin in `rl_fallback` kèm lý do.

## Đối thủ và cách so

| Đối thủ | Phương án trên cụm | Ý nghĩa |
|---|---|---|
| Round Robin | `xds` | Mốc |
| **β cố định = 2** | `xds-beta` | Cùng công thức, không học. **Nếu RL không hơn nó thì phần học không đóng góp gì** |
| Least Request | `static-lr` | Phản ứng theo từng request; khác đường dữ liệu, chỉ so bằng mức cải thiện so với mốc của từng bên |

Quy tắc so sánh đầy đủ: [tra cứu](../2-cong-viec/tra-cuu.md#quy-tắc-so-sánh).

## Ba nguồn bằng chứng

| Nguồn | Công cụ | Trả lời | Tình trạng |
|---|---|---|---|
| Mô phỏng | `train_shape_rl.py` | RL có hơn β cố định trên nhiều loại kịch bản không | Cần chạy lại một lần để chốt số (xem dưới) |
| gRPC thật, máy cục bộ | `compare_shape_rl.py` | Kết quả mô phỏng có giữ trên gRPC thật không | Chưa có lượt đủ lần lặp |
| Cụm ba VM | `run_load.py` với `xds-rl`, `xds-beta` | RL có né b-2 chậm trên hệ thật không | Chưa đo |

**Số mô phỏng cần chốt.** Báo cáo mô phỏng còn trên máy (`results/shape-rl/simulation.md`) ghi RL hơn β cố định tốt nhất +0,012 trên tập kiểm tra, nhưng model của lượt đó khác `configs/shape-model.json`. Trước khi đưa số mô phỏng vào báo cáo, chạy lại huấn luyện một lần và xác nhận model sinh ra trùng model đang triển khai ([phân công](../2-cong-viec/phan-cong.md), việc K2).

## Điều đã biết trước khi đo

- **Sàn 5% giới hạn P95.** Backend chậm còn nhận 5% tải thì P95 và P99 vẫn rơi vào nó. Kỳ vọng RL cải thiện latency trung bình và P50, không cải thiện P99.
- **Chia theo latency có hại khi các backend giống nhau.** Lệch tải nhỏ cũng làm hàng đợi ở instance nhận nhiều tăng lên. Kỳ vọng ở kịch bản `even`, RL và β cố định không hơn Round Robin.
- **Khoảng cách RL − β cố định nhỏ.** Khi một backend chậm rõ và ổn định, mọi β lớn cho kết quả gần giống nhau. Đo được "ngang nhau" là kết quả bình thường và phải ghi đúng như vậy.
- **Khi một instance B dừng**, telemetry không còn khớp danh sách endpoint, RL quay về Round Robin. Đây là hành vi mong muốn.

## Giới hạn

- Mô phỏng coi mỗi backend là hàng đợi M/M/c; chỉ xấp xỉ backend thật.
- Lượt gRPC cục bộ chạy mọi thứ trong một tiến trình trên Windows.
- Cụm chỉ có ba instance B.
- Model học ngoại tuyến rồi đóng băng, không tự thích nghi khi môi trường đổi lâu dài.
