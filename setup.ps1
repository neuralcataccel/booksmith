$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
    }
    $pythonExe = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    & $pythonExe -m pip install -r requirements.lock
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & $pythonExe download_models.py
    if ($LASTEXITCODE -ne 0) { throw 'Model verification failed.' }
    & $pythonExe configure_plex.py
    if ($LASTEXITCODE -ne 0) { throw 'Plex setup failed.' }
    & (Join-Path $PSScriptRoot 'install-startup.ps1')
} finally {
    Pop-Location
}
