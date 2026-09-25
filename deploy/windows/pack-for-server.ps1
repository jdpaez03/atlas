# Pack your ATLAS data on this Windows PC for the Linux server (docs/SERVER_LINUX.md, step 3).
# Usage (from the repo folder):  powershell -ExecutionPolicy Bypass -File deploy\windows\pack-for-server.ps1
#
# Writes Desktop\atlas-transfer.tgz with:
#   atlas-local\   missions, memory, lessons, knowledge notes, brand kit, private prompts, ARGOS state
#                  (NOT the browser profiles or the Microsoft token cache: they only work on this PC)
#   atlas.env      your .env (the server adapts the Windows paths)
#   sessions\      the agents' signed-in browser sessions (REDI...), so you don't log in again on the server
#   claude-agents\ your Claude Code agents used by ATLAS (EOS division), from ATLAS_CLAUDE_AGENTS_DIR
# The file contains secrets: copy it to the server, run import-from-pc.sh there (it deletes it), delete it here.
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$envFile = Join-Path $repo ".env"

$local = Join-Path (Split-Path $repo -Parent) "atlas-local"
if (Test-Path $envFile) {
  $line = Get-Content $envFile | Where-Object { $_ -match '^\s*ATLAS_LOCAL_DIR\s*=' } | Select-Object -Last 1
  if ($line) {
    $value = ($line -split '=', 2)[1].Trim().Trim('"')
    if ($value) { $local = if ([IO.Path]::IsPathRooted($value)) { $value } else { Join-Path $repo $value } }
  }
}
if (-not (Test-Path $local)) { Write-Host "No atlas-local folder at $local"; exit 1 }

try {
  Invoke-RestMethod http://localhost:8000/health -TimeoutSec 2 | Out-Null
  Write-Host "ATLAS is running on this PC. Close its API window first (so the mission database is complete), then run this again."
  exit 1
} catch { }

$stage = Join-Path $env:TEMP ("atlas-transfer-" + (Get-Date -Format "yyyyMMddHHmmss"))
New-Item -ItemType Directory -Path $stage | Out-Null

Write-Host "Exporting the agents' browser sessions..."
Push-Location (Join-Path $repo "apps\api")
try { uv run atlas-browser export-session --all (Join-Path $stage "sessions") } catch { Write-Host "  (skipped: $_)" }
Pop-Location

Write-Host "Copying $local ..."
robocopy $local (Join-Path $stage "atlas-local") /E /NFL /NDL /NJH /NJS /NP /XD browser graphcache /XF "msal_cache.bin*" | Out-Null
if ($LASTEXITCODE -ge 8) { Write-Host "Copy failed (robocopy $LASTEXITCODE)"; exit 1 }

if (Test-Path $envFile) { Copy-Item $envFile (Join-Path $stage "atlas.env") }

# Claude Code agents ATLAS uses through the claude_md adapter (EOS division): ATLAS_CLAUDE_AGENTS_DIR
$agentsDir = Join-Path $HOME ".claude\agents"
if (Test-Path $envFile) {
  $line = Get-Content $envFile | Where-Object { $_ -match '^\s*ATLAS_CLAUDE_AGENTS_DIR\s*=' } | Select-Object -Last 1
  if ($line) {
    $value = ($line -split '=', 2)[1].Trim().Trim('"')
    if ($value) { $agentsDir = $value -replace '^~', $HOME }
  }
}
if (Test-Path $agentsDir) {
  Write-Host "Copying Claude Code agents from $agentsDir ..."
  robocopy $agentsDir (Join-Path $stage "claude-agents") *.md /NFL /NDL /NJH /NJS /NP | Out-Null
}

# tar.exe ships with Windows 10+ (Compress-Archive writes backslash paths Linux tools mis-read)
$zip = Join-Path ([Environment]::GetFolderPath("Desktop")) "atlas-transfer.tgz"
if (Test-Path $zip) { Remove-Item $zip }
tar.exe -czf $zip -C $stage .
if ($LASTEXITCODE -ne 0) { Write-Host "Packing failed (tar $LASTEXITCODE)"; exit 1 }
Remove-Item -Recurse -Force $stage

$mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host ""
Write-Host "Ready: $zip ($mb MB). It contains secrets."
Write-Host "Copy it to the server (replace <user> and <server> with yours):"
Write-Host "  scp `"$zip`" <user>@<server>:~/"
Write-Host "Then on the server:  bash ~/atlas/deploy/linux/import-from-pc.sh ~/atlas-transfer.tgz"
Write-Host "and delete the file from this PC."
