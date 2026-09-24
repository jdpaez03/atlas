# Start ATLAS on Windows: API (port 8000) + Command Center (port 3000), each in its own window.
# Usage (from the repo folder):  powershell -ExecutionPolicy Bypass -File .\start.ps1
$root = $PSScriptRoot
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Write-Host "Installing uv..."; winget install --id astral-sh.uv -e }
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root\apps\api'; uv sync; uv run python -m atlas"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root\apps\web'; if (-not (Test-Path node_modules)) { npm install }; npm run dev"
Start-Sleep -Seconds 12
Start-Process "http://localhost:3000"
