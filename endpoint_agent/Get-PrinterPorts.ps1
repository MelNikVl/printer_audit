<#
.SYNOPSIS
  Отдаёт JSON-снимок принтеров этого ПК: имя, порт, тип подключения
  (Local | Connection) — используется endpoint_agent.ports для того, чтобы
  отличить локальный/прямой принтер (USB, WSD, Standard TCP/IP — учитывается
  здесь) от подключения к чужой сетевой очереди (\\server\printer — уже
  учитывается самим Print Server, исключается здесь во избежание задвоения).

.DESCRIPTION
  Get-Printer (модуль PrintManagement, встроен в Windows 8+/Server 2012+)
  уже отдаёт свойство Type со значениями "Local"/"Connection" — надёжнее,
  чем разбор имени порта. WSD- и IP_-порты Standard TCP/IP Port Monitor
  распознаются Get-Printer как Type=Local, поэтому отдельная обработка не
  нужна.
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
    Write-Error "Get-PrinterPorts: не удалось получить список принтеров: $($_.Exception.Message)"
    exit 1
}

$result = @(
    foreach ($p in $printers) {
        [PSCustomObject]@{
            Name     = $p.Name
            PortName = $p.PortName
            Type     = [string]$p.Type
        }
    }
)

$json = ConvertTo-Json -InputObject $result -Depth 3 -Compress
if ($json -notmatch '^\s*\[') {
    $json = "[$json]"
}

Write-Output $json
