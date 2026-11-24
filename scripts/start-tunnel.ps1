# Script duy trì đường hầm SSH cho Grafana ổn định, tự động kết nối lại nếu bị rớt mạng.
param(
    [string]$Node4 = "3.27.108.199",
    [string]$Key = "C:\Users\THIS PC\Downloads\lb-aws.pem",
    [string]$User = "ubuntu"
)

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "DUY TRI DUONG HAM GRAFANA (Node 4: $Node4)"                -ForegroundColor Cyan
Write-Host "  Dia chi: http://localhost:3000"                           -ForegroundColor Green
Write-Host "  (Giu nguyen cua so nay mo suot buoi demo)"                 -ForegroundColor Yellow
Write-Host "==========================================================" -ForegroundColor Cyan

while ($true) {
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Dang ket noi SSH tunnel toi $Node4..." -ForegroundColor Cyan
    ssh -i "$Key" -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -N -L 3000:127.0.0.1:3000 "$User@$Node4"
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Ket noi bi ngat hoac chua bat. Thu ket noi lai sau 2 giay..." -ForegroundColor Yellow
    Start-Sleep -Seconds 2
}
