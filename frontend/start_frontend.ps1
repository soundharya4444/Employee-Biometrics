# Builds the frontend and serves it on your Wi-Fi (for LAN testing; production = Vercel).
# Usage (second PowerShell window, from the frontend folder, while backend\start_server.ps1 is running):   .\start_frontend.ps1
Set-Location -Path $PSScriptRoot

if (-not (Test-Path ".\node_modules")) { Write-Host "Installing packages (one time)..."; npm install --no-audit --no-fund }
if (-not (Test-Path ".\.env")) {
    $ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -like 'Wi-Fi*' } | Select-Object -First 1).IPAddress
    Set-Content ".\.env" "VITE_API_BASE_URL=http://${ip}:8000" -Encoding ascii
    Write-Host "Created frontend\.env pointing at the backend http://${ip}:8000"
}
Write-Host "Backend address baked into the build:" (Get-Content ".\.env" | Select-String "VITE_API_BASE_URL")
npm run build
if ($LASTEXITCODE -ne 0) { Write-Host "Build failed" -ForegroundColor Red; exit 1 }
Write-Host "Serving the built site on port 4173. Keep this window open. Ctrl+C to stop." -ForegroundColor Green
npm run preview -- --host --port 4173 --strictPort

