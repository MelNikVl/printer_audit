<#
.SYNOPSIS
  Выгружает новые события печати (по умолчанию Event ID 307,
  "Job completed") из ЛОКАЛЬНОГО журнала Microsoft-Windows-PrintService/
  Operational этого пользовательского ПК в JSON на stdout.

.DESCRIPTION
  Копия collector/Export-PrintEvents.ps1 — намеренно ДУБЛИРОВАНА, а не
  импортируется из collector/, потому что endpoint_agent — отдельно
  устанавливаемый пакет (MSI/служба на пользовательском ПК), который не
  предполагает наличия остального репозитория на машине. Логика идентична:
  инкрементальность через -AfterRecordId (EventRecordID монотонно растёт
  в пределах одного журнала), результат — всегда JSON-массив.
#>
param(
    [string]$LogName = 'Microsoft-Windows-PrintService/Operational',
    [int]$EventId = 307,
    [long]$AfterRecordId = 0,
    [int]$MaxEvents = 2000
)

$ErrorActionPreference = 'Stop'

if ($PSVersionTable.PSVersion.Major -le 5) {
    try { & chcp.com 65001 > $null } catch {}
    $OutputEncoding = [System.Text.Encoding]::UTF8
}
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

try {
    $filterXPath = "*[System[EventID=$EventId and EventRecordID > $AfterRecordId]]"
    $events = Get-WinEvent -LogName $LogName -FilterXPath $filterXPath -ErrorAction Stop |
        Sort-Object RecordId |
        Select-Object -First $MaxEvents
}
catch {
    if ($_.Exception.Message -match 'No events were found') {
        Write-Output '[]'
        exit 0
    }
    Write-Error "Export-PrintEvents: не удалось прочитать журнал '$LogName': $($_.Exception.Message)"
    exit 1
}

if (-not $events -or @($events).Count -eq 0) {
    Write-Output '[]'
    exit 0
}

$result = @(
    foreach ($e in $events) {
        [PSCustomObject]@{
            RecordId    = $e.RecordId
            TimeCreated = $e.TimeCreated.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
            Message     = $e.Message
            Properties  = @($e.Properties | ForEach-Object { $_.Value })
        }
    }
)

# См. collector/Export-PrintEvents.ps1 — тот же обход разворачивания
# одноэлементного массива в ConvertTo-Json через pipe.
$json = ConvertTo-Json -InputObject $result -Depth 4 -Compress
if ($json -notmatch '^\s*\[') {
    $json = "[$json]"
}

Write-Output $json
