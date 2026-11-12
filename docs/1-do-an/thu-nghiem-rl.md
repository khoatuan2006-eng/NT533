# Thử nghiệm RL cho lookaside

## Vị trí trong đồ án

RL thuộc **nhánh lỗi mềm** của câu hỏi chính (xem [README](../../README.md#câu-hỏi-của-đồ-án)): một backend vẫn trả lời nhưng chậm, health probe không rút nó ra, nên việc né nó phụ thuộc vào chiến lược cân bằng tải. RL là một chiến lược như vậy: đọc latency theo endpoint và đổi trọng số mà control plane phát qua xDS.

Phần này cần trả lời hai điều:

1. **Có né được backend chậm không**, so với Round Robin, β cố định tốt nhất và Least Request, trong cùng lượt chạy.
2. **Có an toàn khi chính nó lỗi không**: telemetry cũ, thiếu mẫu hoặc lỗi vượt 10% thì phải quay về Round Robin.

Tiêu chí an toàn và kết luận:

- RL không nằm trên đường xử lý request; khi telemetry cũ, sai hoặc thiếu mẫu thì quay về Round Robin.
- Mỗi endpoint giữ ít nhất 5% tải và mỗi cửa sổ đổi tối đa 10 điểm phần trăm; model đóng băng, không có exploration trên cụm.
- Chỉ kết luận RL tốt hơn khi vượt β cố định tốt nhất (cùng công thức, một β chọn trước bằng mô phỏng) trên nhiều lần lặp. Báo cáo median giữa các seed huấn luyện, không báo seed tốt nhất.

RL và β cố định chạy trên chính cụm ba VM qua phương án `xds-rl` và `xds-beta`, cùng lượt `run_load.py` với Round Robin và Least Request. Service B có ba instance, mỗi node một (b-1, b-2, b-3). Least Request đi đường static còn RL đi đường xDS, nên hai bên chỉ so bằng mức cải thiện so với Round Robin cùng đường ([quy tắc so sánh chiến lược](../2-cong-viec/3-tra-cuu.md#quy-tắc-so-sánh-chiến-lược)). Nếu RL không hơn β cố định thì báo cáo ghi đúng như vậy.

## Thiết kế

### Phiên bản 1: nguyên mẫu hai instance (không còn chạy trên cụm)

Chỉ điều khiển đúng hai endpoint; với ba instance B nó luôn quay về Round Robin, nên cụm dùng phiên bản 2 (bên dưới). Giữ ở đây vì các lượt đo localhost cũ và phần đối chiếu nghiên cứu dựa trên nó.

| Thành phần | Trong đồ án |
|---|---|
| Chặng điều khiển | A → B, hai instance (b-1, b-2). A → C giữ Round Robin làm đối chứng trong cùng lượt |
| Nhịp quyết định | Một cửa sổ 10 giây; chỉ đổi khi hai cửa sổ liền nhau cùng nhóm (chống dao động) |
| Tín hiệu | Latency hiệu dụng mỗi instance = median / tỷ lệ thành công, đo ở caller |
| Trạng thái | (nhóm tỷ lệ latency b-1/b-2: nhanh hơn rõ / tương đương / chậm hơn rõ, mức trọng số 3–7) |
| Hành động | Chuyển 10% tải sang một bên, hoặc giữ nguyên |
| Reward | Tỷ lệ thành công, trừ lỗi, trừ P95 và trung bình vượt mục tiêu 50 ms, trừ nhẹ việc đổi trọng số |
| Rào an toàn | Trọng số 30–70%; telemetry cũ quá 15 giây, thiếu mẫu, lỗi vượt 10% hoặc danh sách instance đổi thì quay về Round Robin |
| Học | Ngoại tuyến trong mô phỏng, bảng Q đóng băng khi chạy, không exploration |

Mỗi control plane chạy một bộ điều khiển riêng và đọc telemetry của caller cùng node (a-1 cho controlplane-1, a-2 cho controlplane-2). Hai bộ không cần phối hợp vì cùng quan sát hai instance B; một bên hỏng thì bên kia vẫn chạy, và caller nào mất control plane của mình thì dùng bên còn lại.

Quy tắc đối chứng của phiên bản 1 (`experiment_rl_lookaside.py --rule`) dùng **cùng** tín hiệu, trạng thái, nhịp và rào an toàn; nó chỉ khác ở bước quyết định: lệch rõ thì dịch về bên nhanh, tương đương thì giữ. Phiên bản 2 giữ nguyên ý này: đối chứng là cùng công thức với β cố định.

### Đối chiếu với các nghiên cứu

| Nghiên cứu | Ý chính | Áp dụng vào đồ án |
|---|---|---|
| C3, NSDI 2015 [1] | Xếp hạng replica theo phản hồi từ server (thời gian xử lý, hàng đợi) kèm điều khiển tốc độ gửi; giảm tới 3 lần độ trễ P99.9 trên Cassandra | Giữ trailer `x-instance-id` để quy lỗi cho đúng instance. Bài học ngược: chỉ dùng thời gian xử lý ở server thì bỏ sót độ trễ trên đường mạng. Đồ án đổi sang đo ở caller (xem bên dưới). Chưa dùng tín hiệu hàng đợi |
| Prequal, NSDI 2024 [2] | Ở Google, cân bằng theo số request đang xử lý và latency qua thăm dò, thay cho Weighted Round Robin theo CPU | Giải thích vì sao bộ điều khiển theo cửa sổ 10 giây khó thắng chiến lược theo từng request như Least Request. Báo cáo so cơ chế, không kỳ vọng RL thắng ở phản ứng nhanh |
| gRPC A58 / A51 [3] | Weighted Round Robin phía client, trọng số tính từ số đo backend gửi trong trailer (ORCA) | Kiến trúc đồ án là phiên bản qua control plane của ý này: backend báo trong trailer, trọng số phát qua xDS. Quy tắc đối chứng đóng vai "bộ tính trọng số có sẵn" mà RL phải vượt |
| Mitzenmacher, TPDS 2001 [4] | Chọn ngẫu nhiên hai backend rồi lấy bên ít tải cho lợi ích theo cấp số mũ so với một lựa chọn; chọn ba chỉ thêm hằng số | Cơ sở lý thuyết của Least Request (Envoy, gRPC dùng hai lựa chọn). Với chỉ hai instance, hai lựa chọn trùng với so toàn bộ, nên ba instance mới cho thấy khác biệt (hướng phát triển) |
| Mao và cộng sự, ICLR 2019 [5] | Trong cân bằng tải, tải đến (đầu vào bên ngoài) làm reward dao động mạnh, che mất tác dụng của hành động | Học trong mô phỏng với tải cố định, không học trên cụm. Khi đánh giá, các phương án chạy cùng mức tải, xen thứ tự, lặp nhiều lần, so theo median và khoảng [min–max] |
| Garivier và Moulines, ALT 2011 [6] | Bandit không dừng: khi môi trường đổi đột ngột, phải quên dần dữ liệu cũ (chiết khấu, cửa sổ trượt) | Cửa sổ 10 giây và yêu cầu hai cửa sổ liền nhau là dạng cửa sổ trượt đơn giản. Hướng phát triển: thay Q-learning bằng bandit có chiết khấu, vì hành động ít ảnh hưởng trạng thái tương lai |
| Alshiekh và cộng sự, AAAI 2018 [7] | "Shield" chặn hành động vi phạm điều kiện an toàn, để agent học tự do trong phần còn lại | Rào 30–70%, kiểm tra telemetry và quay về Round Robin chính là shield viết tay. Câu hỏi an toàn của đồ án kiểm tra shield này |
| Samani và Stadler, CNSM 2022 [8] | RL điều khiển định tuyến và chặn request trên service mesh Istio/Kubernetes, học trong mô phỏng rồi đánh giá trên testbed | Cùng quy trình học ngoại tuyến, đánh giá trên hệ thật với tải chưa thấy khi học. Khác: đồ án dùng bảng Q nhỏ và điều khiển một chặng |

### Thay đổi đã làm theo đối chiếu

- **Đo latency ở caller cho cụm** (`EXPERIMENTAL_RL_LATENCY=rpc`). Trước đây collector ưu tiên thời gian xử lý ở server. Kịch bản `slow-node3` làm chậm gói tin chứ không làm chậm xử lý, nên bộ điều khiển không thấy b-2 chậm. Lab localhost vẫn dùng thời gian server, vì độ chậm ở đó nằm trong xử lý và đồng hồ Windows thô.
- **Đối chứng chạy trong control plane**, cùng khung an toàn với RL, để chạy chung một lượt (`EXPERIMENTAL_RL_POLICY=rule` cho phiên bản 1, `shape-fixed` cho phiên bản 2).
- **Mỗi quyết định ghi một dòng log** (`rl_window`: latency hiệu dụng, trạng thái, hành động, trọng số, reward; `rl_fallback`: lý do quay về Round Robin), để nhóm vẽ trọng số theo thời gian và giải thích kết quả.
- **Thời gian ổn định trước mỗi ô đo** (`run_load.py --settle`), áp cho mọi phương án.

### Phiên bản 2: RL chọn mức mạnh tay

**Vì sao cần.** In chính sách của bảng Q phiên bản 1 ra thì thấy nó trùng với quy tắc ở mọi trạng thái lệch. Có ba nguyên nhân: chỉ hai instance nên đáp án hiển nhiên; trạng thái không có mức tải nên không học được "tải nặng thì nhẹ tay"; reward tuyệt đối nên bị tải đến chi phối. Thêm vào đó, sàn 30% làm P95 không thể cải thiện: backend chậm còn nhận trên 5% tải thì P95 vẫn rơi vào nó.

**Thiết kế.** RL không chọn trọng số từng instance; nó chọn β, mức mạnh tay của một công thức chia tải. Cách này giống gRPC A58 (trọng số tính từ số đo backend), chỉ thêm phần học độ gắt.

| Thành phần | Phiên bản 2 |
|---|---|
| Công thức | wᵢ ∝ exp(−β · (lᵢ / l_min − 1)); β = 0 là Round Robin |
| Hành động | Bandit (mặc định): chọn thẳng một β trong {0; 0,5; 1; 2; 4} cho ngữ cảnh hiện tại rồi giữ nó 5 cửa sổ. Q-learning (tùy chọn): giảm, giữ hoặc tăng β một bậc. Cả hai có số hành động không đổi theo số instance |
| Trạng thái | (độ chênh latency chậm nhất / nhanh nhất: 4 mức; áp lực hàng đợi: 3 mức) = 12 ngữ cảnh cho bandit; Q-learning thêm β hiện tại. Latency được co theo sai số đo trước (xem mục sửa bầy đàn) |
| Áp lực | Phần thời gian request chờ trong hàng đợi server, lấy từ trailer `x-server-queue-ms` (tín hiệu kiểu C3) |
| Ràng buộc | Sàn 5% mỗi instance; mỗi cửa sổ đổi tối đa 10 điểm phần trăm; chỉ đổi β sau hai cửa sổ cùng trạng thái; quay về Round Robin như phiên bản 1, trừ một điểm: chỉ lỗi **không quy được cho instance nào** vượt 10% mới gây quay về. Lỗi có địa chỉ (server từ chối vì quá tải) là tín hiệu để né; quay về Round Robin vì chúng sẽ trả lại backend quá tải đủ phần tải của nó (lần chạy thử đầu đã kẹt đúng như vậy) |
| Reward (chỉ khi học) | (latency Round Robin − latency chính sách) / latency Round Robin, cùng đầu vào, cùng cửa sổ; trừ 4 × phần lỗi nhiều hơn Round Robin; trừ 0,01 mỗi lần đổi β |
| Thuật toán | Bandit có ngữ cảnh (mặc định): mỗi ô (ngữ cảnh, β) lưu reward trung bình, chọn ô cao nhất, ε giảm dần 0,3 → 0,02 khi học. Q-learning (tùy chọn): γ = 0,3, α = 0,1 |
| Huấn luyện | Mô phỏng: mỗi backend là hàng đợi M/M/c (Erlang C), latency tăng theo phần tải nhận; 2–4 backend; sáu loại kịch bản |
| Loss | Không có hàm loss riêng. Bandit: trung bình cộng dồn Q ← Q + (r − Q)/n, với r là reward trung bình của 5 cửa sổ lựa chọn có hiệu lực. Q-learning: sai số TD, δ = r + γ·max Q(s′, ·) − Q(s, a) |

Sáu loại kịch bản mô phỏng: `even` (đối chứng, các backend giống hệt), `path-slow` (chậm do mạng), `processing-slow` (chậm do xử lý, mất năng lực), `uneven-capacity` (năng lực lệch 2/4/8 luồng), `flapping` (chậm bật tắt), `load-step` (tải nhảy giữa chừng).

**Đối thủ phải thắng: β cố định tốt nhất**, chọn một lần cho mọi kịch bản như khi triển khai thật. Nếu RL không thắng nó thì việc học không đóng góp gì. Ngoài ra còn Round Robin, Least Request và cận trên "oracle" (biết loại kịch bản thì chọn β tốt nhất của loại đó).

**Quy trình chống tự lừa mình.** Năm seed huấn luyện; một seed kiểm định để chọn model; một seed kiểm tra chạy đúng một lần cho model đã chọn, và chỉ số trên tập kiểm tra được báo cáo. Trong lúc thiết kế, mình đã xem kết quả trên seed kiểm định (thêm rồi gỡ một "vùng chết" vì không có tác dụng); seed kiểm tra 2026 chưa được dùng để quyết định thiết kế.

**Quan sát trong mô phỏng (tập kiểm định, một seed huấn luyện, để dự đoán):**

- Không β cố định nào tốt nhất ở mọi kịch bản: β nhỏ (0,5) tốt nhất khi chậm do mạng hoặc bật tắt; β lớn (4) tốt nhất khi năng lực lệch hoặc xử lý chậm. Đây là lý do để có một bộ chọn β thay vì một hằng số.
- Ở `even`, **mọi** β > 0 đều làm latency tăng 11–16% so với Round Robin. Gần bão hòa, chỉ cần lệch tải chút ít là hàng đợi ở backend nhận nhiều tăng vọt. RL giảm được thiệt hại nhưng không hết.
- Chênh lệch RL − β cố định tốt nhất thay đổi từ −0,032 đến +0,022 giữa năm seed huấn luyện, tức cùng cỡ với biến động huấn luyện. Vì thế mới cần nhiều seed và tập kiểm tra.

**Các lượt chạy công cụ trong lúc thiết kế (11–12/11).** RL − β cố định tốt nhất, trên tập kiểm định, năm seed huấn luyện:

| Cách học | Median | Khoảng |
|---|---:|---|
| Q-learning, β khởi đầu 1, giữ 2 cửa sổ | −0,012 | −0,032 đến +0,022 |
| Q-learning, β khởi đầu 2, giữ 2 cửa sổ | −0,006 | −0,012 đến +0,016 |
| Q-learning, β khởi đầu 2, giữ 1 cửa sổ | −0,014 | −0,059 đến +0,010 |
| Bandit, mỗi lựa chọn giữ 1 cửa sổ | −0,003 | −0,004 đến −0,003 |
| Bandit, giữ 3 cửa sổ | −0,003 | −0,006 đến +0,019 |
| **Bandit, giữ 5 cửa sổ (mặc định)** | **+0,020** | **+0,016 đến +0,021** |

- Q-learning thua ở median trong mọi cấu hình đã thử: một agent điển hình thua β cố định tốt nhất. Seed tốt nhất của nó từng đạt +0,010 trên seed kiểm tra 2026, nhưng đó là nhờ chọn seed may mắn. Seed 2026 vì thế đã bị nhìn; mặc định giờ là seed kiểm tra 3031.
- Bandit giữ lựa chọn 5 cửa sổ thắng ở **cả năm seed**. Lý do hợp lý: β đổi thì trọng số cần vài cửa sổ mới tới đích (mỗi cửa sổ dịch tối đa 10 điểm), nên giữ lâu hơn thì reward chấm đúng lựa chọn đó hơn. Dừng chỉnh ở đây để không khớp quá mức với tập kiểm định.
- Cận trên oracle chỉ hơn β cố định tốt nhất khoảng 0,04–0,05, nên mọi bộ chọn β có ít chỗ để hơn.

**Kết quả trên tập kiểm tra (seed 3031, chạy một lần cho model bandit đã chọn):** bandit hơn β cố định tốt nhất (β = 2) **+0,012** tính chung. Theo từng loại: `even` +0,040 (ít hại hơn), `path-slow` +0,013, `uneven-capacity` +0,017, `flapping` +0,008, `load-step` +0,020 (hơn cả oracle, vì đổi β giữa chừng khi tải nhảy), và **thua** ở `processing-slow` −0,027. Lợi thế giảm từ +0,020 (kiểm định) xuống +0,012 (kiểm tra), và chỉ đạt khoảng một phần tư khoảng cách tới oracle.

Kết luận trong mô phỏng: bandit có ngữ cảnh **hơn ổn định nhưng ít** so với β cố định tốt nhất; Q-learning thì không. Nhóm chạy lại lệnh mặc định sẽ ra đúng các số này (mô phỏng tất định). Chưa có số đo trên gRPC thật.

**Tăng lên 10 instance (12/11).** Mô phỏng được mở rộng tới 2–10 instance (khoảng 30% số instance gặp sự cố, tổng năng lực không đổi), huấn luyện lại, seed kiểm tra mới 4041:

- Ở 10 instance, chia theo latency **làm hại nặng khi các backend giống nhau**: `even` tệ hơn Round Robin 43–99% với β cố định, 61% với RL. Nguyên nhân là hiện tượng bầy đàn: trong 10 số đo nhiễu luôn có vài backend trông nhanh do may rủi, công thức dồn tải vào chúng và làm chúng quá tải. Mitzenmacher [9] đã chỉ ra điều này với thông tin tải cũ, và đó là lý do Least Request chỉ chọn giữa 2 backend ngẫu nhiên.
- RL − β cố định tốt nhất: −0,002 ở 3 instance, +0,005 ở 10 instance, +0,025 trên hỗn hợp 2–10.

Lượt thử trên gRPC thật, 10 backend, 2 lần lặp, cửa sổ 4 giây (mean ms, median của hai lần; hai lần lặp là ít, chỉ để định hướng):

| Kịch bản | Round Robin | Least Request | β = 4 | RL (bandit) |
|---|---:|---:|---:|---:|
| `even` | **166** | 167 | 217 | 188 |
| `uneven-capacity` | 249 | 310 | 263 | **228** |
| `flapping` | 249 | 236 | 220 | **219** |

- `uneven-capacity`: RL nhanh nhất, hơn β = 4 khoảng 36 ms và hơn Least Request ở cả hai lần lặp, P95 thấp nhất (477 ms). Least Request kém vì backend quá tải từ chối request nhanh, nên số request đang chờ ở nó thấp và nó vẫn được gửi tải ("lỗi nhanh hút tải").
- `even`: RL chậm hơn Round Robin và Least Request khoảng 21 ms ở cả hai lần, đúng hiện tượng bầy đàn mà mô phỏng dự đoán, dù ít hại hơn β cố định.
- `flapping`: RL ngang β cố định; so với Round Robin và Least Request thì hai lần lặp cho kết quả ngược nhau.

So với 3 instance, khác biệt ở 10 instance lớn hơn rõ (hàng chục ms, 10–35%), nhưng theo cả hai chiều: RL thắng khi năng lực lệch, thua khi các backend giống nhau.

**Sửa hiện tượng bầy đàn: chỉ tin chênh lệch vượt sai số đo (12/11).** Trước khi vào công thức và trạng thái, latency mỗi backend được kéo về median chung, bớt đi k lần sai số chuẩn của chính nó (sai số chuẩn của median ≈ 1,2533·σ/√n, tính từ mẫu trong cửa sổ). Chênh lệch nhỏ hơn mức đó coi như bằng 0. Ngưỡng k theo hiệu chỉnh Bonferroni cho số backend (xác suất báo động giả 5% mỗi cửa sổ): 2,4 ở 3 backend, 2,8 ở 10. Áp cho cả RL lẫn β cố định. Mô phỏng sinh nhiễu theo số mẫu thật (ít mẫu thì nhiễu lớn), tổng năng lực khoảng 240 RPS như lab localhost.

Tập kiểm định, median 5 seed (cải thiện so với Round Robin):

| | Không co | Ngưỡng cố định 2 | Bonferroni (chọn) |
|---|---:|---:|---:|
| RL, tính chung | +0,230 | +0,278 | +0,282 |
| RL − β cố định tốt nhất | +0,012 | +0,022 | +0,021 |
| RL ở `even` | −0,218 | −0,067 | −0,047 |
| RL ở `even`, 10 instance | — | −0,182 | −0,099 |

Tập kiểm tra seed 5051 (chạy một lần): RL hơn Round Robin +0,281 tính chung, hơn β cố định tốt nhất +0,016. Ở **3 instance**, RL hơn Round Robin +0,427 và gần hòa ở `even` (−0,013), sát cận trên oracle (0,434). Ở **10 instance**, RL hơn Round Robin +0,158 tính chung nhưng vẫn thua ở `even` (−0,208), `path-slow` (−0,072) và `flapping` (−0,073): bầy đàn giảm nhưng chưa hết.

Trên gRPC thật (10 backend, 2 lần lặp) **chưa kết luận được**: cùng một chính sách cho kết quả rất khác giữa các lần chạy. Round Robin ở `uneven-capacity` đo được 249, 303 rồi 531 ms ở ba lượt khác nhau; RL với phần sửa đo được 912 ms trong một lượt nhưng 210 ms (7,7% lỗi) khi chạy riêng. Khi các backend nhỏ quá tải, hệ ở vùng hỗn loạn (hàng đợi đầy rồi từ chối liên tục), nên cần ít nhất 5 lần lặp. `compare_shape_rl.py` giờ loại lượt nào có request gửi trễ lịch quá 50 ms (máy đo nghẽn); `--log-decisions` ghi từng quyết định của controller để giải thích kết quả.

**Giới hạn.** Mô hình M/M/c chỉ xấp xỉ backend thật. Lab localhost chạy mọi thứ trong một tiến trình và chỉ tái hiện được chậm do xử lý. Cụm có ba instance B (đúng quy mô mà mô phỏng cho kết quả tốt nhất), mỗi node một instance nên mất một node vẫn còn hai.

Lệnh chạy: [tham chiếu thí nghiệm](../2-cong-viec/3-tra-cuu.md#rl-phiên-bản-2--train_shape_rlpy-compare_shape_rlpy).

### Chạy trên cụm

Cụm ba VM, B có ba instance (b-1 trên node1, b-3 trên node2, b-2 trên node3); A và C giữ nguyên, C vẫn Round Robin làm đối chứng trong cùng lượt. Hai phương án cho `run_load.py`:

| Phương án | Trọng số A → B |
|---|---|
| `xds-rl` | Công thức chia theo latency, bandit đóng băng (`configs/shape-model.json`) chọn β |
| `xds-beta` | Cùng công thức, β = 2 cố định: β tốt nhất ở ba endpoint trên tập kiểm định của mô phỏng, chọn trước khi đo |

Mỗi control plane đọc telemetry của caller cùng node (a-1 cho controlplane-1, a-2 cho controlplane-2) và quyết định độc lập, nên một bên hỏng thì bên kia vẫn chạy. Latency đo ở caller (`EXPERIMENTAL_RL_LATENCY=rpc`), trailer `x-server-queue-ms` của B cho áp lực hàng đợi. Khi một instance B dừng, telemetry (ba địa chỉ) không còn khớp với danh sách discovery (hai địa chỉ) nên bộ điều khiển quay về Round Robin: an toàn, và là hành vi mong muốn trong bài lỗi cứng.

**Dự đoán trước khi đo** (để nhóm kiểm chứng), ở `slow-node3` (+50 ms cho gói đi ra từ node3, nên b-2 chậm hơn b-1 và b-3 nhiều lần):

- Phần tải vào b-2 (cột "Phần b-2"): Round Robin ≈ 33%; `xds-beta` và `xds-rl` giảm về gần sàn 5%. Mỗi cửa sổ trọng số dịch tối đa 10 điểm, nên cần ít nhất ba cửa sổ; đó là lý do `--settle 90`.
- Latency trung bình và P50 giảm rõ so với `xds`. **P95 gần như không đổi và P99 không đổi**: sàn 5% vẫn gửi tải vào b-2, mà chỉ cần backend chậm còn nhận trên 5% thì P95 vẫn rơi vào nó (trên 1% thì P99).
- `xds-rl` và `xds-beta` gần như bằng nhau ở `slow-node3`, vì độ chênh lớn và ổn định thì mọi β lớn đều cho cùng kết quả. Chỗ phần học có thể khác là ở `even`: RL kéo β xuống để gần Round Robin hơn β cố định. Mô phỏng ba instance cho RL hơn β cố định khoảng +0,016, tức nhỏ; nếu đo được ngang nhau thì đó là kết quả bình thường.
- `static-lr` (Least Request) so với `static` ở `slow-node3`: kỳ vọng cũng giảm phần b-2, và phản ứng nhanh hơn hai phương án trên vì quyết định theo từng request.

### Chưa làm, ghi vào hướng phát triển

- Số request đang xử lý theo từng request như Prequal; RL hiện vẫn phản ứng theo cửa sổ.
- Phiên bản 2 trên cụm (cần b-3), gộp telemetry từ mọi caller, bộ điều khiển cho A → C.
- Bandit có chiết khấu (Garivier và Moulines) để theo kịp khi môi trường đổi lâu dài; bandit hiện học ngoại tuyến rồi đóng băng.

### Tài liệu tham khảo

1. L. Suresh, M. Canini, S. Schmid, A. Feldmann. [C3: Cutting Tail Latency in Cloud Data Stores via Adaptive Replica Selection](https://www.usenix.org/conference/nsdi15/technical-sessions/presentation/suresh). NSDI 2015.
2. B. Wydrowski, R. Kleinberg, S. Rumble, A. Archer. [Load is not what you should balance: Introducing Prequal](https://www.usenix.org/conference/nsdi24/presentation/wydrowski). NSDI 2024.
3. gRPC. [A58: Client-side weighted round robin](https://github.com/grpc/proposal/blob/master/A58-client-side-weighted-round-robin-lb-policy.md); [A51: Custom backend metrics](https://github.com/grpc/proposal/blob/master/A51-custom-backend-metrics.md).
4. M. Mitzenmacher. [The Power of Two Choices in Randomized Load Balancing](https://www.eecs.harvard.edu/~michaelm/postscripts/tpds2001.pdf). IEEE TPDS 12(10), 2001.
5. H. Mao, S. B. Venkatakrishnan, M. Schwarzkopf, M. Alizadeh. [Variance Reduction for Reinforcement Learning in Input-Driven Environments](https://arxiv.org/abs/1807.02264). ICLR 2019.
6. A. Garivier, E. Moulines. [On Upper-Confidence Bound Policies for Switching Bandit Problems](https://arxiv.org/abs/0805.3415). ALT 2011.
7. M. Alshiekh, R. Bloem, R. Ehlers, B. Könighofer, S. Niekum, U. Topcu. [Safe Reinforcement Learning via Shielding](https://ojs.aaai.org/index.php/AAAI/article/view/11797). AAAI 2018.
8. F. Samani, R. Stadler. [Dynamically meeting performance objectives for multiple services on a service mesh](https://arxiv.org/abs/2210.04002). CNSM 2022.
9. M. Mitzenmacher. [How Useful Is Old Information?](https://www.eecs.harvard.edu/~michaelm/abstracts/tpds2000.html) IEEE TPDS 11(1), 2000.

## Nhật ký các lượt thử trước

Lượt nhiều seed trên localhost nằm trong [tham chiếu thí nghiệm](../2-cong-viec/3-tra-cuu.md#rl-nhiều-seed--compare_rl_scenariospy); dùng để thử nhanh trước khi lên cụm.

Agent Q-learning trong `src/grpc_lb/rl.py` có hai đường thử nghiệm. `scripts/train_lookaside_rl.py` chạy mô phỏng hai backend; state mô phỏng chứa danh tính backend chậm và mức tải lý tưởng, không đại diện cho telemetry thật. Model JSON của script này không được control plane đọc.

`scripts/experiment_rl_lookaside.py` chạy vòng Q-learning trực tiếp trong lab localhost: RPC qua gRPC xDS đến hai service, đo latency theo instance, đưa telemetry vào `SnapshotPublisher` và `RLController` thực, phát trọng số, đo reward và cập nhật Q. Đây là học online có exploration, chỉ dành cho lab; ADS và service được script tạo và đóng riêng. Độ trễ 1 giây sau publish là giả định, không xác nhận ACK và không đảm bảo mọi RPC đã nhận policy mới. Mẫu nhỏ và tải tuần tự không chứng minh RL cải thiện hiệu năng.

```powershell
.\.venv\Scripts\python.exe scripts/probe_weighted_xds.py
.\.venv\Scripts\python.exe scripts/train_lookaside_rl.py --episodes 1000 --seed 42 --output results/toy-rl-policy.json
.\.venv\Scripts\python.exe scripts/experiment_rl_lookaside.py --windows 20 --requests 50 --output results/rl-lookaside-local.json
.\.venv\Scripts\python.exe scripts/experiment_rl_lookaside.py --baseline --windows 20 --requests 50 --output results/rl-baseline-local.json
.\.venv\Scripts\python.exe scripts/compare_lookaside_rl.py --train-windows 6 --eval-windows 6 --requests 40 --seeds 31 32 --output-dir results/rl-comparison
```

Bộ so sánh chạy tuần tự, đổi thứ tự RL/Round Robin theo seed; RL ngừng exploration và cập nhật Q trong các cửa sổ evaluation. Dữ liệu từng request thành công được giữ trong file từng lượt, lỗi được đếm riêng. Cùng số cửa sổ warmup/train, số request và điều kiện workload; tải tuần tự không đại diện tải mở hay HA. Đây là đối chứng cục bộ để quan sát kết quả, không phải bằng chứng ưu việt tổng quát.

## Đánh giá lại 01/11/2026

### Hội tụ và các trường hợp tải — lượt bổ sung

`scripts/audit_rl_convergence.py` train Q-learning trong mô phỏng tĩnh, state gồm ba nhóm tương quan latency và 5 mức trọng số 30–70%. Reward thêm phạt lệch khi hai backend tương đương. Ba seed 7/19/31, 300.000 cập nhật/seed: policy trên 15 state giống nhau ở cả ba seed và không đổi giữa các mốc 50.000 đến 300.000; thay đổi Q lớn nhất trong 50.000 cập nhật cuối tương ứng 0,0163/0,0183/0,0176. Đây là hội tụ policy quan sát được trong mô hình tổng hợp tĩnh, không phải chứng minh hội tụ toán học hoặc hội tụ trong hệ thống thực. Dữ liệu: `results/rl-convergence-balanced-20261101/convergence.json`.

Trên gRPC, telemetry caller trước đây bị nhiễu và policy dao động hoặc không thích nghi trong thử nghiệm đổi backend chậm. Controller hiện đòi hai cửa sổ cùng nhóm trước khi đổi trọng số; collector ưu tiên thời gian server từ trailer. Khi đảo instance chậm giữa lượt 10 cửa sổ, 80 RPC/cửa sổ, trọng số (theo thứ tự địa chỉ) đổi 50/50 → 60/40 → 70/30, sau đảo trở về 60/40 → 50/50 → 40/60. Dữ liệu: `results/rl-scenarios-hysteresis-20261101/swap-server-telemetry.json`. Đây chỉ là một lượt, không chứng minh SLA hội tụ sau sự cố.

Đối chứng gRPC thật mới nhất `results/rl-scenarios-server-metric-20261101/report.json`: một lượt mỗi phương án, 8 cửa sổ, hai cửa sổ đầu warmup, 40 RPC/cửa sổ, đồng thời 8. Trung bình/P95 theo từng lượt trên 240 RPC đánh giá, ms:

| Kịch bản | Round Robin | RL | Quy tắc |
|---|---|---|---|
| Hai backend bằng nhau | 31,56 / 63 | 33,26 / 47 | 30,10 / 47 |
| Một backend chậm | 34,71 / 62 | 33,58 / 62 | 30,42 / 47 |
| Đổi backend chậm giữa lượt | 33,10 / 47 | 33,83 / 62 | 33,40 / 47 |
| Giới hạn xử lý mỗi backend còn 2 | 44,60 / 94 | 33,20 / 47 | 32,67 / 47 |

Không có lỗi trong các cửa sổ đánh giá lượt này, không có fallback. Chỉ giảm concurrency backend không chứng minh đã bão hòa: lượt này không quan sát được rejection. Trong thử nghiệm trước dùng telemetry caller, `results/rl-scenarios-20261101/report.json`, cùng kịch bản giới hạn backend có 1/4/6 lỗi tương ứng RR/RL/rule. Các lượt không lặp, không ngẫu nhiên thứ tự, timer Windows thô và thời gian có thể nhiễu; không đủ bằng chứng RL tốt hơn rule hay ổn định trên mọi mức tải. Khi hai backend bằng nhau, cả ba giữ 50/50 trong lượt mới. Trong lượt đổi backend, RL chỉ về 50/50 trước khi kết thúc 8 cửa sổ; lượt dài hơn mới thấy đảo tỷ lệ. Controller không kiểm soát được độ trễ lan truyền policy hay ACK và dùng telemetry của một caller, nên không tuyên bố ổn định trên production hoặc lúc nhiều caller/HA.

```powershell
.\.venv\Scripts\python.exe scripts/audit_rl_convergence.py --updates 300000 --checkpoint 50000 --output-dir results/rl-convergence-balanced-20261101
.\.venv\Scripts\python.exe scripts/compare_rl_scenarios.py --model results/rl-convergence-balanced-20261101/model-7.json --output-dir results/rl-scenarios-server-metric-20261101 --windows 8 --requests 40 --concurrency 8
```

Reward cũ chỉ phạt latency vượt 50 ms, nên không phân biệt phần lớn cửa sổ tốt/xấu. Bổ sung thành phần latency trung bình và giảm phạt đổi trọng số; giới hạn trọng số mỗi endpoint 30–70% để giữ mẫu quan sát. `train_observed_rl.py` huấn luyện Q-table trên mô phỏng có hai mức độ trễ và trạng thái phân nhóm tỷ lệ latency; kịch bản mô phỏng chọn nhóm dựa trên mức trễ giả định, chưa mô phỏng đầy đủ lỗi đo, queue hay mạng. Dùng model `results/rl-observed-model.json` đã đóng băng qua `EXPERIMENTAL_RL_MODEL`. Không xem policy này là đã chứng minh an toàn hoặc tổng quát.

Đối chứng gRPC xDS localhost, hai seed 41/42 lần lượt đảo instance chậm, độ trễ mô phỏng 2/30 ms, 4 cửa sổ warmup và 5 cửa sổ đánh giá, 40 RPC/cửa sổ. Agent đóng băng khi đánh giá; RR và RL chạy tuần tự theo thứ tự đảo. Có thêm chính sách quy tắc ưu tiên instance nhanh, chạy riêng sau cặp RL/RR; do chạy sau, điều kiện thời gian không được cân bằng như cặp chính. Số liệu trung bình/P95/P99, đơn vị ms:

| Seed | RL | Round Robin | Quy tắc |
|---|---|---|---|
| 41 | 23,75 / 47 / 63 | 35,16 / 63 / 125 | 24,38 / 47 / 62 |
| 42 | 27,73 / 62 / 63 | 29,14 / 47 / 62 | 24,22 / 47 / 62 |

Mỗi ô 200 request thành công, không lỗi; không có fallback trong các lượt hoàn thành. Lượt đầu trước khi giới hạn trọng số bị fallback do thiếu mẫu, được giữ tại `results/rl-observed-compare-20261101/`. Dữ liệu các lượt hoàn thành nằm tại `results/rl-observed-compare-v2-20261101/`. RL cải thiện trung bình trong cả hai lượt, nhưng P95 xấu hơn ở seed 42, còn quy tắc đạt tương đương hoặc tốt hơn RL. Các lượt ngắn trên Windows có độ phân giải timer thô và không đủ để kết luận có ý nghĩa thống kê hoặc khẳng định RL vượt quy tắc. Tình huống tối ưu là ưu tiên backend nhanh; đây không phải bằng chứng riêng cho ưu thế của RL. Cần phép thử dài và lặp trên Linux/đa máy với tải mở, thay đổi trạng thái, độ trễ xDS, nhiều caller, lỗi và đối chứng rule trước khi kết luận hữu ích.

```powershell
.\.venv\Scripts\python.exe scripts/train_observed_rl.py --episodes 12000 --seed 17 --output results/rl-observed-model.json
.\.venv\Scripts\python.exe scripts/compare_lookaside_rl.py --train-windows 4 --eval-windows 5 --requests 40 --seeds 41 42 --model results/rl-observed-model.json --output-dir results/rl-observed-compare
.\.venv\Scripts\python.exe scripts/experiment_rl_lookaside.py --rule --windows 9 --train-windows 4 --requests 40 --seed 41 --slow two --output results/rl-observed-compare/rule-41.json
.\.venv\Scripts\python.exe scripts/experiment_rl_lookaside.py --rule --windows 9 --train-windows 4 --requests 40 --seed 42 --slow one --output results/rl-observed-compare/rule-42.json
```

Kết quả localhost ngày 31/10/2026: hai seed 31/32, mỗi lượt 6 cửa sổ train/warmup rồi 6 cửa sổ đánh giá, 40 RPC/cửa sổ, hai backend delay mô phỏng 2 ms và 15 ms. RL seed 31/32 có latency trung bình 24,61/24,93 ms; Round Robin tương ứng 22,65/21,68 ms. P95 cả bốn lượt là 32 ms; P99 RL 47/47 ms, baseline 32/32 ms. Tất cả 960 RPC đánh giá thành công. Trong thử nghiệm ngắn này RL **không tốt hơn**; kết quả không chứng minh hiệu quả trên workload khác. Chi tiết và dữ liệu từng request: `results/rl-collector-comparison-20261031/report.json` và các file cùng thư mục. Windows có độ phân giải hẹn giờ thô nên nhiều latency rơi vào bội số gần 16 ms.

Stack Compose một host cũ có overlay `deploy/compose.lookaside-rl-lab.yaml` (đã xóa khỏi cây thư mục, còn trong lịch sử git; xem `docs/history/README.md`), thêm vào hai file microservices và lookaside. Overlay chỉ cho A-1 ghi telemetry của service-b vào volume chung với control plane; A-2 không ghi, do đó dữ liệu không đại diện toàn bộ caller. Docker chưa được xác minh trên máy hiện tại.

Probe khởi động hai service và ADS riêng trên localhost, phát hai locality 8:2 rồi 2:8 và đếm 200 RPC mỗi lượt. Nó không kiểm chứng HA, nhiều caller, membership thay đổi hay thời gian hội tụ; không dùng probe này làm bằng chứng production. Helper `weighted_snapshot` yêu cầu trọng số khớp chính xác membership đã khám phá, không tự thêm endpoint.

Control plane có thể bật thử nghiệm bằng `EXPERIMENTAL_RL_TELEMETRY` trỏ tới file JSON window và `EXPERIMENTAL_RL_SERVICE` là tên service. Cả hai phải được thiết lập cùng nhau. Một caller xDS có thể thu RPC thật bằng `EXPERIMENTAL_RL_TELEMETRY_OUTPUT` và cùng `EXPERIMENTAL_RL_SERVICE`; cần mount thư mục chung giữa caller và control plane và chỉ cho một caller ghi file. Collector dùng `instance_id` trong response và topology để ánh xạ địa chỉ; mỗi tên phải resolve duy nhất một IP. Từ 01/11, collector ưu tiên thời gian xử lý server trong trailer gRPC, nếu không có trailer hợp lệ dùng thời gian RPC từ caller; do đó hai loại đo có thể bị trộn và đây không phải end-to-end latency toàn hệ thống. Từ 11/11, service gắn trailer `x-instance-id` cả khi trả lỗi; collector ghi lỗi theo endpoint vào trường `endpoint_errors`, và controller tính latency hiệu dụng = median / tỷ lệ thành công. Lỗi không có trailer (mất kết nối, hết deadline phía caller) vẫn không quy được cho endpoint. Khi tổng lỗi vượt 10% controller vẫn quay về Round Robin. Window mặc định 10 giây. File cần `timestamp` Unix giây, `sequence` tăng dần, `service`, `offered`, `errors`, `endpoints` ánh xạ địa chỉ IP:port đã resolve sang danh sách latency ms. Cần ít nhất ba mẫu thành công cho mỗi endpoint; dữ liệu quá 15 giây, lỗi vượt 10%, membership không khớp hoặc không đủ hai endpoint khiến snapshot trở về Round Robin. Writer ghi file tạm rồi đổi tên nguyên tử. Q-table trong bộ nhớ mất khi restart; các replica control plane không đồng bộ. Chỉ bật trên một control plane và một caller lab, không bật trong HA hoặc production.

Trước khi dùng RL với traffic thực, cần thay state lý tưởng bằng telemetry có timestamp và số mẫu; thiết kế reward theo offered/goodput/timeout; train và đánh giá theo seed/workload tách biệt với Round Robin và policy theo ngưỡng; kiểm tra xDS ACK và thời điểm caller thực sự áp dụng; thêm policy controller có giới hạn, lease, xử lý membership, rollback và cơ chế phối hợp giữa các replica control plane. Tuyệt đối không chạy exploration trên deployment mặc định.

Nguồn tham khảo: [gRPC A27](https://github.com/grpc/proposal/blob/master/A27-xds-global-load-balancing.md) mô tả trọng số locality và việc bỏ qua trọng số endpoint; [LoadBalancerAutoScaler-DRL](https://github.com/MartinFarres/LoadBalancerAutoScaler-DRL) tham khảo cách phân tách môi trường, reward và đánh giá (HAProxy, không phải gRPC). Implementation Q-learning ở đây được viết riêng, không chép code của các nguồn đó.
