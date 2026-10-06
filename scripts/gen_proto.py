"""
gen_proto.py — Sinh mã Python từ api/workload.proto

Chạy từ gốc repo:
    python scripts/gen_proto.py

Yêu cầu: grpcio-tools được cài (nằm trong [project.optional-dependencies.dev])
    python -m pip install -e ".[dev]"
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
PROTO_DIR = REPO_ROOT / "api"
OUT_DIR = REPO_ROOT / "src" / "grpc_lb" / "generated"
PROTO_FILES = ["workload.proto"]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"-I{PROTO_DIR}",
        f"--python_out={OUT_DIR}",
        f"--grpc_python_out={OUT_DIR}",
    ] + [str(PROTO_DIR / f) for f in PROTO_FILES]

    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=REPO_ROOT)

    if result.returncode != 0:
        print("ERROR: protoc failed", file=sys.stderr)
        sys.exit(result.returncode)

    # Fix bare import in generated grpc file (grpcio-tools < 1.72 uses bare imports)
    grpc_file = OUT_DIR / "workload_pb2_grpc.py"
    if grpc_file.exists():
        content = grpc_file.read_text(encoding="utf-8")
        bare = "import workload_pb2 as workload__pb2\n"
        relative = (
            "try:\n"
            "    from . import workload_pb2 as workload__pb2\n"
            "except ImportError:\n"
            "    import workload_pb2 as workload__pb2\n"
        )
        if bare in content and relative not in content:
            content = content.replace(bare, relative)
            grpc_file.write_text(content, encoding="utf-8")
            print("Patched relative import in", grpc_file)

    print("Done. Generated files in", OUT_DIR)


if __name__ == "__main__":
    main()
