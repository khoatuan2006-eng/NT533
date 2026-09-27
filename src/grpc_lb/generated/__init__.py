from __future__ import annotations

import sys
from pathlib import Path

# Add current generated directory to sys.path so workload_pb2_grpc can import workload_pb2
_generated_dir = str(Path(__file__).parent.resolve())
if _generated_dir not in sys.path:
    sys.path.insert(0, _generated_dir)

from . import workload_pb2
from . import workload_pb2_grpc

__all__ = ["workload_pb2", "workload_pb2_grpc"]
