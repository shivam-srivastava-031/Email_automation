# Registers two Windows scheduled tasks that start the web app and the worker
# hidden when you log in, and restart them if they stop.
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1          (register + start now)
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -NoStart (register only)
# Logs: logs\web.log and logs\worker.log. Remove with scripts\uninstall_autostart.ps1.
param([switch]$NoStart)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$user = "$env:USERDOMAIN\$env:USERNAME"

foreach ($name in 'web', 'worker') {
    $script = Join-Path $root "scripts\run_$name.cmd"
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -WorkingDirectory $root `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"& '$script'`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName "EmailAutomation-$name" -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description "Email automation ($name): starts at logon, restarts if it stops." -Force | Out-Null
    Write-Output "Registered EmailAutomation-$name"
    if (-not $NoStart) {
        Start-ScheduledTask -TaskName "EmailAutomation-$name"
        Write-Output "Started EmailAutomation-$name"
    }
}
