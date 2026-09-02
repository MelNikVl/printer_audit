<#
.SYNOPSIS
  Запускает веб-UI (uvicorn) из корня репозитория. Для пилота достаточно запускать
  этот скрипт вручную/при логоне; для постоянной работы — обернуть службой через
  NSSM (см. docs/ADMIN_GUIDE.md, раздел "Запуск веб-UI как службы Windows").

.DESCRIPTION
  По умолчанию использует Python из виртуального окружения проекта
  (.venv\Scripts\python.exe), а НЕ первый "python.exe" из PATH — та же
  причина, что и в deploy\register_collector_task.ps1: на сервере в PATH
  может стоять любой системный Python без установленных зависимостей
  проекта, и веб-приложение будет падать при каждом запуске. Перед запуском
  проверяется, что указанный интерпретатор существует и что в нём реально
  установлены зависимости проекта.

.EXAMPLE
  .\deploy\run_webapp.ps1
  .\deploy\run_webapp.ps1 -Port 8080
  .\deploy\run_webapp.ps1 -PythonExe "C:\Python311\python.exe"
#>
param(
    [string]$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path,
    [int]$Port = 8000,
    [string]$PythonExe = (Join-Path (Resolve-Path "$PSScriptRoot\..").Path ".venv\Scripts\python.exe")
)

$ErrorActionPreference = 'Stop'

# См. collector/calibrate_event_fields.ps1 — тот же фикс кодировки для
# Windows PowerShell 5.1 (кириллица в сообщениях и BOM у самого файла).
if ($PSVersionTable.PSVersion.Major -le 5) {
    try { & chcp.com 65001 > $null } catch {}
    $OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
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

& $PythonExe -c "import fastapi, sqlalchemy, ldap3, yaml, uvicorn" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw (
        "Интерпретатор $PythonExe найден, но в нём не установлены зависимости проекта " +
        "(fastapi/sqlalchemy/ldap3/PyYAML/uvicorn). Выполните:`n" +
        "    $PythonExe -m pip install -r requirements.txt`n" +
        "и повторите запуск этого скрипта."
    )
}

Set-Location $RepoRoot
& $PythonExe -m uvicorn webapp.main:app --host 0.0.0.0 --port $Port
