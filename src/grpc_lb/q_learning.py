from __future__ import annotations

import json
import os
import random
from pathlib import Path
from typing import Any, Mapping, Sequence


class QLearningAgent:
    """Agent Q-learning điều khiển phân phối tải thích ứng cho gRPC Client.

    Tuân thủ hợp đồng M2:
      - Quản lý bảng Q-table dạng state -> vector Q-values.
      - Hành động (Actions):
          * Action 0: Phân phối đều (Equal: 1:1:1:...)
          * Action i+1: Ưu tiên backend i (ví dụ tỉ lệ boost 3:1:1:...)
      - Lựa chọn hành động theo chiến lược epsilon-greedy với seed cố định được.
      - Cập nhật giá trị Q theo phương trình Bellman:
          Q(s, a) <- Q(s, a) + alpha * [r + gamma * max_a' Q(s', a') - Q(s, a)]
      - Lưu / nạp checkpoint JSON kèm xác thực phiên bản schema và danh sách backend.
    """

    SCHEMA_VERSION = "1.0"

    def __init__(
        self,
        backends: Sequence[str],
        alpha: float = 0.1,
        gamma: float = 0.9,
        epsilon: float = 0.1,
        seed: int | None = None,
        boost_weight: float = 3.0,
        base_weight: float = 1.0,
    ) -> None:
        if not backends:
            raise ValueError("Backends sequence cannot be empty")
        if not (0.0 < alpha <= 1.0):
            raise ValueError(f"Alpha (learning rate) must be in (0, 1], got {alpha}")
        if not (0.0 <= gamma <= 1.0):
            raise ValueError(f"Gamma (discount factor) must be in [0, 1], got {gamma}")
        if not (0.0 <= epsilon <= 1.0):
            raise ValueError(f"Epsilon must be in [0, 1], got {epsilon}")

        self._backends: list[str] = list(backends)
        self.alpha = float(alpha)
        self.gamma = float(gamma)
        self.epsilon = float(epsilon)
        self.boost_weight = float(boost_weight)
        self.base_weight = float(base_weight)
        self._seed = seed
        self._rng = random.Random(seed)

        # Số hành động: 1 hành động cân bằng đều + N hành động ưu tiên từng backend
        self._num_actions = len(self._backends) + 1

        # Bảng Q-table: key (serialized state) -> list[float] kích thước self._num_actions
        self._q_table: dict[str, list[float]] = {}

    @property
    def backends(self) -> list[str]:
        """Danh sách các backend ID theo thứ tự định nghĩa."""
        return list(self._backends)

    @property
    def num_actions(self) -> int:
        """Tổng số hành động có thể chọn."""
        return self._num_actions

    def _state_key(self, state: Any) -> str:
        """Chuyển đổi trạng thái (tuple, list, str, dict) thành chuỗi key chuẩn hóa."""
        if isinstance(state, str):
            return state
        try:
            return json.dumps(state, sort_keys=True)
        except (TypeError, ValueError):
            return str(state)

    def _get_q_values(self, state_key: str) -> list[float]:
        """Lấy vector Q cho một trạng thái, khởi tạo 0.0 nếu chưa tồn tại."""
        if state_key not in self._q_table:
            self._q_table[state_key] = [0.0] * self._num_actions
        return self._q_table[state_key]

    def get_action_weights(self, action_id: int) -> dict[str, float]:
        """Chuyển đổi một action_id thành ánh xạ trọng số backend_id -> float.

        Args:
            action_id: 0 cho cân bằng đều; 1..N ưu tiên backend tương ứng.

        Returns:
            dict[str, float]: Bộ trọng số danh định tương ứng với hành động.
        """
        if not (0 <= action_id < self._num_actions):
            raise ValueError(
                f"Action ID {action_id} out of bounds (0 <= action < {self._num_actions})"
            )

        if action_id == 0:
            # Action 0: Cân bằng đều tất cả các backend
            return {b: self.base_weight for b in self._backends}

        # Action 1..N: Ưu tiên backend tại vị trí action_id - 1
        favored_idx = action_id - 1
        weights: dict[str, float] = {}
        for idx, b in enumerate(self._backends):
            weights[b] = self.boost_weight if idx == favored_idx else self.base_weight
        return weights

    def choose(self, state: Any, epsilon: float | None = None) -> int:
        """Chọn hành động tiếp theo theo chiến lược epsilon-greedy.

        Args:
            state: Trạng thái hiện tại của hệ thống (tuple, string hoặc đối tượng rời rạc).
            epsilon: Tỉ lệ khám phá (tuỳ chọn ghi đè epsilon mặc định).

        Returns:
            action_id (int): ID hành động được chọn trong đoạn [0, num_actions - 1].
        """
        eps = self.epsilon if epsilon is None else float(epsilon)
        key = self._state_key(state)
        q_vals = self._get_q_values(key)

        # Khám phá (Exploration)
        if self._rng.random() < eps:
            return self._rng.randint(0, self._num_actions - 1)

        # Khai thác (Exploitation): Chọn hành động có Q-value lớn nhất
        max_q = max(q_vals)
        # Tìm tất cả hành động đạt giá trị max để phá hòa (tie-breaking)
        best_actions = [a for a, q in enumerate(q_vals) if abs(q - max_q) < 1e-12]
        if len(best_actions) == 1:
            return best_actions[0]
        return self._rng.choice(best_actions)

    def update(self, state: Any, action: int, reward: float, next_state: Any) -> float:
        """Cập nhật giá trị Q theo phương trình Bellman.

        Args:
            state: Trạng thái trước khi thực hiện hành động.
            action: Hành động đã thực hiện (action_id).
            reward: Điểm thưởng nhận được.
            next_state: Trạng thái kế tiếp sau khi chuyển tiếp.

        Returns:
            float: Giá trị Q(s, a) mới sau khi cập nhật.
        """
        if not (0 <= action < self._num_actions):
            raise ValueError(
                f"Invalid action {action}, must be in [0, {self._num_actions - 1}]"
            )

        s_key = self._state_key(state)
        ns_key = self._state_key(next_state)

        current_q = self._get_q_values(s_key)[action]
        next_max_q = max(self._get_q_values(ns_key))

        # Công thức cập nhật Bellman
        td_target = reward + self.gamma * next_max_q
        td_error = td_target - current_q
        new_q = current_q + self.alpha * td_error

        self._q_table[s_key][action] = new_q
        return new_q

    def get_q_table(self) -> dict[str, list[float]]:
        """Trả về bản sao bảng Q-table hiện tại."""
        return {k: list(v) for k, v in self._q_table.items()}

    def get_action_definitions(self) -> list[dict[str, Any]]:
        """Mô tả chi tiết các hành động để phục vụ log và kiểm tra."""
        definitions = []
        for a in range(self._num_actions):
            definitions.append({
                "action_id": a,
                "description": "Equal distribution" if a == 0 else f"Boost {self._backends[a - 1]}",
                "weights": self.get_action_weights(a),
            })
        return definitions

    def save_checkpoint(self, filepath: str | Path) -> None:
        """Lưu toàn bộ trạng thái học của Agent ra file checkpoint JSON.

        Ghi nguyên tử (atomic write qua file tạm) để chống hỏng file nếu có sự cố.
        """
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "backends": self._backends,
            "num_actions": self._num_actions,
            "action_definitions": self.get_action_definitions(),
            "hyperparameters": {
                "alpha": self.alpha,
                "gamma": self.gamma,
                "epsilon": self.epsilon,
                "seed": self._seed,
                "boost_weight": self.boost_weight,
                "base_weight": self.base_weight,
            },
            "q_table": self._q_table,
        }

        temp_path = path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(temp_path, path)

    def load_checkpoint(self, filepath: str | Path) -> None:
        """Nạp lại trạng thái học từ file checkpoint JSON.

        Raises:
            ValueError: Nếu file không tồn tại, phiên bản schema không khớp,
                        hoặc danh sách backends không trùng khớp thứ tự.
        """
        path = Path(filepath)
        if not path.is_file():
            raise FileNotFoundError(f"Checkpoint file not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 1. Xác thực schema_version
        version = data.get("schema_version")
        if version != self.SCHEMA_VERSION:
            raise ValueError(
                f"Incompatible schema version: expected {self.SCHEMA_VERSION!r}, got {version!r}"
            )

        # 2. Xác thực danh sách backend
        loaded_backends = data.get("backends")
        if loaded_backends != self._backends:
            raise ValueError(
                f"Incompatible backends: expected {self._backends!r}, got {loaded_backends!r}"
            )

        # 3. Xác thực số lượng hành động
        loaded_actions = data.get("num_actions")
        if loaded_actions != self._num_actions:
            raise ValueError(
                f"Incompatible action count: expected {self._num_actions}, got {loaded_actions}"
            )

        # 4. Nạp bảng Q-table
        raw_table = data.get("q_table", {})
        loaded_table: dict[str, list[float]] = {}
        for k, v in raw_table.items():
            if not isinstance(v, list) or len(v) != self._num_actions:
                raise ValueError(
                    f"Malformed Q-value list for state {k!r}: expected length {self._num_actions}"
                )
            loaded_table[str(k)] = [float(x) for x in v]

        self._q_table = loaded_table
