<#
.SYNOPSIS
    Mở SSH tunnel ngầm từ máy local tới các VM AWS để Prometheus thu thập metrics.
.EXAMPLE
    .\start-tunnels.ps1 -Node1 3.27.8.103 -Node2 3.26.114.251 -Node3 16.176.22.169 -Key ~/.ssh/uitiot-lab.pem
#>
param(
    [Parameter(Mandatory=$true)]
    [string]$Node1,

    [Parameter(Mandatory=$true)]
    [string]$Node2,

    [Parameter(Mandatory=$true)]
    [string]$Node3,

    [string]$Key = "$HOME\.ssh\uitiot-lab.pem",

    [string]$User = "ubuntu"
)

$ErrorActionPreference = "Stop"

Write-Host "=== Khởi động SSH Tunnels tới cụm AWS ===" -ForegroundColor Cyan

# Kiểm tra file key nếu có truyền vào
$keyArg = ""
if ($Key -and (Test-Path $Key)) {
    $keyArg = "-i `"$Key`""
    Write-Host "Dùng SSH key: $Key" -ForegroundColor Gray
}

# Node 1: Envoy-1 (9901 -> 19901) và Consul-1 (8500 -> 18500)
Write-Host "Mở tunnel Node 1 ($Node1)..."
Start-Process ssh -ArgumentList "$keyArg -o StrictHostKeyChecking=accept-new -N -L 19901:172.31.20.204:9901 -L 18500:172.31.20.204:8500 $User@$Node1" -WindowStyle Hidden

# Node 2: Envoy-2 (9901 -> 29901) và Consul-2 (8500 -> 28500)
Write-Host "Mở tunnel Node 2 ($Node2)..."
Start-Process ssh -ArgumentList "$keyArg -o StrictHostKeyChecking=accept-new -N -L 29901:172.31.30.73:9901 -L 28500:172.31.30.73:8500 $User@$Node2" -WindowStyle Hidden

# Node 3: Consul-3 (8500 -> 38500)
Write-Host "Mở tunnel Node 3 ($Node3)..."
Start-Process ssh -ArgumentList "$keyArg -o StrictHostKeyChecking=accept-new -N -L 38500:172.31.26.182:8500 $User@$Node3" -WindowStyle Hidden

Start-Sleep -Seconds 2

Write-Host "`n=== Kiểm tra các cổng tunnel trên Localhost ===" -ForegroundColor Cyan
$ports = @(
    @{ Name = "Envoy-1 (Node1)"; Port = 19901 },
    @{ Name = "Consul-1 (Node1)"; Port = 18500 },
    @{ Name = "Envoy-2 (Node2)"; Port = 29901 },
    @{ Name = "Consul-2 (Node2)"; Port = 28500 },
    @{ Name = "Consul-3 (Node3)"; Port = 38500 }
)

foreach ($p in $ports) {
    $tcp = Test-NetConnection -ComputerName "127.0.0.1" -Port $p.Port -WarningAction SilentlyContinue
    if ($tcp.TcpTestSucceeded) {
        Write-Host " [OK] $($p.Name) -> localhost:$($p.Port)" -ForegroundColor Green
    } else {
        Write-Host " [FAIL] $($p.Name) -> localhost:$($p.Port) (chưa kết nối được)" -ForegroundColor Yellow
    }
}

Write-Host "`nĐã mở xong các tunnel. Để dừng tunnel, chạy: .\deploy\monitoring\stop-tunnels.ps1" -ForegroundColor Cyan
