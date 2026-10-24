$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
try {
    $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8787/healthz' -TimeoutSec 4
    if ($health.worker_alive) { exit 0 }
} catch {}
$pythonExe = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$runScript = Join-Path $projectRoot 'run.py'
Start-Process -FilePath $pythonExe -ArgumentList @('"' + $runScript + '"') -WorkingDirectory $projectRoot -WindowStyle Hidden
