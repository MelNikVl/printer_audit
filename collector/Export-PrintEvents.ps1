<#
.SYNOPSIS
  Выгружает новые события печати (по умолчанию Event ID 307,
  "Job completed") из журнала Microsoft-Windows-PrintService/Operational
  в JSON на stdout. Вызывается из collector/collect_print_events.py.

.DESCRIPTION
  Инкрементальность обеспечивается параметром -AfterRecordId: возвращаются
  только события с EventRecordID строго больше переданного курсора.
  EventRecordID монотонно возрастает в рамках одного журнала, поэтому
  это надёжный курсор (в отличие от TimeCreated, где возможны дубли/сдвиги
  при изменении времени на сервере).

  Свойства события (.Properties[i].Value) отдаются как есть, без разбора —
  разбор по field_map выполняется на стороне Python (printaudit/config.py),
  так как индексы полей нужно калибровать под конкретный сервер
  (см. calibrate_event_fields.ps1 и docs/ADMIN_GUIDE.md).

.PARAMETER LogName
  Имя журнала событий.

.PARAMETER EventId
  Идентификатор события для выгрузки.

.PARAMETER AfterRecordId
  Вернуть только события с EventRecordID > этого значения. 0 = все события в журнале.

.PARAMETER MaxEvents
  Ограничение на количество событий за один вызов (защита от слишком большой выгрузки
  при первом запуске на журнале с историей).
#>
param(
    [string]$LogName = 'Microsoft-Windows-PrintService/Operational',
    [int]$EventId = 307,
    [long]$AfterRecordId = 0,
    [int]$MaxEvents = 5000
)

$ErrorActionPreference = 'Stop'
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

$result | ConvertTo-Json -Depth 4 -Compress
