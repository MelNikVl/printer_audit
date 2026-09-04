"""MVP endpoint-агент для пользовательских Windows ПК (Part 6,
docs/PRINTER_MONITORING_FORECASTING.md).

Намеренно НЕ зависит от printaudit/webapp/SQLAlchemy — работает на обычном
пользовательском компьютере, а не на сервере площадки, поэтому у пакета
свой минимальный набор зависимостей (см. endpoint_agent/requirements.txt):
только pywin32 (нужен для чтения журнала событий и обёртки Windows Service).
HTTP и локальная очередь используют исключительно стандартную библиотеку
(urllib, sqlite3) — без httpx/SQLAlchemy, которые тянут за собой лишний вес
на конечный ПК.

Учитывает только USB/WSD/прямые IP-принтеры этого ПК (см. endpoint_agent.ports)
— задания на сетевые очереди (\\\\server\\printer) уже учитываются Print
Server-ом той же площадки и здесь намеренно исключаются, чтобы не задваивать
подсчёт (см. docs/PRINTER_MONITORING_FORECASTING.md, часть 6)."""

AGENT_VERSION = "1.0"
