from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, TextIO

from .contracts import CallResult


def result_to_dict(result: CallResult) -> dict[str, Any]:
    """Chuyển đổi đối tượng CallResult thành dictionary chuẩn hóa 10 trường theo M1.md.
    
    Đảm bảo định dạng nhất quán, không tự suy đoán lại trạng thái và tương thích với JSON.
    """
    status_val = result.status.value if hasattr(result.status, "value") else str(result.status)
    return {
        "run_id": result.run_id,
        "request_id": result.request_id,
        "policy": result.policy,
        "backend_id": result.backend_id,
        "started_at_unix_ns": result.started_at_unix_ns,
        "latency_ms": result.latency_ms,
        "status": status_val,
        "grpc_code": result.grpc_code,
        "error_message": result.error_message,
        "schema_version": result.schema_version,
    }


class TelemetryWriter:
    """Bộ ghi dữ liệu telemetry ra file JSON Lines (JSONL).
    
    Vai trò:
    - Nhận CallResult từ các đợt phát tải và ghi nối tiếp (append-only) ra file.
    - Tự động tạo thư mục cha nếu chưa tồn tại.
    - Hỗ trợ buffer và flush an toàn.
    - Hỗ trợ cú pháp context manager: with TelemetryWriter(...) as writer:
    """

    def __init__(self, file_path: str | Path) -> None:
        self._path = Path(file_path)
        # Tự động tạo thư mục cha nếu chưa có (ví dụ: results/ hoặc results/<run_id>/)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file: TextIO | None = self._path.open("a", encoding="utf-8")
        self._total_recorded = 0

    @property
    def file_path(self) -> Path:
        """Đường dẫn tới file JSONL đang ghi."""
        return self._path

    @property
    def total_recorded(self) -> int:
        """Tổng số dòng log đã được ghi nhận."""
        return self._total_recorded

    def record(self, result: CallResult) -> None:
        """Ghi 1 đối tượng CallResult thành 1 dòng JSONL."""
        if self._file is None:
            raise ValueError(f"TelemetryWriter đã bị đóng, không thể ghi vào {self._path}")
        
        data = result_to_dict(result)
        line = json.dumps(data, ensure_ascii=False)
        self._file.write(line + "\n")
        self._total_recorded += 1

    def record_many(self, results: Iterable[CallResult]) -> None:
        """Ghi hàng loạt kết quả CallResult liên tục."""
        for res in results:
            self.record(res)

    def flush(self) -> None:
        """Đẩy toàn bộ dữ liệu còn trong bộ đệm (buffer) xuống đĩa cứng."""
        if self._file is not None and not self._file.closed:
            self._file.flush()

    def close(self) -> None:
        """Xả bộ đệm và đóng file log."""
        if self._file is not None:
            if not self._file.closed:
                self._file.flush()
                self._file.close()
            self._file = None

    def __enter__(self) -> TelemetryWriter:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


def read_telemetry(file_path: str | Path) -> list[dict[str, Any]]:
    """Hàm tiện ích đọc lại toàn bộ file JSONL thành danh sách các dictionary.
    
    Phục vụ cho module summarize.py tổng hợp và cho các bộ unit test.
    Bỏ qua các dòng trống (nếu có).
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Không tìm thấy file telemetry: {path}")

    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                records.append(record)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Dòng {line_num} trong file {path} không phải JSON hợp lệ: {exc}"
                ) from exc

    return records
