# Исследование: сбор данных печати с Windows Print Server

## 1. Источник данных: Event ID 307

Windows Print Server пишет события жизненного цикла заданий печати в журнал
`Applications and Services Logs > Microsoft > Windows > PrintService > Operational`.
Этот журнал **выключен по умолчанию** и должен быть включён на каждом сервере.

Событие **ID 307** ("задание напечатано") — основной источник данных: оно
создаётся один раз на каждое завершённое задание печати и содержит имя
пользователя, документа, принтера и количество напечатанных страниц. Это
подтверждается несколькими независимыми источниками:

- [How to Track Print History in Windows — NinjaOne](https://www.ninjaone.com/blog/track-print-history/)
- [Tracking Printer Usage with Windows Event Viewer Logs — Windows OS Hub](https://woshub.com/check-printer-usage-windows/)
- [PowerShell One-Liner to Audit Print Jobs — mikefrobbins.com](https://mikefrobbins.com/2017/08/10/powershell-one-liner-to-audit-print-jobs-on-a-windows-based-print-server/)
- [Use the Windows Event Viewer to track printing events — PaperCut KB](https://www.papercut.com/kb/Main/LogPrintJobsInEventViewer/)

**Важная оговорка, подтверждённая несколькими блогами и опытом сообщества:**
индексы позиционных свойств события (`$Event.Properties[i].Value`) — Job Id,
имя документа, пользователь, принтер, число страниц — **не задокументированы
Microsoft как стабильный публичный контракт** и на практике отличаются между
версиями Windows Server и версией драйвера принтера (класс v3 / v4). Поэтому
в этом проекте индексы **калибруются на каждом объекте** через
`collector/calibrate_event_fields.ps1`, а не жёстко зашиваются как факт —
см. `docs/ADMIN_GUIDE.md`.

Готовые PowerShell-скрипты для выгрузки события 307 в CSV, которые
подтверждают сам подход (Get-WinEvent + FilterXPath по EventID=307), но не
идут дальше CSV (без БД, веб-UI, тарификации, группировки по отделам):

- [Windows Server Print Job Accounting script (ID 307 + 805 -> 2 CSV) — Gist, TimMillerDyck](https://gist.github.com/TimMillerDyck/80a62a80aa7cc707eaee5cb42fc3fba6)
- [Print Server Job Logs from Event Viewer to CSV — Gist, theagreeablecow](https://gist.github.com/theagreeablecow/2856010)

## 2. Готовые open-source проекты — обзор и вывод

Целевая поиск-выборка: `windows print audit open source github`,
`print job accounting windows server github`, `powershell print service event 307`.

| Проект | Что делает | Почему не подходит как основа |
|---|---|---|
| Gist-скрипты (TimMillerDyck, theagreeablecow, mikefrobbins) | Читают Event ID 307/805, экспорт в CSV | Это одноразовые скрипты, а не приложения: нет БД, веб-UI, тарификации, группировки по отделам, идемпотентности между запусками. Годятся только как референс подхода к `Get-WinEvent`. |
| [SavaPage](https://www.savapage.org/) | Полноценный open-source "print portal": pull-печать, pay-per-print, biling, аудит | Работает как **собственный print-сервер/прокси** (обычно на базе CUPS), через который должна идти вся печать — то есть требует переподключения принтеров/клиентов к новой системе. Противоречит требованию "печать через существующие очереди Windows Print Server, никакой переделки инфраструктуры". Значительно больший объём внедрения, чем нужно для пилота на 4 объектах. |
| PaperCut, PrintAudit, MyQ | Коммерческие системы учёта печати | Не open source, лицензии/vendor lock-in — то, от чего заказчик уходит этим пилотом. Упомянуты только как ориентир по функциональности (MyQ явно назван прототипом). |

**Вывод:** ни один найденный готовый проект не закрывает связку
"читать Windows Print Server Event Log 307 → БД → веб-отчёты → тарификация"
без чужого print-пайплайна или лицензии. Готовые PowerShell-скрипты полезны
как референс паттерна выгрузки событий, но не как основа для доработки —
дописывать вокруг чужого одноразового скрипта БД и веб-слой сопоставимо по
объёму работы с тем, чтобы сразу написать это на связке
FastAPI + SQLAlchemy + Chart.js, где 90% кода — готовые библиотеки.

## 3. Выбор стека — рекомендация

**Путь B: минимальный MVP на PowerShell + Python**, а не адаптация готового
проекта (обоснование выше). Конкретно:

| Компонент | Выбор | Почему |
|---|---|---|
| Чтение Event Log | PowerShell `Get-WinEvent -FilterXPath` | Официальный, самый предсказуемый способ читать `.evtx`-журналы на Windows; не требует парсинга бинарных `.evtx` из Python. Вызывается из Python через `subprocess`, а не переписывается на `pywin32`/`python-evtx` — незачем: PowerShell делает это одной строкой, а `pywin32`/`python-evtx` для того же результата требуют больше кода и хуже читают live-журнал, который постоянно дописывается. |
| Инкрементальный сборщик | Python (`collect_print_events.py`) | Один язык для сборщика, БД и веб-слоя — меньше кода на стыках (сериализация в JSON между PS и Python — единственная граница). SQLAlchemy даёt одну и ту же схему для SQLite и PostgreSQL без дублирования кода. |
| БД | SQLite по умолчанию, переключение на PostgreSQL — одна строка в `config.yaml` | Для пилота на объект SQLite достаточно (низкий поток событий, один писатель — сборщик, один читатель — веб-UI). SQLAlchemy делает переход на PostgreSQL, если понадобится, тривиальным. |
| Веб-UI | FastAPI + Jinja2 + Chart.js (через CDN) | Минимум кода: FastAPI даёт роутинг+валидацию параметров "из коробки", Jinja2 — шаблоны без сборки фронтенда, Chart.js через `<script src>` — без npm/webpack. Соответствует требованию "простой UI, главное — таблицы и графики". |
| Планировщик сборщика | Task Scheduler (не служба) | Штатный механизм Windows, не требует дополнительных пакетов (NSSM и т.п.) именно для сборщика — только для веб-сервера, который должен работать постоянно. |

Все перечисленные библиотеки (FastAPI, SQLAlchemy, Jinja2, Chart.js) —
активно поддерживаемые проекты с MIT/BSD-подобными лицензиями, без vendor
lock-in — как и требовалось.

## 4. Что решение НЕ умеет "из коробки" (важно проговорить)

- **Точное определение цвета документа.** Событие 307 не сообщает,
  печатался ли документ в цвете. Рабочий обходной путь — заводить
  отдельные очереди печати "Ч/Б" и "Цвет" на один физический МФУ (это
  распространённая практика, а не хак; описана в `docs/ADMIN_GUIDE.md`) и
  тарифицировать по имени очереди через `price_list`.
- **Живая блокировка печати по лимиту.** Windows Print Server сам не умеет
  блокировать печать по квоте — учёт в этом пилоте пассивный (постфактум).
  Активные квоты/блокировки — пункт `docs/ROADMAP.md`.
