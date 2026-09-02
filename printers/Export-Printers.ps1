<#
.SYNOPSIS
  Выгружает список локальных очередей печати (Get-Printer) в JSON на stdout,
  для синхронизации в printer_queues (см. printaudit/printers/discovery.py).

.DESCRIPTION
  ТОЛЬКО ЧТЕНИЕ: скрипт не создаёт, не удаляет и не изменяет ни одной
  очереди/принтера — только Get-Printer. Любые изменения в Windows
  (добавление/удаление принтера) должны выполняться администратором отдельно,
  вручную или другим скриптом, никогда автоматически из этого приложения.

  Тот же контракт "всегда JSON-массив", что и Export-PrintEvents.ps1 (см.
  collector/Export-PrintEvents.ps1) — тот же самый баг с ConvertTo-Json,
  разворачивающим массив из одного принтера в объект, актуален и здесь.
#>
param()

$ErrorActionPreference = 'Stop'

if ($PSVersionTable.PSVersion.Major -le 5) {
    try { & chcp.com 65001 > $null } catch {}
    $OutputEncoding = [System.Text.Encoding]::UTF8
}
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

try {
    $printers = Get-Printer -ErrorAction Stop
}
catch {
    Write-Error "Export-Printers: не удалось получить список принтеров: $($_.Exception.Message)"
    exit 1
}

if (-not $printers -or @($printers).Count -eq 0) {
    Write-Output '[]'
    exit 0
}

$result = @(
    foreach ($p in $printers) {
        [PSCustomObject]@{
            Name           = $p.Name
            ComputerName   = $p.ComputerName
            ShareName      = $p.ShareName
            DriverName     = $p.DriverName
            PortName       = $p.PortName
            Location       = $p.Location
            Comment        = $p.Comment
            Shared         = [bool]$p.Shared
            Published      = [bool]$p.Published
            PrinterStatus  = [string]$p.PrinterStatus
        }
    }
)

$json = ConvertTo-Json -InputObject $result -Depth 4 -Compress
if ($json -notmatch '^\s*\[') {
    $json = "[$json]"
}
Write-Output $json
