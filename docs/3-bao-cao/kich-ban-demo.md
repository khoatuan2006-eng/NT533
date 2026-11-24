# Kịch bản demo (khoảng 8 phút)

Demo chứng minh bốn điều, theo đúng thứ tự:

1. Hệ thống chia tải hai tầng hoạt động: Envoy chọn A, A chọn B.
2. Bài toán có thật: b-2 chậm nhưng vẫn khỏe, Round Robin vẫn gửi 25% tải vào nó.
3. Chia tải theo độ trễ giải quyết được: β = 2 và RL đẩy b-2 về khoảng 5%.
4. Hệ thống chịu lỗi: tắt một bản sao B, request vẫn thành công.

## Phân vai

| Vai | Người | Việc |
|---|---|---|
| Người nói | Lầu | Đứng cạnh màn chiếu, nói theo lời thoại bên dưới |
| Người gõ lệnh | Khoa | Ngồi máy, gõ đúng lệnh khi người nói ra hiệu "em chạy lệnh" |
| Người chỉ | Đạt | Dùng chuột hoặc bút laser chỉ vào đúng panel Grafana mỗi bước; nói phần giải thích ở bước 6 |

## Chuẩn bị (30 phút trước, không làm trước mặt thầy)

Trong cả tài liệu, `labenv` là viết tắt của:

```powershell
python scripts/labenv.py --env results/aws-4node-4a4b/lab.json
```

1. Bật 4 máy ảo trên AWS. IP công khai đổi mỗi lần bật máy, nên cập nhật lại `results/aws-4node-4a4b/lab.json`: trường `ssh` của node1 và node2 là địa chỉ hai Envoy, của node4 là máy chạy Grafana.
2. Đưa cụm về trạng thái gốc: `labenv heal`, rồi `labenv up --variant xds`.
3. Mở đường hầm tới Grafana (giữ cửa sổ này mở suốt buổi):

   ```powershell
   ssh -i <khóa.pem> -N -L 13000:127.0.0.1:3000 ubuntu@<IP node4>
   ```

4. Mở `http://localhost:13000`, vào dashboard **grpc-lb-overview**. Chọn khung thời gian **Last 15 minutes**, tự làm mới **5s**. Cuộn tới hai hàng:
   - **Phân phối tải tầng 2 (Service A -> Service B)**, panel **Tỷ lệ phân phối tải Service B (%)**.
   - **Bộ điều khiển trọng số A -> B (β cố định / RL)**, gồm **β đang áp dụng**, **Trạng thái bộ điều khiển** và **Trọng số control plane giao cho từng bản sao B (%)**.
5. Màn hình chia đôi: bên trái là terminal 1 (chạy `demo_client.py`), bên phải là Grafana. Terminal 2 dùng để gõ lệnh `labenv`, đặt sẵn phía sau.
6. Chạy thử toàn bộ kịch bản một lần, bấm giờ từng bước.
7. Dự phòng: mở sẵn video quay lượt chạy thử và ảnh Hình 4.1, 4.2 của báo cáo. AWS hoặc mạng lỗi thì chuyển sang chiếu video, không sửa lỗi trước mặt thầy.

## Bước 1. Hệ thống đang chạy bình thường (1 phút)

**Gõ (terminal 1):**

```powershell
python scripts/demo_client.py --targets <IP node1>:80 <IP node2>:80 --rate 5
```

**Màn hình hiện:** mỗi giây khoảng 5 dòng, mỗi dòng là một request, ví dụ:

```text
10:02:11 laptop #12  qua 3.27.239.78:80      182.4 ms  a-3 → b-1
10:02:11 laptop #13  qua 13.239.63.219:80     179.9 ms  a-1 → b-4
```

Vài giây một lần có dòng tổng kết `tầng B: b-1 25%  b-2 25%  b-3 25%  b-4 25%`.

**Chỉ vào:** cột `qua ...` (đổi giữa hai Envoy), phần `a-x → b-y` (đổi liên tục). Bên Grafana: panel **Tỷ lệ phân phối tải Service B (%)**, bốn đường đi ngang quanh 25%.

**Lời nói:**
> "Đây là client gửi 5 request mỗi giây qua Internet vào cụm. Mỗi dòng cho biết request đi qua Envoy nào, bản sao A nào, bản sao B nào. Mỗi request được cân bằng tải hai lần: Envoy chọn A, rồi A tự chọn B theo trọng số mà control plane gửi xuống qua xDS. Bên phải là Grafana: bốn bản sao B đang nhận đều, mỗi bản 25%. Độ trễ khoảng 180 ms là do đường mạng từ đây tới AWS."

## Bước 2. Tạo bản sao chậm: bài toán của đề tài (1,5 phút)

**Lời nói trước khi gõ:**
> "Bây giờ em làm b-2 chậm thêm 200 ms, nhưng b-2 vẫn chạy và vẫn trả lời kiểm tra sức khỏe. Thầy để ý đường b-2 trên Grafana: nó sẽ **không** giảm."

**Gõ (terminal 2):**

```powershell
labenv degrade-instance b-2 --delay-ms 200
```

**Màn hình hiện:** trong terminal 1, các dòng có `→ b-2` chuyển màu vàng, độ trễ khoảng 380 ms thay vì 180 ms. Dòng tổng kết vẫn ghi `b-2 25%`.

**Chỉ vào:** một dòng vàng có `b-2`. Panel **Tỷ lệ phân phối tải Service B (%)**: đường b-2 vẫn nằm ở 25%.

**Lời nói:**
> "b-2 đã chậm gấp đôi, nhưng kiểm tra sức khỏe vẫn báo nó khỏe, và Round Robin vẫn gửi cho nó 25%. Cứ 4 request thì 1 request bị chậm. Đây chính là bài toán: bản sao chậm nhưng không chết."

## Bước 3. Bật chia tải theo độ trễ, β = 2 (1,5 phút)

**Lời nói trước khi gõ:**
> "Em chuyển A sang chia tải theo độ trễ. Control plane đọc độ trễ của từng B mỗi 10 giây và tính trọng số theo công thức, β cố định bằng 2."

**Gõ (terminal 2):**

```powershell
labenv up --variant xds-beta
```

**Chờ khoảng 1 phút.** Trong lúc chờ, người nói giải thích công thức (slide β).

**Màn hình hiện:** panel **β đang áp dụng** hiện 2. Panel **Trọng số control plane giao cho từng bản sao B (%)**: đường b-2 đi xuống từng bậc, mỗi 10 giây tối đa 10 điểm, rồi nằm ở khoảng 5%. Ba bản sao còn lại tăng lên khoảng 31–32%. Panel **Tỷ lệ phân phối tải Service B (%)** đi theo cùng hình dạng. Trong terminal 1 gần như không còn dòng vàng.

**Lời nói:**
> "Phần tải của b-2 giảm dần chứ không rơi thẳng, vì mỗi lần trọng số chỉ được đổi 10 điểm, để hệ thống không dao động. Nó dừng ở 5% chứ không về 0, vì nhóm giữ sàn 5% để vẫn đo được b-2 và biết khi nào nó hồi phục. Panel trên là trọng số control plane ra lệnh, panel dưới là tải thực tế. Hai đường trùng nhau, tức A làm đúng theo trọng số."

## Bước 4. Thay β cố định bằng RL (1,5 phút)

**Lời nói trước khi gõ:**
> "Bây giờ thay β cố định bằng bộ chọn RL. RL không chọn trọng số, nó chỉ chọn β, dựa trên bảng đã học trước trong mô phỏng."

**Gõ (terminal 2):**

```powershell
labenv up --variant xds-rl
```

**Màn hình hiện:** panel **Trạng thái bộ điều khiển** hiện RL đang chạy. Panel **β đang áp dụng** hiện β do RL chọn. b-2 vẫn ở khoảng 5%.

**Chỉ vào:** panel β, rồi đường b-2.

**Lời nói:**
> "RL chọn β theo tình huống, và kết quả ngang β = 2. Đây cũng là kết quả đo trong báo cáo: 5,4% so với 5,5%. Lý do là b-2 chậm hơn rất nhiều lần, nên β nào lớn hơn 0 cũng đẩy nó về sàn 5%. RL chỉ có cơ hội hơn khi bản sao chậm nhẹ hoặc lúc chậm lúc không. Đó là hướng phát triển của nhóm."

## Bước 5. Gỡ lỗi, quay về Round Robin (45 giây)

**Gõ (terminal 2):**

```powershell
labenv heal
labenv up --variant xds
```

**Màn hình hiện:** các dòng b-2 hết vàng, bốn đường B quay về khoảng 25%.

**Lời nói:**
> "Gỡ độ trễ của b-2 và quay về Round Robin. Tải chia đều trở lại."

## Bước 6. Tắt một bản sao: phát hiện dịch vụ và chịu lỗi (1,5 phút)

Bước này làm **sau** bước 5, khi đang ở Round Robin. Nếu làm lúc RL đang chạy, danh sách B thay đổi làm RL tự quay về chia đều (đây là rào an toàn), và đường b-2 sẽ nhảy lên giữa chừng, gây khó hiểu.

**Lời nói trước khi gõ:**
> "Cuối cùng em tắt hẳn b-3. Thầy để ý terminal: request vẫn thành công, chỉ là không còn đi vào b-3."

**Gõ (terminal 2):**

```powershell
labenv stop b-3
```

**Màn hình hiện:** sau vài giây, không còn dòng nào có `→ b-3`. Dòng tổng kết chỉ còn b-1, b-2, b-4, mỗi bản khoảng 33%, không có lỗi. Grafana: đường b-3 về 0, ba đường còn lại lên khoảng 33%.

**Lời nói:**
> "Control plane kiểm tra sức khỏe B mỗi giây, hai lần lỗi liên tiếp thì rút b-3 khỏi danh sách và gửi danh sách mới cho A qua xDS. Các request vẫn được phục vụ bởi ba bản sao còn lại."

Nếu đúng lúc tắt có một vài request lỗi (request đang đi dở vào b-3), nói thẳng: "Vài request đang xử lý dở ở b-3 bị lỗi, sau vài giây thì không còn lỗi nữa."

**Gõ (terminal 2):**

```powershell
labenv start b-3
```

**Lời nói:**
> "Bật lại b-3. Nó tự đăng ký với Consul và nhận tải trở lại."

Dừng `demo_client.py` bằng Ctrl+C. Terminal in bảng tổng kết: số request, số lỗi (kỳ vọng là 0), phân phối tải. Chỉ vào dòng số lỗi rồi kết thúc:

> "Đó là toàn bộ phần trình diễn. Em xin mời thầy đặt câu hỏi."

## Nếu không như mong đợi

| Hiện tượng | Xử lý |
|---|---|
| `demo_client.py` báo lỗi kết nối ngay từ đầu | IP đổi sau khi bật máy: sửa lại `--targets` theo `lab.json`. |
| Grafana trống | Đường hầm SSH đã tắt: chạy lại lệnh ở mục Chuẩn bị, bước 3. |
| Ở bước 3, sau 2 phút b-2 vẫn không giảm | Xem panel **Trạng thái bộ điều khiển**. Nếu đang ở chế độ dự phòng thì nói: "Bộ điều khiển đang ở chế độ an toàn vì thiếu số đo, nó tự quay về chia đều." Sau đó chuyển sang video. |
| Bất kỳ lỗi nào không sửa được trong 30 giây | Chuyển sang video dự phòng, không gỡ lỗi trước mặt thầy. |

Số trên terminal chỉ để quan sát: client chạy trên laptop qua Internet. Số liệu dùng để kết luận là 36 lượt đo trong báo cáo.
