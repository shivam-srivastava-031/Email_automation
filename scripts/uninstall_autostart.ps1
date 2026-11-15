# Stops and removes the two "EmailAutomation-*" scheduled tasks (and their running processes).
#   powershell -ExecutionPolicy Bypass -File scripts\uninstall_autostart.ps1
$root = Split-Path -Parent $PSScriptRoot
foreach ($name in 'web', 'worker') {
    $task = Get-ScheduledTask -TaskName "EmailAutomation-$name" -ErrorAction SilentlyContinue
    if ($task) {
        Stop-ScheduledTask -TaskName "EmailAutomation-$name" -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName "EmailAutomation-$name" -Confirm:$false
        Write-Output "Removed EmailAutomation-$name"
    }
}
# Stopping the task does not always end its child processes: end any left from this folder.
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cmd.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$root*" -and ($_.CommandLine -match 'run_worker|runserver|run_web|run_worker\.cmd') } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Output "Stopped process $($_.ProcessId)" }
