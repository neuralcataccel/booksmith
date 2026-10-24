$ErrorActionPreference = 'Stop'
$taskName = 'Booksmith Audiobook Factory'
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Disable-ScheduledTask -TaskName $taskName | Out-Null
}
$runFile = Join-Path $PSScriptRoot 'run.py'
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like ('*' + $runFile + '*') } |
    ForEach-Object {
        if (Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue) {
            & taskkill.exe /PID $_.ProcessId /T /F | Out-Null
        }
    }
Write-Output 'Booksmith stopped. Conversion progress is retained. Run install-startup.ps1 to resume.'
