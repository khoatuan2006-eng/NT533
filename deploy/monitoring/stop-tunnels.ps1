<#
.SYNOPSIS
    Dừng các tiến trình SSH tunnel đang chạy nền cho monitoring.
#>
Write-Host "=== Dừng các tiến trình SSH tunnel cho monitoring ===" -ForegroundColor Cyan

$tunnels = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq "ssh.exe" -and (
        $_.CommandLine -match "19901" -or
        $_.CommandLine -match "29901" -or
        $_.CommandLine -match "38500" -or
        $_.CommandLine -match "18500"
    )
}

if ($tunnels) {
    foreach ($proc in $tunnels) {
        Write-Host "Dừng tiến trình SSH PID: $($proc.ProcessId)" -ForegroundColor Yellow
        Stop-Process -Id $proc.ProcessId -Force
    }
    Write-Host "Đã đóng toàn bộ SSH tunnels." -ForegroundColor Green
} else {
    Write-Host "Không có SSH tunnel nào đang chạy." -ForegroundColor Gray
}
