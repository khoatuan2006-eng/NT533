# ─────────────────────────────────────────────────────────────────────────────
# Dockerfile — Một image dùng chung cho ba replica WorkloadService (Lầu — M1)
#
# Cách build:
#   docker build -f deploy/Dockerfile -t grpc-lb-server .
#
# Cách chạy mỗi replica (ví dụ):
#   docker run --rm -e BACKEND_ID=replica-1 -e SERVICE_DELAY_S=0.1 \
#              -e MAX_CONCURRENCY=10 -p 50051:50051 grpc-lb-server
#
# Biến môi trường:
#   BACKEND_ID      : tên replica (mặc định "replica-1")
#   SERVICE_DELAY_S : độ trễ mỗi request, giây (mặc định 0.1)
#   SERVICE_DELAY_MS: độ trễ mỗi request, mili-giay (fallback nếu không truyền SERVICE_DELAY_S)
#   MAX_CONCURRENCY : tối đa request đồng thời (mặc định 10)
#   PORT            : cổng lắng nghe (mặc định 50051)
#   LOG_PATH        : đường dẫn file JSONL (mặc định "/logs/server.jsonl")
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim AS base

# Tắt .pyc và bật unbuffered output để log hiển thị ngay
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# ── Cài dependency runtime ────────────────────────────────────────────────────
# Copy pyproject.toml trước để Docker cache layer này khi source chưa đổi
COPY pyproject.toml ./

# Cài đúng runtime dependencies đồng bộ với pyproject.toml
RUN pip install --no-cache-dir \
    "grpcio>=1.71.2,<1.72" \
    "grpcio-health-checking>=1.71.2,<1.72" \
    "protobuf>=5.26.1,<6"

# ── Copy source ───────────────────────────────────────────────────────────────
COPY api/            ./api/
COPY src/grpc_lb/    ./src/grpc_lb/

# Thêm src vào PYTHONPATH để `from grpc_lb...` hoạt động
ENV PYTHONPATH="/app/src"

# ── Thư mục log (volume mount từ compose.yaml) ────────────────────────────────
RUN mkdir -p /logs
VOLUME ["/logs"]

# ── Biến môi trường mặc định ──────────────────────────────────────────────────
ENV BACKEND_ID="replica-1" \
    SERVICE_DELAY_S="0.1" \
    MAX_CONCURRENCY="10" \
    PORT="50051" \
    LOG_PATH="/logs/server.jsonl"

EXPOSE 50051

# ── Entry point ───────────────────────────────────────────────────────────────
CMD ["python", "-m", "grpc_lb.server"]
