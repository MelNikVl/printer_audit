<#
.SYNOPSIS
  Регистрирует задачу в Task Scheduler, которая запускает collector\agent_sync.py
  каждые N минут — отправку накопленной локальной очереди печати в
  центральный Print Audit (см. docs/MULTISITE_ARCHITECTURE.md).

.DESCRIPTION
  Только для APP_MODE=agent (см. .env.example) — этот сервер уже собирает
  события локально через collector\collect_print_events.py (отдельная
  задача, см. register_collector_task.ps1) и должен ДОПОЛНИТЕЛЬНО отправлять
  их в центр. Регистрирует ОТДЕЛЬНУЮ задачу Task Scheduler, а не встраивает
  отправку в сам сборщик — недоступность центра не должна останавливать
  локальный сбор событий, и наоборот.

  По умолчанию использует Python из виртуального окружения проекта
  (.venv\Scripts\python.exe), как и все остальные deploy-скрипты проекта —
  см. register_collector_task.ps1 и run_webapp.ps1 про причину.

.EXAMPLE
  .\deploy\register_agent_sync_task.ps1
  .\deploy\register_agent_sync_task.ps1 -IntervalMinutes 1
#>
param(
    [string]$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path,
    [string]$PythonExe = (Join-Path (Resolve-Path "$PSScriptRoot\..").Path ".venv\Scripts\python.exe"),
    [int]$IntervalMinutes = 2,
    [string]$TaskName = "PrintAuditAgentSync"
)

$ErrorActionPreference = 'Stop'

# См. collector/calibrate_event_fields.ps1 — тот же фикс кодировки для
# Windows PowerShell 5.1 (кириллица в сообщениях и BOM у самого файла).
if ($PSVersionTable.PSVersion.Major -le 5) {
    try { & chcp.com 65001 > $null } catch {}
    $OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
}

$scriptPath = Join-Path $RepoRoot "collector\agent_sync.py"
if (-not (Test-Path $scriptPath)) {
    throw "Не найден $scriptPath — запускайте скрипт из корня репозитория (или укажите -RepoRoot)."
}

$envPath = Join-Path $RepoRoot ".env"
if (-not (Test-Path $envPath)) {
    Write-Warning (
        "Не найден $envPath. Для APP_MODE=agent нужны APP_MODE/CENTRAL_BASE_URL/" +
        "AGENT_SITE_UUID/AGENT_PRINT_SERVER_UUID/AGENT_TOKEN в .env (см. .env.example) — " +
        "без них agent_sync.py просто ничего не будет отправлять (см. лог)."
    )
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

& $PythonExe -c "import httpx, sqlalchemy, yaml" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw (
        "Интерпретатор $PythonExe найден, но в нём не установлены зависимости проекта " +
        "(httpx/sqlalchemy/PyYAML). Выполните:`n" +
        "    $PythonExe -m pip install -r requirements.txt`n" +
        "и повторите запуск этого скрипта."
    )
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
Write-Host "Лог отправки в центр: $RepoRoot\logs\agent_sync.log" -ForegroundColor Green
Write-Host "Диагностика соединения с центром: $PythonExe scripts\agent_diagnose.py" -ForegroundColor Green
