<#
.SYNOPSIS
    Triển khai Prometheus & Grafana lên AWS Node 4 với network_mode: host.
    Đồng thời cài Node Exporter trên cả 4 node để giám sát CPU/RAM/Network.

.DESCRIPTION
    Bước 0: Cài Node Exporter trên cả 4 node AWS (nếu chưa có)
    Bước 1: Upload cấu hình monitoring lên Node 4
    Bước 2: Khởi động container Prometheus + Grafana trên Node 4
    Bước 3: Kiểm tra kết nối Envoy admin và Node Exporter
    Bước 4: Mở SSH tunnel cổng 3000 và 9090 về máy local

.NOTES
    Yêu cầu tiên quyết:
    - Envoy trên Node 1 & 2 phải bind port 9901 ra 0.0.0.0 (không phải 127.0.0.1).
    - Security Group AWS: mở port 9901, 8500, 9100 giữa các node trong VPC.

.EXAMPLE
    .\deploy\monitoring\run-grafana-on-node4.ps1
    .\deploy\monitoring\run-grafana-on-node4.ps1 -Node4 54.206.61.224
#>
param(
    [string]$Node1 = "3.25.139.183",
    [string]$Node2 = "3.26.2.87",
    [string]$Node3 = "15.135.223.24",
    [string]$Node4 = "3.107.13.147",
    [string]$Key = "C:\Users\THIS PC\Downloads\lb-aws.pem",
    [string]$User = "ubuntu",
    [string]$Node1Private = "172.31.20.204",
    [string]$Node2Private = "172.31.30.73",
    [string]$Node3Private = "172.31.26.182",
    [string]$Node4Private = "172.31.18.115",
    [switch]$SkipNodeExporter
)

$ErrorActionPreference = "Continue"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "Trien khai Monitoring Stack len cum 4 Node AWS"             -ForegroundColor Cyan
Write-Host "  Prometheus + Grafana => Node 4 ($Node4)"                  -ForegroundColor Cyan
Write-Host "  Node Exporter => tat ca 4 node"                          -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# 0. Kiểm tra file key tồn tại
try {
    if ((Test-Path $Key -ErrorAction SilentlyContinue) -eq $false) {
        Write-Host "Canh bao: Khong tim thay SSH key tai: $Key" -ForegroundColor Yellow
    }
}
catch {}

# ==========================================
# BUOC 0: Cài Node Exporter trên cả 4 node
# ==========================================
if (-not $SkipNodeExporter) {
    Write-Host "`n0. Cai dat Node Exporter tren 4 node..." -ForegroundColor Yellow

    $nodeExporterCmd = @'
if sudo docker ps --format '{{.Names}}' | grep -q '^node-exporter$'; then
    echo 'ALREADY_RUNNING'
else
    sudo docker rm -f node-exporter >/dev/null 2>&1
    sudo docker run -d --name node-exporter --restart unless-stopped --net=host --pid=host -v /:/host:ro,rslave quay.io/prometheus/node-exporter:v1.8.2 --path.rootfs=/host >/dev/null 2>&1
    if [ $? -eq 0 ]; then
        echo 'STARTED'
    else
        echo 'FAILED'
    fi
fi
'@

    $allNodes = @(
        @{ Name = "Node 1"; IP = $Node1 },
        @{ Name = "Node 2"; IP = $Node2 },
        @{ Name = "Node 3"; IP = $Node3 },
        @{ Name = "Node 4"; IP = $Node4 }
    )

    foreach ($n in $allNodes) {
        $result = ssh -i "$Key" -o StrictHostKeyChecking=accept-new "$User@$($n.IP)" $nodeExporterCmd 2>$null
        if ($result -match "ALREADY_RUNNING") {
            Write-Host "   [OK] $($n.Name) ($($n.IP)): Node Exporter da chay san." -ForegroundColor Green
        }
        elseif ($result -match "STARTED") {
            Write-Host "   [OK] $($n.Name) ($($n.IP)): Node Exporter da khoi dong." -ForegroundColor Green
        }
        else {
            Write-Host "   [WARN] $($n.Name) ($($n.IP)): $result" -ForegroundColor Yellow
        }
    }
}
else {
    Write-Host "`n0. Bo qua cai Node Exporter (flag -SkipNodeExporter)." -ForegroundColor Gray
}

# ==========================================
# BUOC 1: Upload cấu hình monitoring lên Node 4
# ==========================================
Write-Host "`n1. Dang tai cau hinh monitoring len Node 4..." -ForegroundColor Yellow

ssh -i "$Key" -o StrictHostKeyChecking=accept-new "$User@$Node4" "mkdir -p ~/monitoring/prometheus ~/monitoring/grafana" 2>$null

scp -i "$Key" -o StrictHostKeyChecking=accept-new "deploy/monitoring/docker-compose.node4.yml" "$User@$Node4`:~/monitoring/docker-compose.yml" 2>$null
scp -i "$Key" -o StrictHostKeyChecking=accept-new "deploy/monitoring/prometheus/prometheus.node4.yml" "$User@$Node4`:~/monitoring/prometheus/prometheus.node4.yml" 2>$null
scp -i "$Key" -o StrictHostKeyChecking=accept-new -r "deploy/monitoring/grafana/provisioning" "$User@$Node4`:~/monitoring/grafana/" 2>$null
scp -i "$Key" -o StrictHostKeyChecking=accept-new -r "deploy/monitoring/grafana/dashboards" "$User@$Node4`:~/monitoring/grafana/" 2>$null

Write-Host "   Da upload xong cau hinh." -ForegroundColor Green

# ==========================================
# BUOC 2: Khởi động Prometheus + Grafana trên Node 4
# ==========================================
Write-Host "`n2. Khoi dong container Prometheus + Grafana tren Node 4..." -ForegroundColor Yellow

ssh -i "$Key" -o StrictHostKeyChecking=accept-new "$User@$Node4" "cd ~/monitoring && sudo docker compose down 2>/dev/null; sudo docker compose up -d" 2>$null

Write-Host "   Container da khoi dong." -ForegroundColor Green

# ==========================================
# BUOC 3: Kiểm tra kết nối
# ==========================================
Write-Host "`n3. Kiem tra ket noi tu Node 4..." -ForegroundColor Yellow

# Kiểm tra Envoy
$checkCmd1 = "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 http://" + $Node1Private + ":9901/stats/prometheus 2>/dev/null || echo FAIL"
$envoyCheck1 = ssh -i "$Key" -o StrictHostKeyChecking=accept-new "$User@$Node4" $checkCmd1 2>$null
if ($envoyCheck1 -match "200") {
    Write-Host "   [OK] Envoy Node 1 ($Node1Private`:9901)" -ForegroundColor Green
}
else {
    Write-Host "   [WARN] Envoy Node 1 ($Node1Private`:9901): KHONG KET NOI DUOC" -ForegroundColor Yellow
}

$checkCmd2 = "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 http://" + $Node2Private + ":9901/stats/prometheus 2>/dev/null || echo FAIL"
$envoyCheck2 = ssh -i "$Key" -o StrictHostKeyChecking=accept-new "$User@$Node4" $checkCmd2 2>$null
if ($envoyCheck2 -match "200") {
    Write-Host "   [OK] Envoy Node 2 ($Node2Private`:9901)" -ForegroundColor Green
}
else {
    Write-Host "   [WARN] Envoy Node 2 ($Node2Private`:9901): KHONG KET NOI DUOC" -ForegroundColor Yellow
}

# Kiểm tra Node Exporter
$checkNE = "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 http://" + $Node1Private + ":9100/metrics 2>/dev/null || echo FAIL"
$neCheck = ssh -i "$Key" -o StrictHostKeyChecking=accept-new "$User@$Node4" $checkNE 2>$null
if ($neCheck -match "200") {
    Write-Host "   [OK] Node Exporter Node 1 ($Node1Private`:9100)" -ForegroundColor Green
}
else {
    Write-Host "   [WARN] Node Exporter Node 1: KHONG KET NOI DUOC (kiem tra Security Group port 9100)" -ForegroundColor Yellow
}

# ==========================================
# BUOC 4: Mở SSH tunnel về máy local
# ==========================================
Write-Host "`n4. Mo SSH tunnel: Grafana (3000) + Prometheus (9090) ve localhost..." -ForegroundColor Yellow

# Kill existing tunnels if any
Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq "ssh.exe" -and (
        $_.CommandLine -match "3000:localhost:3000" -or
        $_.CommandLine -match "9090:localhost:9090"
    )
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

Start-Process ssh -ArgumentList "-i `"$Key`" -o StrictHostKeyChecking=accept-new -N -L 3000:localhost:3000 -L 9090:localhost:9090 $User@$Node4" -WindowStyle Hidden

Start-Sleep -Seconds 3

Write-Host "`n==========================================================" -ForegroundColor Green
Write-Host "DA TRIEN KHAI XONG MONITORING STACK!"                        -ForegroundColor Green
Write-Host ""                                                               -ForegroundColor Green
Write-Host "   Grafana:     http://localhost:3000  (admin / admin)"         -ForegroundColor Cyan
Write-Host "   Prometheus:  http://localhost:9090"                          -ForegroundColor Cyan
Write-Host ""                                                               -ForegroundColor Green
Write-Host "   Scrape targets:"                                              -ForegroundColor Gray
Write-Host "     - Envoy-1:        $Node1Private`:9901"                      -ForegroundColor Gray
Write-Host "     - Envoy-2:        $Node2Private`:9901"                      -ForegroundColor Gray
Write-Host "     - Consul 1-3:     :8500"                                    -ForegroundColor Gray
Write-Host "     - Node Exporter:  :9100 (4 nodes)"                          -ForegroundColor Gray
Write-Host "==========================================================" -ForegroundColor Green
