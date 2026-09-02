# Print Audit — учёт печати на Windows Print Server с AD-авторизацией

Аналог MyQ для нескольких объектов. Учёт идёт на уровне **Windows Print
Server Event Log**, а не устройства — работает с любыми принтерами (HP,
Kyocera, Canon и т.д.), пока печать проходит через очереди печати сервера.
Доступ к отчётам и админке — только через Active Directory, с ролями
`superadmin` / `admin` / `viewer`.

См. также: [docs/RESEARCH.md](docs/RESEARCH.md) (какие открытые решения
рассматривались и почему выбран этот стек), [docs/ADMIN_GUIDE.md](docs/ADMIN_GUIDE.md)
(эксплуатация: AD, миграции, принтеры, тарифы, бэкап), [docs/ROADMAP.md](docs/ROADMAP.md)
(развитие после пилота).

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
     - очередь печати -> printer_queues (авто-создание, если ещё не обнаружена)
     - тариф -> price_rules (версионированные, с приоритетом и периодом действия)
     - отдел -> ad_users (по правилам AD-группа→отдел) с fallback на CSV
     - идемпотентная запись (курсор EventRecordID + UNIQUE(site_code, record_id))
     - история каждого прогона -> sync_runs
                    │
                    ▼
              SQLite / PostgreSQL  (Alembic-миграции, printaudit/models.py)
                    │
                    ▼
        webapp/main.py (FastAPI) + AD-логин + RBAC + CSRF
        дашборд · по отделам/пользователям/принтерам · экспорт CSV · /admin
                    ▲
                    │ LDAP/LDAPS (ldap3)
                    ▼
              Active Directory
     (аутентификация, поиск/импорт пользователей и групп)

Get-Printer ──> printers/Export-Printers.ps1 ──> /admin/printers "Обнаружить очереди"
```

На каждом из объектов разворачивается **одинаковый код**, различается
только `config/config.yaml` (`site_code`, БД, откалиброванный `field_map`,
тарифы по умолчанию), `.env` (параметры AD/сессий — секреты) и локальная БД.

## Структура репозитория

```
printaudit/            общий Python-пакет
  ad/                    LDAP-клиент (ldap3) — вход, поиск пользователей/групп
  printers/              обнаружение очередей (Get-Printer) и резолвинг тарифа
  security/              серверные сессии, CSRF
  models.py              вся схема БД (SQLAlchemy)
  admin_users.py         правила назначения ролей (защита superadmin и т.п.)
  department_resolver.py правила "AD-группа -> отдел", резолвинг для заданий печати
  audit.py               запись в audit_log
collector/             сбор событий печати (PowerShell + Python)
printers/              Export-Printers.ps1 (Get-Printer, только чтение)
webapp/                FastAPI: отчёты, вход/выход, /admin/*
alembic/               миграции БД (versions/ — по одной ревизии на этап)
scripts/               init_db.py, sync_users_departments.py, bootstrap_superadmin.py
config/                config.example.yaml, users_departments.example.csv
.env.example           переменные AD/сессий (реальный .env — НЕ в Git)
db/schema.sql           справочная DDL-схема исходных 5 таблиц MVP
deploy/                Task Scheduler / запуск веб-сервера
docs/                   исследование, руководство админа, roadmap
tests/                  135+ тестов (pytest), без реального AD/принтера/домена
data/, logs/            БД и логи (создаются автоматически, не в Git)
```

## Развёртывание на одном объекте (с нуля)

Предполагается Windows Server 2016/2019/2022 с ролью Print Server и Python
3.10+ ([python.org](https://www.python.org/downloads/windows/), при установке
отметить "Add python.exe to PATH"). Домен AD — **опционален**: можно
развернуть только на локальных учётках, без AD вообще (см. ниже).

```powershell
cd C:\path\to\print-audit
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy config\config.example.yaml config\config.yaml
notepad config\config.yaml         # site_code, при желании БД/тарифы по умолчанию

copy .env.example .env
notepad .env                       # LOCAL_AUTH_ENABLED/AD_AUTH_ENABLED + SESSION_SECRET_KEY (+ AD_* если нужен AD)

python scripts\init_db.py          # применяет миграции Alembic + сидит price_list
```

Сгенерировать секрет сессий (вставить в `.env` -> `SESSION_SECRET_KEY`):

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Выдать доступ первому superadmin — локально (без AD, пароль вводится
интерактивно, не аргументом командной строки):

```powershell
python scripts\bootstrap_local_superadmin.py --login localadmin
```

...либо по логину AD, если AD настроен (см.
[docs/ADMIN_GUIDE.md](docs/ADMIN_GUIDE.md), раздел «Bootstrap первого superadmin»):

```powershell
python scripts\bootstrap_superadmin.py --login "DOMAIN\ivanov"
```

Если печать нужно учитывать по Windows Print Server events (обычный сценарий
этого проекта) — включите журнал печати и откалибруйте разбор полей событий
307 по [docs/ADMIN_GUIDE.md](docs/ADMIN_GUIDE.md), разделы про калибровку.
Это обязательный шаг: без калибровки `field_map` отчёты будут содержать мусор.

Зарегистрировать сборщик в Task Scheduler (каждые 2 минуты по умолчанию,
использует Python из `.venv`, проверяет интерпретатор перед регистрацией):

```powershell
.\deploy\register_collector_task.ps1
```

Запустить веб-UI (для постоянной работы — обернуть NSSM, см.
`docs/ADMIN_GUIDE.md`):

```powershell
.\deploy\run_webapp.ps1
```

Открыть `http://localhost:8000/login` (или `http://<имя-сервера>:8000/login`
с другого компьютера, если порт разрешён в брандмауэре), войти под тем
логином, которому только что выдали superadmin, паролем от AD.

Дальше как superadmin в `/admin`:
1. **Администраторы** — выдать доступ остальным сотрудникам (найти в AD, назначить роль).
2. **Отделы** — создать структуру отделов.
3. **Пользователи AD** / **Группы AD** — импортировать нужных пользователей/группы,
   связать AD-группы с отделами (раздел «Группы AD»), проверить план через
   «Правила отделов (dry-run)» и применить.
4. **Принтеры** — «Обнаружить очереди» (Get-Printer), задать цвет/тариф по очередям.
5. **Тарифы** — при необходимости завести версионированные правила (период действия, приоритет).

## Как проверить, что всё работает

1. Напечатайте 2-3 тестовых документа с разных ПК через очереди этого сервера.
2. Подождите один цикл сборщика (по умолчанию до 2 минут) или запустите вручную:
   ```powershell
   .\.venv\Scripts\python.exe collector\collect_print_events.py
   ```
3. Проверьте `logs\collector.log` — должно быть `вставлено=N`; или откройте
   `/admin` (Обзор) — там видна история запусков сборщика (sync_runs).
4. Откройте дашборд (`/`) — задания должны появиться в текущем месяце;
   проверьте `/by-user`, `/by-printer`, `/export`.
5. Попробуйте зайти без логина (`/`, `/export/csv`, `/api/print-jobs`) в окне
   инкогнито — должен быть редирект на `/login` (страницы) или 401 (API/CSV).

## Обновление уже развёрнутого сервера

```powershell
git pull
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts\init_db.py    # применит новые миграции, если есть
```

Существующая БД и накопленная история печати не теряются — миграции
Alembic написаны так, чтобы безопасно применяться поверх уже работающей базы
(см. `alembic/versions/90fa7d836021_baseline_existing_mvp_schema.py`).

## Тиражирование на другие объекты

1. Скопировать весь репозиторий (или сделать `git clone`/выгрузку из общего
   хранилища кода — код одинаковый для всех объектов).
2. На новом сервере повторить шаги «Развёртывание на одном объекте» выше,
   с **обязательной повторной калибровкой** `field_map` (может отличаться на
   другой версии Windows Server/драйвера), своим `site_code`, своим `.env`
   (тот же AD, но секрет сессий должен быть СВОЙ на каждом объекте) и
   отдельным bootstrap superadmin.
3. Никогда не копировать между объектами: `data\*.db`, `config\config.yaml`,
   `.env`, `config\users_departments.csv` — все они либо специфичны для
   площадки, либо содержат секреты.

## Конфигурация

- `config/config.yaml` — БД, тарифы по умолчанию, пути, параметры сборщика
  (`collector.field_map`, `document_name_policy`). Путь к файлу можно
  переопределить переменной окружения `PRINTAUDIT_CONFIG`. **Не содержит
  секретов** и не должен коммититься (см. `.gitignore`).
- `.env` — параметры AD (`AD_SERVER`, `AD_BASE_DN`, `AD_BIND_USER`/
  `AD_BIND_PASSWORD`, ...) и сессий (`SESSION_SECRET_KEY`,
  `SESSION_COOKIE_SECURE`). Секреты — никогда не коммитится, см. `.env.example`.
- `config/users_departments.csv` — легаси-fallback сопоставления пользователь
  → отдел для тех, кого не завели через AD; перечитывается через
  `scripts/sync_users_departments.py`. Основной путь — через AD-группы в `/admin`.
- Тарифы — через `/admin/pricing` (price_rules, с периодом действия и
  приоритетом) или `/admin/printers` (быстрый тариф на очередь). Легаси
  `price_list` в БД остаётся рабочим fallback.

## Тесты

```powershell
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

135+ тестов, ни один не требует реального AD-домена, принтера или сети —
AD замокан через `ldap3` MOCK_SYNC, `Get-Printer` и `Export-PrintEvents.ps1`
подменяются на уровне `subprocess.run`/фабрик, БД — временный SQLite на
каждый тест. Подробности — в docs/ADMIN_GUIDE.md, раздел «Тесты».
