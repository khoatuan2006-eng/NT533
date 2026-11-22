#!/usr/bin/env python3
"""Zero-dependency Monitoring Dashboard for NT533 gRPC Load Balancing.

Runs purely on Python standard library (no pip install, no Docker needed).
Scrapes the local SSH tunnels:
  - Node 1 Envoy: http://localhost:19901/stats/prometheus
  - Node 2 Envoy: http://localhost:29901/stats/prometheus
  - Node 1 Consul: http://localhost:18500/v1/agent/metrics?format=prometheus
  - Node 2 Consul: http://localhost:28500/v1/agent/metrics?format=prometheus
  - Node 3 Consul: http://localhost:38500/v1/agent/metrics?format=prometheus

Serves a modern Grafana-style Dark Theme Web Dashboard at http://localhost:3000
"""

from __future__ import annotations

import collections
import http.server
import json
import re
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PORT = 3000
HISTORY_LEN = 60  # Keep 60 data points (2 minutes at 2s interval)

# Metrics storage
history: list[dict] = []
history_lock = threading.Lock()

# Targets via SSH tunnels
TARGETS = {
    "envoy1": "http://localhost:19901/stats/prometheus",
    "envoy2": "http://localhost:29901/stats/prometheus",
    "consul1": "http://localhost:18500/v1/agent/metrics?format=prometheus",
    "consul2": "http://localhost:28500/v1/agent/metrics?format=prometheus",
    "consul3": "http://localhost:38500/v1/agent/metrics?format=prometheus",
}

# Previous counters for rate calculation
prev_counters: dict[str, float] = {}
prev_time: float = 0.0


def fetch_text(url: str, timeout: float = 1.5) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "NT533-Dashboard"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return ""


def parse_prometheus(text: str) -> dict[str, float]:
    """Parse Prometheus text lines into a flat key->value map."""
    metrics: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            key, val_str = parts[0], parts[1]
            try:
                metrics[key] = float(val_str)
            except ValueError:
                pass
    return metrics


def collector_loop():
    global prev_counters, prev_time
    while True:
        now = time.time()
        point = {
            "timestamp": time.strftime("%H:%M:%S", time.localtime(now)),
            "envoy1_up": False,
            "envoy2_up": False,
            "consul1_up": False,
            "consul2_up": False,
            "consul3_up": False,
            "total_rps": 0.0,
            "envoy1_rps": 0.0,
            "envoy2_rps": 0.0,
            "success_rate": 100.0,
            "grpc_ok": 0.0,
            "grpc_unavailable": 0.0,
            "grpc_deadline": 0.0,
            "p50_ms": 0.0,
            "p90_ms": 0.0,
            "p99_ms": 0.0,
            "consul_leader": "None",
            "consul_peers": 0,
        }

        # 1. Scrape Envoy 1
        t1 = fetch_text(TARGETS["envoy1"])
        if t1:
            point["envoy1_up"] = True
            m1 = parse_prometheus(t1)
            e1_reqs = m1.get('envoy_http_downstream_rq_total{envoy_http_conn_manager_prefix="ingress_grpc"}', 0.0)
            e1_ok = m1.get('envoy_cluster_grpc_0{envoy_cluster_name="entry_backends"}', 0.0)
            e1_unavail = m1.get('envoy_cluster_grpc_14{envoy_cluster_name="entry_backends"}', 0.0)
            e1_dead = m1.get('envoy_cluster_grpc_4{envoy_cluster_name="entry_backends"}', 0.0)
        else:
            m1, e1_reqs, e1_ok, e1_unavail, e1_dead = {}, 0.0, 0.0, 0.0, 0.0

        # 2. Scrape Envoy 2
        t2 = fetch_text(TARGETS["envoy2"])
        if t2:
            point["envoy2_up"] = True
            m2 = parse_prometheus(t2)
            e2_reqs = m2.get('envoy_http_downstream_rq_total{envoy_http_conn_manager_prefix="ingress_grpc"}', 0.0)
            e2_ok = m2.get('envoy_cluster_grpc_0{envoy_cluster_name="entry_backends"}', 0.0)
            e2_unavail = m2.get('envoy_cluster_grpc_14{envoy_cluster_name="entry_backends"}', 0.0)
            e2_dead = m2.get('envoy_cluster_grpc_4{envoy_cluster_name="entry_backends"}', 0.0)
        else:
            m2, e2_reqs, e2_ok, e2_unavail, e2_dead = {}, 0.0, 0.0, 0.0, 0.0

        # Calculate Rates (RPS)
        if prev_time > 0 and (now - prev_time) > 0:
            dt = now - prev_time
            d_e1 = max(0.0, e1_reqs - prev_counters.get("e1_reqs", e1_reqs)) / dt
            d_e2 = max(0.0, e2_reqs - prev_counters.get("e2_reqs", e2_reqs)) / dt
            d_ok = max(0.0, (e1_ok + e2_ok) - prev_counters.get("ok", e1_ok + e2_ok)) / dt
            d_unavail = max(0.0, (e1_unavail + e2_unavail) - prev_counters.get("unavail", e1_unavail + e2_unavail)) / dt
            d_dead = max(0.0, (e1_dead + e2_dead) - prev_counters.get("dead", e1_dead + e2_dead)) / dt

            point["envoy1_rps"] = round(d_e1, 1)
            point["envoy2_rps"] = round(d_e2, 1)
            point["total_rps"] = round(d_e1 + d_e2, 1)
            point["grpc_ok"] = round(d_ok, 1)
            point["grpc_unavailable"] = round(d_unavail, 1)
            point["grpc_deadline"] = round(d_dead, 1)

            total_grpc = d_ok + d_unavail + d_dead
            if total_grpc > 0:
                point["success_rate"] = round((d_ok / total_grpc) * 100, 2)
            else:
                point["success_rate"] = 100.0

        prev_counters = {
            "e1_reqs": e1_reqs,
            "e2_reqs": e2_reqs,
            "ok": e1_ok + e2_ok,
            "unavail": e1_unavail + e2_unavail,
            "dead": e1_dead + e2_dead,
        }
        prev_time = now

        # 3. Scrape Consul Leader & Peers
        for cname, node in [("consul1", "Node 1"), ("consul2", "Node 2"), ("consul3", "Node 3")]:
            ctext = fetch_text(TARGETS[cname])
            if ctext:
                point[f"{cname}_up"] = True
                cm = parse_prometheus(ctext)
                if cm.get("consul_raft_leader", 0) == 1:
                    point["consul_leader"] = node
                peers = cm.get("consul_raft_peers", 0)
                if peers > point["consul_peers"]:
                    point["consul_peers"] = int(peers)

        with history_lock:
            history.append(point)
            if len(history) > HISTORY_LEN:
                history.pop(0)

        time.sleep(2.0)


HTML_PAGE = """<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <title>NT533 - gRPC Load Balancing Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {
      --bg: #0b0f19;
      --card-bg: #111827;
      --border: #1f2937;
      --text: #f3f4f6;
      --text-muted: #9ca3af;
      --green: #10b981;
      --red: #ef4444;
      --blue: #3b82f6;
      --yellow: #f59e0b;
      --purple: #8b5cf6;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); padding: 20px; }
    header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 15px; margin-bottom: 20px; }
    h1 { font-size: 1.4rem; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 10px; }
    .badge { font-size: 0.75rem; padding: 4px 8px; border-radius: 9999px; background: rgba(59, 130, 246, 0.2); color: var(--blue); border: 1px solid rgba(59, 130, 246, 0.4); }
    
    /* Cluster status bar */
    .nodes-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 20px; }
    .node-card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 12px; display: flex; align-items: center; justify-content: space-between; }
    .node-title { font-size: 0.85rem; font-weight: 600; }
    .node-sub { font-size: 0.75rem; color: var(--text-muted); }
    .dot { width: 10px; height: 10px; border-radius: 50%; background: #4b5563; }
    .dot.online { background: var(--green); box-shadow: 0 0 8px var(--green); }
    .dot.offline { background: var(--red); box-shadow: 0 0 8px var(--red); }

    /* Key metrics row */
    .stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; margin-bottom: 20px; }
    .stat-card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
    .stat-label { font-size: 0.8rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; }
    .stat-value { font-size: 1.8rem; font-weight: 700; margin-top: 5px; }

    /* Charts grid */
    .charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
    .chart-card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 16px; min-height: 280px; }
    .chart-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; font-size: 0.95rem; font-weight: 600; }

    /* Footer status */
    footer { margin-top: 25px; text-align: center; font-size: 0.8rem; color: var(--text-muted); }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>⚡ NT533 - Hệ thống Cân Bằng Tải gRPC <span class="badge">AWS c7i 4-Node Cluster</span></h1>
      <div style="font-size: 0.8rem; color: var(--text-muted); margin-top: 4px;">Giám sát thời gian thực qua SSH Tunnels • Cập nhật mỗi 2 giây</div>
    </div>
    <div id="conn-status" style="font-size: 0.85rem; color: var(--green);">● Đang đồng bộ</div>
  </header>

  <!-- Node status -->
  <div class="nodes-grid">
    <div class="node-card">
      <div>
        <div class="node-title">Node 1 (15.134.228.127)</div>
        <div class="node-sub">Envoy-1 | Consul-1 | a-1, b-1</div>
      </div>
      <div id="dot-node1" class="dot"></div>
    </div>
    <div class="node-card">
      <div>
        <div class="node-title">Node 2 (3.27.42.43)</div>
        <div class="node-sub">Envoy-2 | Consul-2 | a-2, c-1</div>
      </div>
      <div id="dot-node2" class="dot"></div>
    </div>
    <div class="node-card">
      <div>
        <div class="node-title">Node 3 (54.252.167.177)</div>
        <div class="node-sub">Consul-3 | a-3, b-2</div>
      </div>
      <div id="dot-node3" class="dot"></div>
    </div>
    <div class="node-card">
      <div>
        <div class="node-title">Node 4 (54.206.61.224)</div>
        <div class="node-sub">Consul Agent | a-4, c-2 | Load Gen</div>
      </div>
      <div id="dot-node4" class="dot online"></div>
    </div>
  </div>

  <!-- Big Stat Cards -->
  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-label">Tổng Thông Lượng (Ingress)</div>
      <div id="stat-rps" class="stat-value" style="color: var(--blue);">0 RPS</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Tỷ Lệ Thành Công</div>
      <div id="stat-success" class="stat-value" style="color: var(--green);">100%</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Consul Raft Leader</div>
      <div id="stat-leader" class="stat-value" style="color: var(--yellow); font-size: 1.4rem;">Đang đọc...</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Raft Peers (Quorum = 3)</div>
      <div id="stat-peers" class="stat-value" style="color: var(--purple);">0 / 3</div>
    </div>
  </div>

  <!-- Real-time Charts -->
  <div class="charts-grid">
    <div class="chart-card">
      <div class="chart-header">
        <span>📈 Thông lượng RPS (Envoy 1 vs Envoy 2)</span>
      </div>
      <canvas id="chart-rps" height="180"></canvas>
    </div>

    <div class="chart-card">
      <div class="chart-header">
        <span>🚦 Mã trạng thái gRPC (Stacked Rate/s)</span>
      </div>
      <canvas id="chart-grpc" height="180"></canvas>
    </div>
  </div>

  <footer>
    Đồ án Hệ tính toán phân bố NT533 • Bảng giám sát không phụ thuộc Docker
  </footer>

  <script>
    const chartRPS = new Chart(document.getElementById('chart-rps'), {
      type: 'line',
      data: {
        labels: [],
        datasets: [
          { label: 'Tổng RPS', borderColor: '#3b82f6', backgroundColor: 'rgba(59, 130, 246, 0.1)', data: [], fill: true, tension: 0.2 },
          { label: 'Envoy 1', borderColor: '#10b981', borderDash: [4, 4], data: [], fill: false },
          { label: 'Envoy 2', borderColor: '#f59e0b', borderDash: [4, 4], data: [], fill: false }
        ]
      },
      options: {
        responsive: true,
        animation: false,
        scales: {
          x: { grid: { color: '#1f2937' }, ticks: { color: '#9ca3af', maxTicksLimit: 10 } },
          y: { grid: { color: '#1f2937' }, ticks: { color: '#9ca3af' }, beginAtZero: true }
        },
        plugins: { legend: { labels: { color: '#f3f4f6' } } }
      }
    });

    const chartGRPC = new Chart(document.getElementById('chart-grpc'), {
      type: 'line',
      data: {
        labels: [],
        datasets: [
          { label: 'gRPC OK', borderColor: '#10b981', backgroundColor: 'rgba(16, 185, 129, 0.15)', data: [], fill: true },
          { label: 'gRPC UNAVAILABLE', borderColor: '#ef4444', backgroundColor: 'rgba(239, 68, 68, 0.2)', data: [], fill: true },
          { label: 'DEADLINE EXCEEDED', borderColor: '#f59e0b', data: [], fill: false }
        ]
      },
      options: {
        responsive: true,
        animation: false,
        scales: {
          x: { grid: { color: '#1f2937' }, ticks: { color: '#9ca3af', maxTicksLimit: 10 } },
          y: { grid: { color: '#1f2937' }, ticks: { color: '#9ca3af' }, beginAtZero: true }
        },
        plugins: { legend: { labels: { color: '#f3f4f6' } } }
      }
    });

    async function updateData() {
      try {
        const res = await fetch('/api/metrics');
        const data = await res.json();
        if (!data || data.length === 0) return;

        const latest = data[data.length - 1];

        // Update cards
        document.getElementById('stat-rps').innerText = latest.total_rps + ' RPS';
        document.getElementById('stat-success').innerText = latest.success_rate + '%';
        document.getElementById('stat-success').style.color = latest.success_rate > 98 ? 'var(--green)' : 'var(--red)';
        document.getElementById('stat-leader').innerText = latest.consul_leader;
        document.getElementById('stat-peers').innerText = latest.consul_peers + ' / 3';

        // Update dots
        document.getElementById('dot-node1').className = 'dot ' + (latest.envoy1_up ? 'online' : 'offline');
        document.getElementById('dot-node2').className = 'dot ' + (latest.envoy2_up ? 'online' : 'offline');
        document.getElementById('dot-node3').className = 'dot ' + (latest.consul3_up ? 'online' : 'offline');

        // Update charts
        const labels = data.map(d => d.timestamp);
        chartRPS.data.labels = labels;
        chartRPS.data.datasets[0].data = data.map(d => d.total_rps);
        chartRPS.data.datasets[1].data = data.map(d => d.envoy1_rps);
        chartRPS.data.datasets[2].data = data.map(d => d.envoy2_rps);
        chartRPS.update();

        chartGRPC.data.labels = labels;
        chartGRPC.data.datasets[0].data = data.map(d => d.grpc_ok);
        chartGRPC.data.datasets[1].data = data.map(d => d.grpc_unavailable);
        chartGRPC.data.datasets[2].data = data.map(d => d.grpc_deadline);
        chartGRPC.update();

      } catch (err) {
        document.getElementById('conn-status').innerText = '○ Mất kết nối backend';
        document.getElementById('conn-status').style.color = 'var(--red)';
      }
    }

    setInterval(updateData, 2000);
    updateData();
  </script>
</body>
</html>
"""


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))
        elif self.path == "/api/metrics":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with history_lock:
                payload = json.dumps(history)
            self.wfile.write(payload.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress console HTTP access logs to keep terminal clean


def main():
    print("=" * 60)
    print("🚀 NT533 Zero-Dependency Web Dashboard")
    print("=" * 60)
    print("Đang chạy thu thập dữ liệu từ các cổng SSH Tunnel...")
    print(f"👉 MỞ TRÌNH DUYỆT TẠI: http://localhost:{PORT}")
    print("Bấm Ctrl + C để dừng Dashboard.")
    print("=" * 60)

    # Start background scraper thread
    t = threading.Thread(target=collector_loop, daemon=True)
    t.start()

    # Start HTTP server
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), DashboardHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nĐã tắt Dashboard.")


if __name__ == "__main__":
    main()
