# Мониторинг принтеров, endpoint-агент, прогнозирование

Этот документ описывает архитектуру, добавленную в ветке
`feature/printer-health-endpoint-forecasting`: техническое состояние
физических принтеров (не только очередей печати), endpoint-агент для
USB/прямых IP-принтеров на пользовательских ПК, и прогнозирование нагрузки/
расходников/простоя. Строится поверх мультиплощадочной архитектуры (см.
[MULTISITE_ARCHITECTURE.md](MULTISITE_ARCHITECTURE.md)) и не меняет её —
`standalone`/`agent`/`central` режимы, существующий журнал заданий, тарифы
и AD/локальная авторизация работают как раньше.

## 1. Зачем разделять устройство и очередь

`PrinterQueue` (уже существовавшая сущность) — это очередь печати Windows:
то, что видит `Get-Printer`/Print Server, привязана к конкретному серверу
или endpoint-агенту (`print_server_id` XOR `endpoint_agent_id`, см.
[MULTISITE_ARCHITECTURE.md](MULTISITE_ARCHITECTURE.md#4-исправленная-идемпотентность-было--потенциальный-баг)).
Одна и та же ОЧЕРЕДЬ может существовать на нескольких серверах (общий
сетевой принтер, опубликованный из нескольких точек), и один и тот же
ФИЗИЧЕСКИЙ принтер может обслуживать несколько очередей (например, отдельные
цветная/чёрно-белая очереди на одном устройстве, или очередь на Print
Server + прямая печать по IP с нескольких ПК на тот же аппарат).

`PrinterDevice` — это физическое устройство: IP/hostname, MAC/серийный
номер, вендор/модель, SNMP-профиль, источник мониторинга. Связь с очередями
— явная таблица `PrinterDeviceQueueLink` (many-to-many, с `is_active` и
аудитом кто/когда связал/отвязал), а НЕ автоматическое сопоставление по
имени очереди или IP. Автосопоставление было бы удобнее, но ненадёжно:
имена очередей произвольны и часто не содержат ни модели, ни IP, а
случайное неверное автосопоставление тихо смешало бы данные о нагрузке
одного устройства со счётчиками совсем другого. Управление связями — в
`printaudit/monitoring/devices.py` (`create_device`, `set_monitoring_source`,
`link_queue`, `unlink_queue`), каждое изменение пишет запись в аудит-лог
(`printaudit.audit`).

## 2. Что и как собирается

| Что | Модель | Обязательность |
|---|---|---|
| Доступность, SNMP/Zabbix статус, замятие/крышка/бумага/аппаратная ошибка, сырой статус источника | `PrinterHealthSample` | тип `Optional` — недоступный OID/item = `unknown`, не `False` |
| Суммарный счётчик страниц, цвет/ч-б счётчики (если поддерживаются) | `PrinterCounterSample` | те же, что и выше — отсутствие данных не значит "0 страниц" |
| Уровень тонера/чернил/др. расходника | `PrinterSupplySample` | `level_percent: Optional[float]`, `level_status` через `classify_supply_level()` (20%=low, 5%=critical, ≤0%=empty, `None`=unknown — НИКОГДА не по умолчанию 0%) |
| Замятия/ошибки/предупреждения как отдельные события с временем открытия/закрытия | `PrinterAlert` | реконсилируется на каждом опросе (открыть новые, оставить существующие, закрыть отсутствующие, **переоткрыть** ранее закрытые той же связки type+external_id — см. ниже) |
| Метаданные одного прогона опроса (сколько устройств, сколько успешно/с ошибкой) | `MonitoringRun` | по одной записи на вызов `collector/monitor_printers.py` |

Опрос по умолчанию — каждые **5 минут** (`collector/monitor_printers.py`,
интервал задаётся при регистрации задачи Task Scheduler, см.
`deploy/register_monitor_printers_task.ps1 -IntervalMinutes`).

### Тонкость реконсиляции алертов

`PrinterAlert` уникален по `(printer_device_id, alert_type, external_id)`.
У Zabbix `external_id` — реальный id проблемы (из `problem.get`), у прямого
SNMP его нет, поэтому используется `external_id = alert_type` (стабильный
псевдо-id). Значит один и тот же тип ошибки может закрыться и открыться
заново много раз за время жизни устройства — если бы реконсиляция всегда
делала `INSERT` для "новых" ключей, второй `INSERT` того же
`(device, alert_type, external_id)` после закрытия нарушил бы уникальность.
`printaudit/monitoring/ingest.py::_reconcile_alerts` поэтому запрашивает
**все** существующие алерты устройства (не только открытые) и для каждого
ключа: нет строки → `INSERT`; строка закрыта → `UPDATE` (переоткрыть,
`resolved_at=None`); строка уже открыта → ничего не делать. Регрессионный
тест: `tests/test_monitoring_ingest.py::test_alert_reopens_without_violating_uniqueness`.

## 3. Источники данных: Zabbix и прямой SNMP

Мониторинг НЕ обязателен и не единственного вида — на площадке настраивается
по устройству через `PrinterDevice.monitoring_source`:

| Значение | Когда использовать |
|---|---|
| `zabbix_api` | На площадке уже есть Zabbix, который мониторит принтеры — не дублируем опрос, читаем то, что уже собрано |
| `direct_snmp` | Zabbix нет — площадка сама опрашивает принтер по SNMP |
| `manual` | Устройство существует в системе (для привязки очередей/отчётов), но телеметрия не собирается |
| `disabled` | Мониторинг выключен |

Оба адаптера (`printaudit/monitoring/zabbix_adapter.py`,
`printaudit/monitoring/snmp_adapter.py`) производят **один и тот же**
`NormalizedDeviceReading` (см. `printaudit/monitoring/normalize.py`) — код
приёма (`ingest.py`) не знает и не должен знать, откуда пришли данные.

### Zabbix (`zabbix_api`)

- Настройка — **только через `.env`**: `ZABBIX_API_URL`, `ZABBIX_API_TOKEN`
  (API-токен, не логин/пароль — создаётся в Zabbix с ролью только на чтение).
  Токен никогда не пишется в лог (см. `ZabbixClient` — ошибки не включают
  токен в текст исключения).
- Соответствие Zabbix host → `PrinterDevice` задаётся через
  `PrinterDevice.zabbix_host_id` при создании/редактировании устройства.
- Читает: последние значения item'ов (`item.get`), активные проблемы
  (`problem.get`), при необходимости историю/тренды. **Никогда** не
  пишет в конфигурацию Zabbix.
- Соответствие item key → метрика настраивается через
  `DEFAULT_ITEM_KEY_MAP` (total_pages/color_pages/bw_pages/toner_black/
  cyan/magenta/yellow) — при нетиповых шаблонах Zabbix передайте свой
  `item_key_map` в `poll_device()`.

### Прямой SNMP (`direct_snmp`)

- **SNMPv3 предпочтителен** (аутентификация+шифрование); community-based
  v2c — только если устройство не поддерживает v3. Учётные данные — в
  `.env` (`SnmpProfile.credentials_env_var` указывает НА ИМЯ переменной
  окружения, не хранит сам секрет в БД/config.yaml).
- Базовый набор OID — Printer-MIB (`DEFAULT_OIDS` в `snmp_adapter.py`),
  вендор-специфичные добавляются через `SnmpProfile.oid_map_json`
  (мержится поверх дефолтов, невалидный JSON — понятная ошибка, не
  тихий откат).
- Ограниченные таймаут/retry/concurrency (`SnmpProfile.timeout_seconds`,
  `.retries`) — не должно "зависать" на недоступном устройстве и не
  должно перегружать сеть площадки параллельными опросами.
- Опрос **всегда с площадки** (тот же принцип, что и у Print Server:
  исходящие HTTPS в центр, никогда не наоборот) — центр никогда не
  открывает соединение к устройству напрямую.
- `pysnmp` — **опциональная** зависимость (`requirements.txt`,
  закомментирована), нужна только площадкам с `direct_snmp`; площадки на
  одном `zabbix_api` её не устанавливают. Реальный SNMP-запрос
  (`_default_snmp_get`) импортирует `pysnmp` лениво, внутри
  `try/except ImportError`, с понятным сообщением, если библиотека не
  установлена.

## 4. Хранение истории и retention

Хранить сырые 5-минутные сэмплы вечно — не вариант (рост БД без предела).
`printaudit/monitoring/retention.py` (`run_retention`, запускается
ежедневно — `scripts/monitoring_retention.py`,
`deploy/register_monitoring_retention_task.ps1`):

1. **Сначала агрегирует** уровни расходников в `PrinterSupplyDailyAgg`
   (мин/среднее/макс/число сэмплов за день) — **до** удаления сырых данных,
   чтобы долгосрочный тренд (нужен и для UI-графика за 90 дней, и для
   прогноза даты исчерпания тонера) пережил очистку.
2. Удаляет сырые `PrinterHealthSample`/`PrinterCounterSample`/
   `PrinterSupplySample` старше `RAW_RETENTION_DAYS` (по умолчанию 30 дней).
3. Удаляет **решённые** алерты старше `RESOLVED_ALERT_RETENTION_DAYS`
   (по умолчанию 180 дней) — активные алерты не удаляются никогда,
   независимо от возраста.

## 5. Передача данных площадка → центр

Расширяет уже существующий протокол агента (`collector/agent_sync.py`,
`webapp/agent_api.py`), **не ломая** версию протокола заданий печати:
`PROTOCOL_VERSION` (задания печати) и `MONITORING_PROTOCOL_VERSION`
(мониторинг) — независимые константы в `printaudit/agent_settings.py`,
расширение одного не требует бампа другого.

Endpoint: `POST /api/v1/agent/monitoring/batch` (тот же bearer-токен
Print Server'а, та же гейтинг-логика `require_central_mode`, тот же лимит
размера тела запроса, что и у `/events/batch`).

**Курсор, не outbox-состояние.** В отличие от заданий печати (полноценный
outbox с pending/failed/retry, см.
[MULTISITE_ARCHITECTURE.md](MULTISITE_ARCHITECTURE.md#5-гарантии-доставки-part-4)),
мониторинговые данные синхронизируются по курсору
(`MonitoringSyncState`, одна строка на площадку): `last_health_sample_id`/
`last_counter_sample_id`/`last_supply_sample_id` (по id — эти таблицы
только INSERT, id монотонно растёт) и `last_alert_synced_at` (по времени —
алерты ещё и обновляются при resolve). Это осознанное упрощение: приём на
центре уже идемпотентен по уникальным ключам таблиц (тот же принцип, что и
для заданий печати), а курсор продвигается ТОЛЬКО после подтверждённого
200 — значит полноценная per-row машина состояний тут просто не нужна.
Устройства пересылаются целиком при каждой отправке (дёшево — обычно
десятки-сотни на площадку), это гарантирует, что центр узнает об
устройстве до первого сэмпла на него.

Центр авто-регистрирует `PrinterDevice` по UUID, СТРОГО в рамках
`site_id` аутентифицированного Print Server — сэмпл/устройство,
ссылающееся на UUID из ДРУГОЙ площадки, тихо пропускается (не валит весь
пакет), см. `webapp/agent_api.py::_resolve_device`.

## 6. Интерфейс

- **`/printers`** — список устройств: фильтры (площадка, Print Server,
  статус, модель, источник мониторинга, поиск), чекбоксы "только с
  активными ошибками"/"только с низким расходником"/"только без данных",
  бейдж источника (Zabbix/SNMP/вручную), полоски уровня расходников,
  заданий/страниц за 30 дней (из `PrintJob` через связанные очереди — НЕ
  из аппаратных счётчиков, это отдельная телеметрия для трендов).
- **`/printers/{id}`** — карточка устройства: техническое состояние
  (замятие/крышка/бумага/ошибка), расходники, активные и недавние
  (30 дней) алерты, связанные очереди с их нагрузкой за период, график
  аппаратного счётчика страниц (Chart.js), и прогнозы (Часть 7 ниже) —
  нагрузка по метрике/горизонту, дата исчерпания расходника, риск
  простоя. Каждый блок прогноза либо показывает число, либо явное
  "Недостаточно данных" — никогда не выдумывает точность.
- **Центральный дашборд** (`/admin`, карточки "Мониторинг принтеров"):
  всего устройств, online/warning/error/offline, устройств с низким
  тонером, с активными ошибками, без данных вообще, площадок с
  проблемами. Print Audit намеренно НЕ дублирует полноценную систему
  алертинга Zabbix — его задача здесь: связать техническое состояние с
  реальной печатью, затраты и прогноз, а не быть second-мониторингом.

Запрос `printaudit/monitoring/device_queries.py` держит "последний сэмпл на
устройство" через `GROUP BY max(id)` + `JOIN` (портируемо между
SQLite/PostgreSQL — в кодовой базе до сих пор избегали оконных функций).

## 7. Endpoint-агент (USB и прямая печать)

MVP-агент для конечных Windows ПК — учитывает печать, которая НЕ проходит
через Print Server: USB, WSD, прямой TCP/IP порт. Отдельный пакет
`endpoint_agent/` (не зависит от `printaudit`/SQLAlchemy/FastAPI — только
stdlib + `pywin32` для службы, чтобы не тянуть тяжёлый стек на
пользовательский ПК).

### Как исключается задвоение с Print Server

Windows пишет Event ID 307 в локальный `Microsoft-Windows-PrintService/
Operational` для ЛЮБОГО задания печати с этого ПК — включая задания на
сетевую очередь Print Server (которую тот же самый Print Server и так уже
учтёт). `endpoint_agent/ports.py::should_capture` классифицирует каждый
принтер этого ПК по `Get-Printer.Type` (`Local` vs `Connection` —
надёжнее, чем разбор имени порта; `endpoint_agent/Get-PrinterPorts.ps1`)
и **исключает** `Connection` (сетевые очереди). Дополнительно
поддерживается allow/denylist по имени принтера
(`PRINTER_ALLOWLIST`/`PRINTER_DENYLIST` в `endpoint_agent.env`, glob-шаблоны).
Принтер, не найденный в снимке портов (устаревший снимок/неизвестное
состояние), тоже ИСКЛЮЧАЕТСЯ по умолчанию — лучше пропустить с записью в
лог, чем рискнуть задвоить.

### Архитектура агента

```
endpoint_agent/
  capture.py    -- читает Event 307 (Export-PrintEvents.ps1, тот же
                    field_map-подход, что у collector/collect_print_events.py)
  ports.py      -- классификация локальный/сетевой + allow/denylist
  outbox.py     -- локальная durable очередь, raw sqlite3 (retryable/
                    terminal-failed — та же семантика, что у серверного
                    OutboxEvent, см. MULTISITE_ARCHITECTURE.md)
  sync_client.py -- stdlib urllib, POST /api/v1/endpoint/events/batch,
                     /heartbeat
  runner.py     -- один цикл захват+отправка
  service.py    -- pywin32 ServiceFramework (работает без окна, переживает
                    logoff/перезагрузку; деградирует в понятную ошибку, а
                    не падает при импорте, если pywin32 не установлен —
                    остальной пакет тестируется без pywin32/Windows)
  main.py       -- консольная точка входа (--once для отладки, иначе цикл)
```

Endpoint-агент шлёт данные на веб-сервер СВОЕЙ площадки
(`POST /api/v1/endpoint/events/batch`/`heartbeat`, `webapp/endpoint_api.py`)
— НИКОГДА не обращается к центру напрямую. Площадка сама пересылает эти
задания в центр как обычные `print_jobs` через уже существующий протокол
агент→центр (`PrintJob.endpoint_agent_id` вместо `print_server_id` —
дуальное scoping-поле по тому же принципу, что и остальные multisite-поля,
см. [MULTISITE_ARCHITECTURE.md](MULTISITE_ARCHITECTURE.md)). Каждое
событие проходит ТОТ ЖЕ пайплайн, что и задание Print Server (тариф,
отдел, `document_name_policy`) — никакого отдельного отчётного пути,
`/print-jobs`/дашборд/отчёты видят endpoint-задания автоматически.

`webapp/endpoint_api.py::require_local_mode` — зеркало
`agent_api.py::require_central_mode`: 404 в `APP_MODE=central` (endpoint-
агенты существуют только там, где есть локальная площадка — standalone/agent).

### Регистрация

Администратор площадки регистрирует агента в **`/admin/endpoint-agents`**
(локальная страница, НЕ центральная `/admin/print-servers`): указывает имя
компьютера, получает `ENDPOINT_UUID` и одноразово показанный
`ENDPOINT_TOKEN` — оба нужно скопировать в `endpoint_agent.env` на целевом
ПК (см. `endpoint_agent/endpoint_agent.env.example`).

### Развёртывание

**Пилот / малое число ПК** — вручную или push-скриптом:
```powershell
# На целевом ПК, из копии репозитория (или общей сетевой папки):
Copy-Item endpoint_agent.env.example endpoint_agent.env
notepad endpoint_agent.env   # заполнить SERVER_BASE_URL/ENDPOINT_UUID/ENDPOINT_TOKEN
.\deploy\install_endpoint_agent.ps1
```
Ставит `PrintAuditEndpointAgent` как службу Windows (`LocalSystem`,
автозапуск), без открытого окна.

**Массовое развёртывание через GPO (рекомендуется для многих ПК):**

1. Соберите `endpoint_agent/` в один исполняемый файл через PyInstaller
   (`pyinstaller --onefile endpoint_agent/service.py`) — устраняет
   зависимость от установленного на целевом ПК Python/pywin32.
   **Эта команда не выполнялась в данном окружении** (PyInstaller не
   установлен) — проверьте сборку на тестовой машине перед раскаткой.
2. Упакуйте бинарник в MSI с ServiceInstall-таблицей — см. скаффолд
   `deploy/endpoint_agent.wxs` (там же подробный комментарий про то,
   почему ENDPOINT_TOKEN НЕ должен ехать внутри одного MSI на много
   машин, и про сборку через WiX Toolset — тоже не выполнялась здесь,
   нужен WiX и сертификат подписи кода организации).
3. Подпишите MSI сертификатом организации (`signtool.exe sign ...`) —
   несигнированные пакеты обычно блокируются политикой AppLocker/
   Defender Application Control на корпоративных ПК.
4. Group Policy Management Console → создайте GPO → Computer Configuration
   → Policies → Software Settings → Software Installation → New →
   Package → укажите путь к подписанному .msi на сетевой шаре, видимой
   компьютерам целевого OU (используйте UNC-путь, не локальный).
5. Свяжите GPO с OU, содержащим целевые компьютеры. Установка происходит
   при следующей перезагрузке (Computer Configuration deployment).
6. Конфигурация (`ENDPOINT_UUID`/`ENDPOINT_TOKEN`) заполняется ОТДЕЛЬНО от
   MSI (см. п.2 в комментарии `endpoint_agent.wxs`) — например, Group
   Policy Preferences → Files (раскатать per-OU заранее подготовленный
   `endpoint_agent.env` для каждой машины/группы машин) или отдельным
   внутренним скриптом выдачи секретов. Регистрация в `/admin/endpoint-agents`
   на площадке всё равно нужна для каждого ПК заранее (иначе токена
   просто не существует).

## 8. Прогнозирование

`printaudit/forecasting/` — намеренно простые, объяснимые baseline-модели
(не "чёрный ящик"), автоматический выбор лучшей по backtest, честное
"недостаточно данных" вместо выдуманной точности.

### Модели

| Модель | Идея |
|---|---|
| `seasonal_naive` | Прогноз = значение того же дня недели `season_length` (по умолчанию 7) дней назад, циклически |
| `moving_average` | Плоский прогноз = среднее последних N дней |
| `exponential_smoothing` | Простое экспоненциальное сглаживание (SES), плоский прогноз |

### Backtest и выбор модели

Rolling-origin backtest (`printaudit/forecasting/backtest.py`): точка
отсчёта сдвигается по истории с шагом `horizon // 4`, на каждой из моделей
обучается ТОЛЬКО на данных до точки, сравнивается с фактом после —
несколько проверок вместо одной случайно удачной/неудачной. Лучшая модель
выбирается по WAPE (weighted absolute percentage error — устойчивее MAPE
на нулевых днях печати), при неопределённом WAPE (весь фактический период
— нули) — по MAE.

### Минимальная история

`printaudit.forecasting.MIN_HISTORY_DAYS = {7: 14, 30: 60, 90: 180}` —
**ровно вдвое** больше горизонта: backtest использует
`min_train_size=horizon_days`, поэтому 2×horizon — минимум, при котором
можно набрать хотя бы одну проверочную точку. История считается от
**реальной первой активности** охвата (`earliest_activity_date` в
`printaudit/forecasting/series.py`), не от искусственного окна — иначе
только что созданная площадка/устройство выглядела бы как имеющая полную
историю из одних нулей. Меньше минимума → `insufficient_history=True`,
`forecast_json` без выдуманных значений — UI показывает "Недостаточно
данных".

### Доверительный интервал

Приближённый: `± 1.96 × MAE` остатков backtest (нормальное приближение),
явно помечен как `confidence_interval_method:
"approximate_normal_backtest_residual"` в `ForecastRun.forecast_json` —
не выдаётся за строгую статистику.

### Охваты и метрики

Разделяются по `scope_type`/`scope_id` в `ForecastRun`: `device` (через
связанные очереди устройства), `queue`, `site`, `organization`
(`scope_id=NULL`). Метрики: `job_count`, `total_pages`, `color_pages`,
`bw_pages` (по `PrintJob.is_color` — `None` не попадает ни в цвет, ни в
ч/б, только в `total_pages`), `cost`. Горизонты: 7/30/90 дней.

**Важная деталь реализации:** upsert `ForecastRun` — явный (запрос-затем-
запись), НЕ полагается на `UniqueConstraint(scope_type, scope_id, metric,
horizon_days)` — для `organization` `scope_id` всегда `NULL`, а `NULL != NULL`
в правилах уникальности SQL, значит ограничение БД не защищает от дублей
organization-строк. Регрессионный тест:
`tests/test_forecasting_pipeline.py::test_organization_scope_upsert_does_not_duplicate_despite_null_scope_id`.

### Дата исчерпания расходника

`printaudit/forecasting/supply.py` — линейная регрессия уровня по времени
на `PrinterSupplyDailyAgg`, **только по точкам после последней замены
картриджа** (скачок уровня вверх больше `RESET_JUMP_THRESHOLD_PERCENT=10%`
трактуется как замена, сбрасывает окно тренда). Не убывающий тренд/
недостаточно точек/точки в один день → `None`, не выдуманная дата.

### Риск простоя и аномальная нагрузка

`printaudit/forecasting/risk.py` — явные эвристики, не ML: доля
health-сэмплов за 14 дней с `is_reachable=False`/`device_status` в
(error, offline) плюс число активных критичных алертов → категория
low/medium/high (не непрозрачная "вероятность"). Аномальная нагрузка —
отклонение факта от базовой линии больше 50%.

### Расписание

`scripts/compute_forecasts.py` (ежедневно,
`deploy/register_compute_forecasts_task.ps1`) — прогнозы считаются по
расписанию, НЕ на каждый просмотр `/printers/{id}`.

## 9. Безопасность и приватность

- Токены Zabbix/SNMP/агентов — **только** в `.env`, никогда в
  `config.yaml`/БД в открытом виде (агентские токены — только хэш, тот же
  принцип, что и `PrintServer`/`EndpointAgent.token_hash`) и никогда в
  логах/тексте исключений (см. `ZabbixClient`, `sync_client.py`).
- Endpoint-агент НЕ передаёт содержимое документов — только метаданные
  задания (имя документа тоже проходит `document_name_policy`, как и у
  Print Server).
- Endpoint-агент не собирает ничего о пользователе ПК, кроме того, что
  нужно для учёта задания (hostname, версия агента, heartbeat) — списки
  процессов, историю браузера, произвольные файлы и т.п. не читает.
- Все административные изменения (создание/связывание устройств, смена
  источника мониторинга, регистрация/ротация/отключение endpoint-агентов)
  пишутся в аудит-лог (`printaudit.audit`).
- Никакого произвольного удалённого выполнения команд — ни у endpoint-
  агента, ни у мониторинга устройств. Обновление endpoint-агента (когда
  понадобится) должно идти через подписанные пакеты (тот же MSI-канал,
  что и первичная установка) и отдельный безопасный механизм, НЕ через
  выполнение произвольной команды/скрипта, присланного с сервера.

## 10. Совместимость

Все новые таблицы и колонки добавлены НОВЫМИ Alembic-миграциями (см.
список ниже) — существующие миграции не менялись. Проверено на копии
реальной БД (см. раздел тестов): апгрейд сохраняет все существующие
`print_jobs`, связи, не создаёт дублей, идемпотентен при повторном запуске,
приложение открывается после миграции. `standalone`/`agent`/`central`,
SQLite/PostgreSQL, AD и локальная авторизация, RBAC/CSRF, старые задания,
тарифы, мультиплощадочная доставка — без изменений.

### Миграции (в порядке применения)

| Ревизия | Что добавляет |
|---|---|
| `4e5a0c10c94f` | `snmp_profiles`, `printer_devices`, `printer_device_queue_links`, `monitoring_runs`, `printer_health_samples`, `printer_counter_samples`, `printer_supply_samples`, `printer_alerts`, `endpoint_agents`, `forecast_runs`; `print_jobs.endpoint_agent_id` + `uq_print_jobs_endpoint_record` |
| `aeb97b6d88e4` | `printer_supply_daily_agg` |
| `ee90f44a0772` | `monitoring_sync_state` |
| `cca38199a688` | `printer_queues.endpoint_agent_id` + `uq_printer_queues_endpoint_name` |

## 11. Тесты

Ни один тест не требует реального принтера, Zabbix или AD — SNMP/Zabbix
опрашиваются через инжектируемые фейки (`getter`/`transport`/`client`),
endpoint-агент читает Windows Event Log через инжектируемый `runner`.

| Файл | Что проверяет |
|---|---|
| `test_printer_devices.py` | Создание устройств, аудит, связывание/отвязывание очередей (relink переиспользует строку), `compute_device_status`, `classify_supply_level` |
| `test_monitoring_ingest.py` | Идемпотентная запись сэмплов, unsupported OID = unknown не 0, реконсиляция алертов (открыть/оставить/закрыть/**переоткрыть**) |
| `test_zabbix_adapter.py`, `test_snmp_adapter.py` | Нормализация, отсутствующий item/OID не задаёт 0, одна плохая метрика не валит весь опрос, токен не в логах |
| `test_monitor_printers.py` | Опрашиваются только zabbix/snmp устройства, одно упавшее не останавливает остальные |
| `test_monitoring_retention.py` | Агрегация ДО удаления, идемпотентность, активные алерты не удаляются |
| `test_agent_monitoring_api.py`, `test_agent_sync_monitoring.py` | Приём на центре с версионированием протокола и cross-site защитой, курсор синхронизации |
| `test_endpoint_agent_*.py` (7 файлов) | Конфигурация, классификация портов (USB/WSD/IP vs сетевая очередь), захват событий, локальная очередь, HTTP-клиент, полный цикл (`runner.py`), **сквозной сценарий** против реального `webapp/endpoint_api.py` |
| `test_endpoint_api.py` | Приём заданий endpoint-агента на сервере площадки, изоляция очередей от Print Server, outbox только в agent-режиме |
| `test_monitoring_device_queries.py`, `test_printers_page.py` | Список/карточка устройства, фильтры, central-дашборд |
| `test_forecasting_*.py` (7 файлов) | Baseline-модели, backtest/выбор модели, доверительный интервал, недостаточная история (граница ровно на минимуме), тренд расходника со сбросом на замену, риск простоя, полный pipeline с идемпотентным upsert (включая NULL-scope_id регрессию) |

### Сквозной сценарий (демонстрация)

`tests/test_endpoint_agent_e2e.py::test_usb_job_counted_once_network_queue_excluded_survives_network_loss`
— против настоящего `webapp/endpoint_api.py` (через `TestClient`, не мок):
2 события на тестовом ПК (одно с USB-принтера, одно с сетевой очереди
Print Server) → захват исключает сетевую очередь (не задвоение) → первая
попытка отправки падает по сети → событие остаётся в локальной очереди
(не потеряно) → вторая попытка успешна → ровно один `PrintJob`
(`endpoint_agent_id` заполнен, `print_server_id` пуст) → повторный цикл не
создаёт дублей.
