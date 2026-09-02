# Print Audit — минимальный учёт печати на Windows Print Server

Пилот-аналог MyQ для 4 объектов. Учёт идёт на уровне **Windows Print
Server Event Log**, а не устройства — работает с любыми принтерами (HP,
Kyocera, Canon и т.д.), пока печать проходит через очереди печати сервера.

См. также: [docs/RESEARCH.md](docs/RESEARCH.md) (какие открытые решения
рассматривались и почему выбран этот стек), [docs/ADMIN_GUIDE.md](docs/ADMIN_GUIDE.md)
(эксплуатация), [docs/ROADMAP.md](docs/ROADMAP.md) (развитие после пилота).

## Архитектура

```
Клиенты печатают через очереди Print Server (без прямой печати на IP/USB)
                    │
                    ▼
     Microsoft-Windows-PrintService/Operational (журнал событий, Event ID 307)
                    │  Get-WinEvent -FilterXPath (Export-PrintEvents.ps1)
                    ▼
   collector/collect_print_events.py   ── каждые 1-5 мин, Task Scheduler
     - разбор полей события (collector.field_map, откалиброван на объекте)
     - сопоставление user -> department (таблица users)
     - подбор тарифа по имени принтера (таблица price_list)
     - идемпотентная запись (курсор EventRecordID + UNIQUE(site_code, record_id))
                    │
                    ▼
              SQLite / PostgreSQL  (printaudit/models.py, одна схема для обоих)
                    │
                    ▼
        webapp/main.py (FastAPI)  ──  Jinja2 + Chart.js
        дашборд · по отделам · по пользователям · по принтерам · экспорт CSV
```

На каждом из 4 объектов разворачивается **одинаковый набор кода**, различается
только `config/config.yaml` (`site_code`, БД, откалиброванный `field_map`,
тарифы) и `config/users_departments.csv`.

## Структура репозитория

```
printaudit/          общий Python-пакет: конфиг, БД, модели, тарификация, запросы отчётов
collector/           сбор событий печати (PowerShell + Python)
webapp/               FastAPI веб-UI (шаблоны Jinja2, статика)
scripts/              init_db.py, sync_users_departments.py — разовые/периодические операции
config/               config.example.yaml, users_departments.csv (правится под объект)
db/schema.sql         справочная DDL-схема (реально таблицы создаёт scripts/init_db.py)
deploy/                скрипты регистрации Task Scheduler и запуска веб-сервера
docs/                  исследование, руководство админа, roadmap
data/, logs/           БД и логи (создаются автоматически)
```

## Развёртывание на одном объекте

Предполагается Windows Server 2016/2019/2022 с ролью Print Server и Python
3.9+ ([python.org](https://www.python.org/downloads/windows/), при установке
отметить "Add python.exe to PATH").

```powershell
cd C:\path\to\print-audit
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy config\config.example.yaml config\config.yaml
notepad config\config.yaml   # задать site_code, при желании БД/тарифы

python scripts\init_db.py
```

Включить журнал печати и откалибровать разбор полей — по
[docs/ADMIN_GUIDE.md](docs/ADMIN_GUIDE.md), разделы 1–3. Это обязательный шаг:
без калибровки `field_map` отчёты будут содержать мусор.

Заполнить отделы/пользователей и загрузить их в БД:

```powershell
notepad config\users_departments.csv
python scripts\sync_users_departments.py
```

Зарегистрировать сборщик в Task Scheduler (каждые 2 минуты по умолчанию):

```powershell
.\deploy\register_collector_task.ps1
```

Запустить веб-UI (для постоянной работы — обернуть NSSM, см.
`docs/ADMIN_GUIDE.md` раздел 8):

```powershell
.\deploy\run_webapp.ps1
```

Открыть `http://localhost:8000/` (или `http://<имя-сервера>:8000/` с другого
компьютера, если порт разрешён в брандмауэре).

## Как проверить, что всё работает

1. Напечатайте 2-3 тестовых документа с разных ПК через очереди этого сервера.
2. Подождите один цикл сборщика (по умолчанию до 2 минут) или запустите вручную:
   ```powershell
   python collector\collect_print_events.py
   ```
3. Проверьте `logs\collector.log` — должно быть `вставлено=N`.
4. Откройте дашборд (`/`) — задания должны появиться в текущем месяце;
   проверьте `/by-user`, `/by-printer`, `/export`.

## Тиражирование на объекты 2-4

1. Скопировать весь репозиторий (или сделать `git clone`/выгрузку из общего
   хранилища кода — код одинаковый для всех объектов).
2. На новом сервере повторить шаги "Развёртывание на одном объекте" выше,
   с **обязательной повторной калибровкой** `field_map` (может отличаться на
   другой версии Windows Server/драйвера) и своим `site_code`,
   `users_departments.csv`, при необходимости — своим `price_list`.
3. Не копировать `data\print_audit.db` и `config\config.yaml` между объектами —
   это единственные артефакты, специфичные для конкретной площадки.

## Конфигурация

- `config/config.yaml` — БД, тарифы по умолчанию, пути, параметры сборщика
  (`collector.field_map` — см. калибровку выше). Переопределить путь к файлу
  можно переменной окружения `PRINTAUDIT_CONFIG`.
- `config/users_departments.csv` — сопоставление пользователь → отдел,
  перечитывается вручную через `scripts/sync_users_departments.py`.
- `price_list` (таблица в БД) — тарифы Ч/Б и цвет по паттерну имени
  принтера/очереди; правится SQL-запросом или любым SQLite-клиентом
  (см. `docs/ADMIN_GUIDE.md`, раздел 5).
