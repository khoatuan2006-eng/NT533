<#
.SYNOPSIS
    Triển khai Prometheus & Grafana trực tiếp lên máy ảo AWS (Node 1) với network_mode: host.
    Nhờ host network, Prometheus cào thẳng được Envoy admin tại 127.0.0.1:9901.
#>
param(
    [string]$Node1 = "15.134.228.127",
    [string]$Key = "C:\Users\THIS PC\Downloads\lb-aws.pem",
    [string]$User = "ubuntu"
)

$ErrorActionPreference = "Stop"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "🚀 Cập nhật Prometheus & Grafana lên AWS Node 1 (Host Network)" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# 1. Upload cấu hình mới
Write-Host "1. Đang tải cấu hình cập nhật lên Node 1..." -ForegroundColor Yellow
ssh -i $Key -o StrictHostKeyChecking=accept-new $User@$Node1 "mkdir -p ~/monitoring/prometheus ~/monitoring/grafana"

scp -i $Key -o StrictHostKeyChecking=accept-new "deploy/monitoring/docker-compose.aws.yml" "$($User)@$($Node1):~/monitoring/docker-compose.yml"
scp -i $Key -o StrictHostKeyChecking=accept-new "deploy/monitoring/prometheus/prometheus.aws.yml" "$($User)@$($Node1):~/monitoring/prometheus/prometheus.aws.yml"
scp -i $Key -o StrictHostKeyChecking=accept-new -r "deploy/monitoring/grafana/provisioning" "$($User)@$($Node1):~/monitoring/grafana/"
scp -i $Key -o StrictHostKeyChecking=accept-new -r "deploy/monitoring/grafana/dashboards" "$($User)@$($Node1):~/monitoring/grafana/"

# 2. Khởi động lại container với cấu hình host network
Write-Host "2. Khởi động lại container với host network..." -ForegroundColor Yellow
ssh -i $Key -o StrictHostKeyChecking=accept-new $User@$Node1 "cd ~/monitoring && sudo docker compose down && sudo docker compose up -d"

# 3. Mở tunnel cổng 3000 về máy bạn
Write-Host "3. Đảm bảo cổng 3000 được kết nối về máy bạn..." -ForegroundColor Yellow
Start-Process ssh -ArgumentList "-i `"$Key`" -o StrictHostKeyChecking=accept-new -N -L 3000:localhost:3000 $User@$Node1" -WindowStyle Hidden

Start-Sleep -Seconds 3

Write-Host "`n==========================================================" -ForegroundColor Green
Write-Host "🎉 ĐÃ CẬP NHẬT XONG! PROMETHEUS ĐÃ CÀO TRỰC TIẾP ENVOY 127.0.0.1:9901" -ForegroundColor Green
Write-Host "👉 Hãy tải lại trang: http://localhost:3000" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Green
