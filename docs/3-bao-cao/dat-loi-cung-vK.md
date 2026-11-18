# Mục 4 — Lỗi cứng (vK)

Bản vK đo độc lập trên cụm AWS mới, với D2 chạy cục bộ trên máy Khoa. [Bản gốc của Đạt](dat-loi-cung.md).

## Nhật ký lượt chạy

Mỗi lượt một dòng, kể cả lượt hỏng.

| Ngày | Mã | Lệnh (rút gọn) | Thư mục kết quả | `release` | Ghi chú |
|---|---|---|---|---|---|
| | D1 | | | | |
| | D2 | | | — | |

## 1. Dự đoán (commit trước khi đo)

| Tình huống | Ai phát hiện | Theo cơ chế / timeout nào | Dự đoán gián đoạn (s) | Request lỗi mang mã gì |
|---|---|---|---|---|
| `backend-stop` | Control plane | Probe gRPC sức khỏe b-2 | 0–5 | UNAVAILABLE hoặc DEADLINE_EXCEEDED |
| `controlplane-stop` | Caller xDS | ADS mất kết nối và thử control plane còn lại | 0–15 | UNAVAILABLE hoặc DEADLINE_EXCEEDED |
| `consul-leader` | Consul | Raft bầu lại với hai voter còn lại | 0–10 | Có thể không có lỗi RPC |
| `quorum-loss` | Control plane | Lease của registry hết hạn sau 15 s | Có thể ngừng tới khi khôi phục | UNAVAILABLE sau khi hết lease |
| `node3-down` | Control plane + Consul | Probe b-2/c-2; quorum còn 2/3 | 0–10 | UNAVAILABLE hoặc DEADLINE_EXCEEDED |
| `node1-down` | Bộ phát tải + Envoy còn lại | Kết nối gRPC đứt; thử đường vào Envoy node2; probe a-1 | 0–20 | UNAVAILABLE hoặc DEADLINE_EXCEEDED |
| `partition-node3` | Control plane + Consul | Probe sang node3 mất đáp ứng; quorum còn 2/3 | 0–15 | DEADLINE_EXCEEDED trong pha chuyển tiếp |
| `partition-node1` | Raft, Envoy và caller xDS | Node1 tách khỏi đa số; probe giữa node không tới; lease có thể hết | Quan sát, có thể kéo dài tới khôi phục | DEADLINE_EXCEEDED rồi có thể UNAVAILABLE |

## 2. Kết quả D1 trên baseline

- Thư mục kết quả:
- Giá trị `release`:
- Đã tự đếm lại lần lặp nào, có khớp bảng không:

Dán bảng từ `report.md`:

| Tình huống | Lần lặp | Gián đoạn (s) | Lỗi trong lúc lỗi (%) | P95 OK (ms) | Phục hồi (s) | Instance còn phục vụ | Kết luận |
|---|---:|---:|---:|---:|---:|---|---|
| `backend-stop` | | | | | | | |
| `controlplane-stop` | | | | | | | |
| `consul-leader` | | | | | | | |
| `quorum-loss` | | | | | | | |
| `node3-down` | | | | | | | |
| `node1-down` | | | | | | | |
| `partition-node3` | | | | | | | |
| `partition-node1` | | | | | | | |

## 3. Dự đoán so với số đo, và giải thích

| Tình huống | Dự đoán | Đo được | Lệch vì sao |
|---|---|---|---|
| | | | |

Giải thích kỹ (mỗi tình huống một đoạn: ai phát hiện, theo cơ chế nào, vì sao ra con số đó, tải chuyển sang đâu):

### `backend-stop`

### `node1-down`

### `quorum-loss`

### `partition-node1`

### Bốn tình huống còn lại (mỗi cái vài dòng)

## 4. Cặp trước / sau

- Ô chọn để sửa, và vì sao chọn:
- Phân loại: lỗi hệ thống, lỗi công cụ đo, hay tiêu chí chưa hợp lý:
- Giả thuyết và bằng chứng (log, trace):
- Thay đổi đã làm (nhánh, commit):
- Kết quả trước (thư mục, số đo):
- Kết quả sau (thư mục, `release`, số đo):
- Kết luận. Nếu không sửa được: đã thử gì, vì sao dừng:

## 5. Giới hạn của phép đo

## 6. Số liệu D2 — RL trên gRPC thật (chạy hộ phần RL)

Chỉ ghi số liệu và quan sát; phần diễn giải do Khoa viết ở `khoa-rl.md`.

- Thư mục kết quả:
- Máy chạy (CPU, RAM), có chạy việc khác cùng lúc không:
- Số lượt bị loại vì máy đo nghẽn (ghi trong `report.md`):

Dán bảng mean / P95 / P99 / lỗi từ `report.md`:

Dán bảng hiệu RL trừ từng đối thủ theo từng lần lặp:

Điều thấy lạ khi chạy (lỗi, cảnh báo, lượt dao động mạnh):

## 7. Trả lời câu hỏi của nhánh

Khi backend, node, control plane, Consul hoặc mạng gặp sự cố, hệ thống gián đoạn bao lâu và tự phục hồi thế nào?
