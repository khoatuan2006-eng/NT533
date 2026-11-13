# Mục 1–3 và 7 — Phần chung

Mục 1–3 do Khoa viết. Mục 7 mỗi người viết một đoạn cho nhánh mình; Lầu ghép.

## 1. Giới thiệu và câu hỏi của đồ án

Nguồn: [de-tai.md](../1-do-an/de-tai.md), mục "Câu hỏi" và "Khái niệm hệ phân tán được thể hiện".

## 2. Kiến trúc

Nguồn: [kien-truc.md](../1-do-an/kien-truc.md). Cần có: hai tầng cân bằng tải, đường điều khiển (Consul, control plane, xDS), bố trí ba node, hành vi fail closed.

## 3. Phương pháp đo

Nguồn: [tra-cuu.md](../2-cong-viec/tra-cuu.md). Cần có:

- Môi trường: ba VM, bộ phát tải ngoài cụm qua VPN.
- Công cụ: cách gây lỗi, cách phát tải, cách tính gián đoạn và phần b-2.
- Tiêu chí đạt của bài lỗi và bài tải.
- Baseline: giá trị `release` mà mọi bảng "trước" đo trên đó.
- Giới hạn chung: VM dùng chung tài nguyên, "node chết" là dừng container, đo qua VPN.

Giá trị `release` của baseline:

## 7. Kết luận và hướng phát triển

### Lỗi cứng (Đạt)

### Lỗi mềm: Round Robin và Least Request (Lầu)

### Lỗi mềm: RL (Khoa)

### Hướng phát triển

Nguồn: mục "Ngoài phạm vi" trong [de-tai.md](../1-do-an/de-tai.md) và mục "Giới hạn" của từng phần.
