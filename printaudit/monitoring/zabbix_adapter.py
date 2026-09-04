"""Read-only адаптер источника zabbix_api — превращает данные Zabbix API
(latest values через item.get, активные проблемы через problem.get) в
NormalizedDeviceReading (см. printaudit.monitoring.normalize). Никогда не
пишет и не меняет конфигурацию Zabbix — используются только item.get/
problem.get (history.get/trend.get доступны через get_history для
бэктеста прогнозов, тоже read-only).

ZABBIX_API_URL/ZABBIX_API_TOKEN — только из .env (см. .env.example),
токен НИКОГДА не логируется и не появляется в тексте исключений, которые
могут дойти до UI (см. ZabbixApiError — сообщение нейтральное, детали
только через logger.error)."""
import logging
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from printaudit.monitoring import MONITORING_SOURCE_ZABBIX, classify_supply_level
from printaudit.monitoring.normalize import NormalizedAlertReading, NormalizedDeviceReading, NormalizedSupplyReading

logger = logging.getLogger("printaudit.monitoring.zabbix")

# Типичные ключи Zabbix-итемов для принтерных SNMP-шаблонов — НЕ гарантия
# для конкретного инстанса Zabbix, тот же смысл, что и field_map коллектора
# событий печати: сверяется/донастраивается администратором один раз.
DEFAULT_ITEM_KEY_MAP: Dict[str, str] = {
    "total_pages": "printer.pages.total",
    "color_pages": "printer.pages.color",
    "bw_pages": "printer.pages.bw",
    "toner_black": "printer.supply.toner.black",
    "toner_cyan": "printer.supply.toner.cyan",
    "toner_magenta": "printer.supply.toner.magenta",
    "toner_yellow": "printer.supply.toner.yellow",
}

SUPPLY_ITEM_KEYS = ("toner_black", "toner_cyan", "toner_magenta", "toner_yellow")


class ZabbixApiError(RuntimeError):
    """Сбой обращения к Zabbix API — сообщение НЕ должно долетать до UI как
    есть (см. webapp/errors.py::safe_error_message для того же принципа)."""


class ZabbixClient:
    """Тонкая обёртка над JSON-RPC Zabbix API. `transport(method, params)`
    внедряется — по умолчанию реальный HTTP через httpx, в тестах фейк без
    сети (тот же паттерн, что и fetch_local_printers/fetch_events в
    коллекторе)."""

    def __init__(self, base_url: str, token: str, transport: Optional[Callable] = None, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._transport = transport or self._http_transport

    def _http_transport(self, method: str, params: dict) -> dict:
        import httpx

        try:
            resp = httpx.post(
                f"{self.base_url}/api_jsonrpc.php",
                json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json-rpc"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise ZabbixApiError(f"Zabbix API недоступен: {exc}") from exc
        if "error" in data:
            logger.error("Zabbix API error for %s: %s", method, data["error"])
            raise ZabbixApiError(f"Zabbix API вернул ошибку для {method}")
        return data.get("result", [])

    def call(self, method: str, params: dict):
        return self._transport(method, params)

    def get_latest_items(self, host_id: str) -> List[dict]:
        return self.call("item.get", {"hostids": [host_id], "output": "extend"}) or []

    def get_active_problems(self, host_id: str) -> List[dict]:
        return self.call(
            "problem.get", {"hostids": [host_id], "output": "extend", "recent": False, "sortfield": "eventid"},
        ) or []

    def get_history(self, item_id: str, time_from: int, time_till: int, history_type: int = 0) -> List[dict]:
        """history.get (0=float, 3=unsigned) для бэктеста/прогноза счётчика
        страниц по данным Zabbix, если понадобится — read-only, как и всё
        остальное здесь."""
        return self.call(
            "history.get",
            {"itemids": [item_id], "time_from": time_from, "time_till": time_till, "output": "extend", "history": history_type},
        ) or []


def _item_value_by_key(items: List[dict], key: str) -> Optional[float]:
    if not key:
        return None
    for item in items:
        if item.get("key_") == key:
            raw = item.get("lastvalue")
            if raw in (None, ""):
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
    return None


def _zabbix_severity(raw) -> str:
    try:
        level = int(raw)
    except (TypeError, ValueError):
        return "warning"
    if level >= 4:
        return "critical"
    if level >= 2:
        return "warning"
    return "info"


def poll_device(
    client: ZabbixClient, host_id: str, item_key_map: Optional[Dict[str, str]] = None,
) -> NormalizedDeviceReading:
    """Опрашивает ОДИН хост Zabbix. Итем, которого нет в шаблоне на данном
    хосте, даёт None (не 0) — см. printaudit.monitoring.normalize."""
    item_key_map = item_key_map or DEFAULT_ITEM_KEY_MAP
    now = datetime.now(timezone.utc)

    try:
        items = client.get_latest_items(host_id)
    except ZabbixApiError:
        return NormalizedDeviceReading(
            collected_at=now, source=MONITORING_SOURCE_ZABBIX, is_reachable=None, device_status="unknown",
            raw_status_text="Zabbix API недоступен",
        )

    if not items:
        return NormalizedDeviceReading(
            collected_at=now, source=MONITORING_SOURCE_ZABBIX, is_reachable=None, device_status="unknown",
            raw_status_text="Zabbix не вернул ни одного итема для этого хоста",
        )

    try:
        problems = client.get_active_problems(host_id)
    except ZabbixApiError:
        problems = []

    total_pages = _item_value_by_key(items, item_key_map.get("total_pages", ""))
    color_pages = _item_value_by_key(items, item_key_map.get("color_pages", ""))
    bw_pages = _item_value_by_key(items, item_key_map.get("bw_pages", ""))

    supplies = []
    for supply_type in SUPPLY_ITEM_KEYS:
        key = item_key_map.get(supply_type)
        if not key:
            continue
        level = _item_value_by_key(items, key)
        supplies.append(
            NormalizedSupplyReading(
                supply_type=supply_type, level_percent=level,
                level_status=classify_supply_level(level) if level is not None else "unknown",
            )
        )

    alerts = [
        NormalizedAlertReading(
            alert_type=(p.get("name") or "zabbix_problem")[:40],
            severity=_zabbix_severity(p.get("severity")),
            message=p.get("name"),
            external_id=str(p.get("eventid") or ""),
        )
        for p in problems
    ]

    device_status = "error" if any(a.severity == "critical" for a in alerts) else ("warning" if alerts else "online")

    return NormalizedDeviceReading(
        collected_at=now, source=MONITORING_SOURCE_ZABBIX, is_reachable=True, device_status=device_status,
        total_pages=int(total_pages) if total_pages is not None else None,
        color_pages=int(color_pages) if color_pages is not None else None,
        bw_pages=int(bw_pages) if bw_pages is not None else None,
        supplies=supplies, alerts=alerts,
    )
