<#
.SYNOPSIS
  Регистрирует задачу в Task Scheduler, которая запускает collector\collect_print_events.py
  каждые N минут.

.EXAMPLE
  .\deploy\register_collector_task.ps1
  .\deploy\register_collector_task.ps1 -IntervalMinutes 5 -PythonExe "C:\Python311\python.exe"
#>
param(
    [string]$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path,
    [string]$PythonExe = "python.exe",
    [int]$IntervalMinutes = 2,
    [string]$TaskName = "PrintAuditCollector"
)

$ErrorActionPreference = 'Stop'

$scriptPath = Join-Path $RepoRoot "collector\collect_print_events.py"
if (-not (Test-Path $scriptPath)) {
    throw "Не найден $scriptPath — запускайте скрипт из корня репозитория (или укажите -RepoRoot)."
}

$action = New-ScheduledTaskAction -Execute $PythonExe -Argument "`"$scriptPath`"" -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration ([TimeSpan]::MaxValue)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -DontStopOnIdleEnd

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Задача '$TaskName' зарегистрирована: запуск каждые $IntervalMinutes мин., $PythonExe $scriptPath" -ForegroundColor Green
Write-Host "Проверить/запустить вручную: Start-ScheduledTask -TaskName '$TaskName'" -ForegroundColor Green
Write-Host "Лог сборщика: $RepoRoot\logs\collector.log" -ForegroundColor Green
