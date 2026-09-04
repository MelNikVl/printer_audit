<#
.SYNOPSIS
  Регистрирует задачу в Task Scheduler, которая опрашивает принтеры
  (Zabbix/direct SNMP) через collector\monitor_printers.py каждые N минут.

.DESCRIPTION
  См. deploy/register_collector_task.ps1 — тот же принцип (используется
  Python из .venv проекта, проверяются зависимости перед регистрацией).
  По умолчанию каждые 5 минут (см. docs/PRINTER_MONITORING_FORECASTING.md
  — интервал опроса по умолчанию).

.EXAMPLE
  .\deploy\register_monitor_printers_task.ps1
  .\deploy\register_monitor_printers_task.ps1 -IntervalMinutes 10
#>
param(
    [string]$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path,
    [string]$PythonExe = (Join-Path (Resolve-Path "$PSScriptRoot\..").Path ".venv\Scripts\python.exe"),
    [int]$IntervalMinutes = 5,
    [string]$TaskName = "PrintAuditMonitorPrinters"
)

$ErrorActionPreference = 'Stop'

if ($PSVersionTable.PSVersion.Major -le 5) {
    try { & chcp.com 65001 > $null } catch {}
    $OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
}

$scriptPath = Join-Path $RepoRoot "collector\monitor_printers.py"
if (-not (Test-Path $scriptPath)) {
    throw "Не найден $scriptPath — запускайте скрипт из корня репозитория (или укажите -RepoRoot)."
}

if (-not (Test-Path $PythonExe)) {
    throw (
        "Python-интерпретатор не найден: $PythonExe`n" +
        "Из корня репозитория выполните:`n" +
        "    python -m venv .venv`n" +
        "    .\.venv\Scripts\python.exe -m pip install -r requirements.txt`n" +
        "(для direct_snmp дополнительно: pip install pysnmp==6.2.6 — см. requirements.txt)"
    )
}

& $PythonExe -c "import fastapi, sqlalchemy, alembic, httpx" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw (
        "Интерпретатор $PythonExe найден, но в нём не установлены зависимости проекта. Выполните:`n" +
        "    $PythonExe -m pip install -r requirements.txt`n" +
        "и повторите запуск этого скрипта."
    )
}

$action = New-ScheduledTaskAction -Execute $PythonExe -Argument "`"$scriptPath`"" -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -DontStopOnIdleEnd

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null

Write-Host "Задача '$TaskName' зарегистрирована: опрос принтеров каждые $IntervalMinutes мин." -ForegroundColor Green
Write-Host "Проверить/запустить вручную: Start-ScheduledTask -TaskName '$TaskName'" -ForegroundColor Green
Write-Host "Лог: $RepoRoot\logs\monitor_printers.log" -ForegroundColor Green
