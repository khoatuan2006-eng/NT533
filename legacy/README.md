# Mã của các giai đoạn trước

Thư mục này giữ nguyên trạng các script và file Compose của những đợt đã qua: M1 (benchmark, recovery), nghiệm thu một host, điều tra deadline và HA, nghiệm thu và thử lỗi trên cloud ngày 28/10. Chúng **không dùng trong cách làm hiện tại** và được chuyển ra khỏi `scripts/` và `deploy/` để khỏi lẫn với công cụ đang dùng.

Các file được giữ **đúng như lúc chuyển**, không sửa đường dẫn bên trong; nhiều file tham chiếu `scripts/...` và `deploy/...` theo vị trí cũ, nên **không chạy trực tiếp từ đây**. Muốn chạy lại một lệnh trong [tài liệu lịch sử](../docs/history/README.md), lấy bản repo trước khi dọn:

```powershell
git worktree add ..\LoadBalance-history 4d0a876
```

rồi chạy lệnh đó trong thư mục `..\LoadBalance-history`.

| Nhóm | File |
|---|---|
| M1 | `scripts/benchmark.py`, `scripts/recovery.py`, `deploy/compose.yaml`, `deploy/compose.slow.yaml`, `deploy/envoy.yaml` |
| Ingress, một host | `scripts/check_ingress.py`, `deploy/compose.ingress.yaml` |
| Nghiệm thu một host | `scripts/acceptance_microservices.py`, `scripts/acceptance_lookaside.py`, `scripts/acceptance_lookaside_ha.py`, `scripts/probe_load.py`, `scripts/probe_xds.py`, `scripts/check_xds_cold.py`, `deploy/compose.microservices.yaml`, `deploy/compose.lookaside.yaml`, `deploy/compose.lookaside-ha.yaml`, `deploy/compose.lookaside-rl-lab.yaml` |
| Điều tra deadline, HA | `scripts/investigate_deadline.py`, `scripts/investigate_ha.py`, `scripts/probe_ha_timeline.py` |
| Cloud 28/10 | `scripts/acceptance_cloud.py`, `scripts/cloud_api.py`, `scripts/cloud_audit.py`, `scripts/cloud_mode.py`, `scripts/controlled_suite.py`, `scripts/fault_probe.py`, `scripts/report_controlled.py`, `scripts/report_faults.py` |

Thay thế hiện tại: thử lỗi dùng `scripts/run_faults.py`, đo tải dùng `scripts/run_load.py`, cả hai qua `scripts/labenv.py` (xem [hướng dẫn cho nhóm](../docs/team-guide.md)).
