<#
.SYNOPSIS
  Диагностический скрипт для калибровки collector.field_map в config/config.yaml.

.DESCRIPTION
  Печатает последние N событий печати (по умолчанию EventID 307) с индексами
  всех свойств (.Properties[i].Value) и рендеренным текстом сообщения — чтобы
  можно было сопоставить индекс -> смысл поля (Job Id, имя документа,
  пользователь, имя принтера, число страниц) и внести правильные индексы
  в config.yaml -> collector.field_map.

  Индексы .Properties НЕ документированы Microsoft как стабильный публичный
  контракт и на практике отличаются между Windows Server 2016/2019/2022
  и версией драйвера принтера (v3 vs v4) — поэтому калибровку нужно повторить
  на каждом из 4 объектов при первом развёртывании.

.EXAMPLE
  .\calibrate_event_fields.ps1
  .\calibrate_event_fields.ps1 -Count 10
#>
param(
    [string]$LogName = 'Microsoft-Windows-PrintService/Operational',
    [int]$EventId = 307,
    [int]$Count = 5
)

$ErrorActionPreference = 'Stop'

# Windows PowerShell 5.1 по умолчанию читает .ps1 и пишет в консоль не в UTF-8,
# из-за чего кириллица в Write-Host превращается в нечитаемые символы. Нужны
# ОБА исправления: этот файл сохранён в UTF-8 с BOM (чтобы PS 5.1 правильно
# прочитал строковые литералы из самого файла), и ниже принудительно
# переключаем кодовую страницу консоли и OutputEncoding на UTF-8 (чтобы то,
# что выводится, тоже отображалось верно). В PowerShell 7+ это не требуется,
# но безопасно выполнить в любом случае.
if ($PSVersionTable.PSVersion.Major -le 5) {
    try { & chcp.com 65001 > $null } catch {}
    $OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
}

try {
    $events = Get-WinEvent -LogName $LogName -FilterXPath "*[System[EventID=$EventId]]" -MaxEvents $Count -ErrorAction Stop
}
catch {
    # Get-WinEvent бросает завершающую ошибку, если совпадений нет (это не
    # предупреждение) — с $ErrorActionPreference='Stop' её нужно перехватывать
    # явно, иначе скрипт упадёт вместо того, чтобы показать дружелюбную подсказку.
    if ($_.Exception.Message -match 'No events were found') {
        Write-Host "Событий с EventID=$EventId в журнале '$LogName' не найдено." -ForegroundColor Yellow
        Write-Host "Напечатайте тестовый документ через очередь этого сервера и повторите." -ForegroundColor Yellow
        exit 0
    }
    Write-Host "Не удалось прочитать журнал '$LogName': $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

if (-not $events) {
    Write-Host "Событий с EventID=$EventId в журнале '$LogName' не найдено." -ForegroundColor Yellow
    Write-Host "Напечатайте тестовый документ через очередь этого сервера и повторите." -ForegroundColor Yellow
    exit 0
}

foreach ($e in $events) {
    Write-Host ("===== RecordId {0}   TimeCreated {1} =====" -f $e.RecordId, $e.TimeCreated) -ForegroundColor Cyan
    Write-Host "Message:"
    Write-Host $e.Message
    Write-Host ""
    Write-Host "Properties (индекс = значение):"
    for ($i = 0; $i -lt $e.Properties.Count; $i++) {
        Write-Host ("  [{0}] = {1}" -f $i, $e.Properties[$i].Value)
    }
    Write-Host ""
}

Write-Host "Сопоставьте индексы выше со смыслом полей (Job Id, документ, пользователь, принтер, число страниц)" -ForegroundColor Green
Write-Host "и обновите collector.field_map в config/config.yaml." -ForegroundColor Green
