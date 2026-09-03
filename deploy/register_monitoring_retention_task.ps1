<#
.SYNOPSIS
  Регистрирует ежедневную задачу Task Scheduler для scripts\monitoring_retention.py
  (агрегация уровней расходников + очистка сырых мониторинговых сэмплов и
  старых решённых алертов — см. printaudit/monitoring/retention.py).

.EXAMPLE
  .\deploy\register_monitoring_retention_task.ps1
  .\deploy\register_monitoring_retention_task.ps1 -At "03:30"
#>
param(
    [string]$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path,
    [string]$PythonExe = (Join-Path (Resolve-Path "$PSScriptRoot\..").Path ".venv\Scripts\python.exe"),
    [string]$At = "03:15",
    [string]$TaskName = "PrintAuditMonitoringRetention"
)

$ErrorActionPreference = 'Stop'

if ($PSVersionTable.PSVersion.Major -le 5) {
    try { & chcp.com 65001 > $null } catch {}
    $OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
}

$scriptPath = Join-Path $RepoRoot "scripts\monitoring_retention.py"
if (-not (Test-Path $scriptPath)) {
    throw "Не найден $scriptPath — запускайте скрипт из корня репозитория (или укажите -RepoRoot)."
}
if (-not (Test-Path $PythonExe)) {
    throw "Python-интерпретатор не найден: $PythonExe`nСоздайте .venv и установите зависимости (см. deploy/register_collector_task.ps1)."
}

$action = New-ScheduledTaskAction -Execute $PythonExe -Argument "`"$scriptPath`"" -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -DontStopOnIdleEnd

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Задача '$TaskName' зарегистрирована: ежедневно в $At" -ForegroundColor Green
Write-Host "Проверить/запустить вручную: Start-ScheduledTask -TaskName '$TaskName'" -ForegroundColor Green
Write-Host "Лог: $RepoRoot\logs\monitoring_retention.log" -ForegroundColor Green
