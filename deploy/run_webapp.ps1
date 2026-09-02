<#
.SYNOPSIS
  Запускает веб-UI (uvicorn) из корня репозитория. Для пилота достаточно запускать
  этот скрипт вручную/при логоне; для постоянной работы — обернуть службой через
  NSSM (см. docs/ADMIN_GUIDE.md, раздел "Запуск веб-UI как службы Windows").

.EXAMPLE
  .\deploy\run_webapp.ps1
  .\deploy\run_webapp.ps1 -Port 8080
#>
param(
    [string]$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path,
    [int]$Port = 8000,
    [string]$PythonExe = "python.exe"
)

Set-Location $RepoRoot
& $PythonExe -m uvicorn webapp.main:app --host 0.0.0.0 --port $Port
