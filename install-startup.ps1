$ErrorActionPreference = 'Stop'
$taskName = 'Booksmith Audiobook Factory'
$launchFile = Join-Path $PSScriptRoot 'launch.ps1'
$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existingTask -and ($existingTask.Actions.Arguments -notlike ('*' + $launchFile + '*'))) {
    throw 'A different task already uses this name. It was not changed.'
}
$taskUserName = [Security.Principal.WindowsIdentity]::GetCurrent().Name
# powershell.exe -WindowStyle Hidden still flashes a Windows Terminal window every run (WT is the
# default terminal). run-hidden.exe is a GUI-subsystem launcher that starts PowerShell with no window.
$runHidden = 'run-hidden.exe'
if (Test-Path $runHidden) {
    $action = New-ScheduledTaskAction -Execute $runHidden -Argument ('"' + $launchFile + '"')
} else {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $launchFile + '"')
}
$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $taskUserName),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Minutes 5))
)
$principal = New-ScheduledTaskPrincipal -UserId $taskUserName -LogonType Interactive -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Principal $principal -Settings $taskSettings -Description 'Starts the private Booksmith service at sign-in and checks it every five minutes.' -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Output 'Booksmith starts at sign-in and is checked every five minutes while the user is signed in.'
