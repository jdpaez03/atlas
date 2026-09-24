# Start ATLAS on Windows: API (port 8000) + Command Center (port 3000), each in its own window.
# Usage (from the repo folder):  .\start.ps1
$root = $PSScriptRoot

function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host "Installing uv..."
  winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements
  Refresh-Path   # make the fresh install visible to this session and the windows it opens
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Write-Host "uv not found. Open a new PowerShell window and run .\start.ps1 again."; exit 1 }
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { Write-Host "Node.js not found. Install it (winget install OpenJS.NodeJS.LTS) and run again."; exit 1 }

Start-Process powershell -ArgumentList "-NoExit", "-Command", "`$host.UI.RawUI.WindowTitle='ATLAS API'; cd '$root\apps\api'; uv sync; uv run python -m atlas"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "`$host.UI.RawUI.WindowTitle='ATLAS Command Center'; cd '$root\apps\web'; if (-not (Test-Path node_modules)) { npm install }; npm run dev"

Write-Host "Waiting for the API on http://localhost:8000 ..."
for ($i = 0; $i -lt 90; $i++) {
  try { Invoke-RestMethod http://localhost:8000/health -TimeoutSec 2 | Out-Null; break } catch { Start-Sleep -Seconds 2 }
}
Start-Process "http://localhost:3000"
