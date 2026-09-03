<#
.SYNOPSIS
  Устанавливает endpoint-агент (учёт USB/WSD/прямых IP-принтеров, см.
  endpoint_agent/) на пользовательский Windows ПК как службу Windows —
  работает без открытого окна, переживает logoff/перезагрузку.

.DESCRIPTION
  Предполагает, что каталог endpoint_agent/ (с зависимостями, установленными
  в него -- см. -RequirementsAlreadyInstalled) уже скопирован на целевой ПК
  (например, через GPO Software Installation с использованием MSI-обёртки,
  см. deploy/endpoint_agent.wxs и раздел "Развёртывание через GPO/MSI" в
  docs/PRINTER_MONITORING_FORECASTING.md, либо просто скопирован вручную/
  через сетевую папку для пилота на малом числе ПК).

  Шаги:
    1. Проверяет endpoint_agent.env (создаёт из example и завершается с
       понятной ошибкой, если ENDPOINT_UUID/ENDPOINT_TOKEN не заполнены --
       их даёт администратор при регистрации в /admin/endpoint-agents на
       сервере площадки).
    2. Устанавливает pywin32 в переданный интерпретатор (если ещё не стоит).
    3. Регистрирует и запускает Windows Service через endpoint_agent/service.py.

.EXAMPLE
  .\deploy\install_endpoint_agent.ps1
  .\deploy\install_endpoint_agent.ps1 -PythonExe "C:\Python311\python.exe"
#>
param(
    [string]$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path,
    [string]$PythonExe = (Join-Path (Resolve-Path "$PSScriptRoot\..").Path ".venv\Scripts\python.exe")
)

$ErrorActionPreference = 'Stop'

if ($PSVersionTable.PSVersion.Major -le 5) {
    try { & chcp.com 65001 > $null } catch {}
    $OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
}

$agentDir = Join-Path $RepoRoot "endpoint_agent"
$envPath = Join-Path $agentDir "endpoint_agent.env"
$examplePath = Join-Path $agentDir "endpoint_agent.env.example"
$servicePath = Join-Path $agentDir "service.py"

if (-not (Test-Path $servicePath)) {
    throw "Не найден $servicePath — этот скрипт запускается из копии репозитория с каталогом endpoint_agent\ (не только endpoint_agent\ отдельно)."
}

if (-not (Test-Path $envPath)) {
    Copy-Item $examplePath $envPath
    throw (
        "Создан $envPath из шаблона — заполните SERVER_BASE_URL/ENDPOINT_UUID/ENDPOINT_TOKEN " +
        "(выдаются в /admin/endpoint-agents на сервере ЭТОЙ площадки при регистрации агента для " +
        "этого компьютера) и запустите установку ещё раз."
    )
}

$envContent = Get-Content $envPath -Raw
if ($envContent -notmatch 'ENDPOINT_UUID=\S' -or $envContent -notmatch 'ENDPOINT_TOKEN=\S') {
    throw "$envPath существует, но ENDPOINT_UUID/ENDPOINT_TOKEN не заполнены. Заполните их и повторите запуск."
}

if (-not (Test-Path $PythonExe)) {
    throw "Python-интерпретатор не найден: $PythonExe`nСоздайте окружение: python -m venv .venv"
}

& $PythonExe -c "import win32serviceutil" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Устанавливаю pywin32..." -ForegroundColor Yellow
    & $PythonExe -m pip install -r (Join-Path $agentDir "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "Не удалось установить pywin32 в $PythonExe" }
}

Write-Host "Регистрирую службу PrintAuditEndpointAgent..." -ForegroundColor Yellow
& $PythonExe $servicePath --startup=auto install
if ($LASTEXITCODE -ne 0) { throw "Регистрация службы завершилась ошибкой (код $LASTEXITCODE)." }

& $PythonExe $servicePath start
if ($LASTEXITCODE -ne 0) { throw "Запуск службы завершился ошибкой (код $LASTEXITCODE) — см. Просмотр событий Windows -> Приложения." }

Write-Host "Служба 'Print Audit Endpoint Agent' установлена и запущена." -ForegroundColor Green
Write-Host "Лог: $agentDir\logs\endpoint_agent.log" -ForegroundColor Green
Write-Host "Проверить статус: Get-Service PrintAuditEndpointAgent" -ForegroundColor Green
