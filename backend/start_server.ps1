# Starts the attendance backend so phones on the same Wi-Fi can reach it.
# Usage (PowerShell, from this folder):   .\start_server.ps1
Set-Location -Path $PSScriptRoot

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Host "Python environment missing. Run:  py -3.13 -m venv .venv ; .\.venv\Scripts\python.exe -m pip install -r requirements.txt" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path ".\.env")) {
    Write-Host ".env not found. Run:  Copy-Item .env.example .env  and fill it in." -ForegroundColor Red
    exit 1
}

$python = ".\.venv\Scripts\python.exe"
$port = 8000

# An OLD copy of the server still running on this port keeps serving OLD code (and fails with "Internal server error"
# after an update). Find it and offer to stop it.
$old = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
if ($old.Count -gt 0) {
    Write-Host "Port $port is already in use by an older server:" -ForegroundColor Yellow
    foreach ($id in $old) {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$id"
        Write-Host ("  PID {0}  {1}" -f $id, $p.CommandLine)
    }
    $answer = Read-Host "Stop it and start the new server? (y/n)"
    if ($answer -ne 'y') { Write-Host "Not started. Stop the old server (Ctrl+C in its window) and run this script again." -ForegroundColor Red; exit 1 }
    # also stop any 'uvicorn --reload' parent / worker processes of THIS project that could re-open the port
    $mine = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*$PSScriptRoot*" -and ($_.CommandLine -match "uvicorn" -or $_.CommandLine -match "multiprocessing") }
    foreach ($m in $mine) { Stop-Process -Id $m.ProcessId -Force -ErrorAction SilentlyContinue }
    foreach ($id in $old) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    if (@(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue).Count -gt 0) {
        Write-Host "Port $port is still busy. Close the other PowerShell windows running the server and try again." -ForegroundColor Red
        exit 1
    }
}

& $python -c "from app.config import get_settings as g; s=g(); print('Backend API address   :', s.base_url); print('Frontend / QR address :', s.frontend_url, '(start it with start_frontend.ps1)')"

$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -like 'Wi-Fi*' } | Select-Object -First 1).IPAddress
if ($ip) { Write-Host "This PC's Wi-Fi address now : http://${ip}:$port   (must match BASE_URL above and VITE_API_BASE_URL in frontend\.env)" }

Write-Host "Starting server... keep this window open. Press Ctrl+C to stop." -ForegroundColor Green
& $python -W ignore -m uvicorn app.main:app --host 0.0.0.0 --port $port
