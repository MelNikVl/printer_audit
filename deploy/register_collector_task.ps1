<#
.SYNOPSIS
  Регистрирует задачу в Task Scheduler, которая запускает collector\collect_print_events.py
  каждые N минут.

.DESCRIPTION
  По умолчанию использует Python из виртуального окружения проекта
  (.venv\Scripts\python.exe), а НЕ первый "python.exe" из PATH — на сервере
  в PATH может стоять любой системный Python (включая Microsoft Store stub),
  без установленных зависимостей проекта (fastapi/sqlalchemy/alembic/ldap3),
  и задача будет молча падать при каждом запуске. Перед регистрацией
  проверяется, что указанный интерпретатор существует и что в нём реально
  установлены зависимости проекта.

.EXAMPLE
  .\deploy\register_collector_task.ps1
  .\deploy\register_collector_task.ps1 -IntervalMinutes 5 -PythonExe "C:\Python311\python.exe"
#>
param(
    [string]$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path,
    [string]$PythonExe = (Join-Path (Resolve-Path "$PSScriptRoot\..").Path ".venv\Scripts\python.exe"),
    [int]$IntervalMinutes = 2,
    [string]$TaskName = "PrintAuditCollector"
)

$ErrorActionPreference = 'Stop'

# См. collector/calibrate_event_fields.ps1 — тот же фикс кодировки для
# Windows PowerShell 5.1 (кириллица в сообщениях и BOM у самого файла).
if ($PSVersionTable.PSVersion.Major -le 5) {
    try { & chcp.com 65001 > $null } catch {}
    $OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
}

$scriptPath = Join-Path $RepoRoot "collector\collect_print_events.py"
if (-not (Test-Path $scriptPath)) {
    throw "Не найден $scriptPath — запускайте скрипт из корня репозитория (или укажите -RepoRoot)."
}

if (-not (Test-Path $PythonExe)) {
    throw (
        "Python-интерпретатор не найден: $PythonExe`n" +
        "Похоже, виртуальное окружение проекта не создано. Из корня репозитория выполните:`n" +
        "    python -m venv .venv`n" +
        "    .\.venv\Scripts\python.exe -m pip install -r requirements.txt`n" +
        "и повторите запуск этого скрипта (или передайте -PythonExe с путём к нужному интерпретатору)."
    )
}

& $PythonExe -c "import fastapi, sqlalchemy, alembic, ldap3, yaml" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw (
        "Интерпретатор $PythonExe найден, но в нём не установлены зависимости проекта " +
        "(fastapi/sqlalchemy/alembic/ldap3/PyYAML). Выполните:`n" +
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

Write-Host "Задача '$TaskName' зарегистрирована: запуск каждые $IntervalMinutes мин., $PythonExe $scriptPath" -ForegroundColor Green
Write-Host "Проверить/запустить вручную: Start-ScheduledTask -TaskName '$TaskName'" -ForegroundColor Green
Write-Host "Лог сборщика: $RepoRoot\logs\collector.log" -ForegroundColor Green
