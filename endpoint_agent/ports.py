"""Классификация принтеров этого ПК на «локальный/прямой» (USB, WSD,
Standard TCP/IP — учитывается endpoint-агентом) и «сетевая очередь»
(подключение к очереди Print Server — уже учитывается самим Print Server,
здесь ИСКЛЮЧАЕТСЯ, чтобы не задваивать подсчёт, см.
docs/PRINTER_MONITORING_FORECASTING.md, часть 6).

Источник истины — Win32_Printer.PortName/Type с самого ПК (см.
endpoint_agent/Get-PrinterPorts.ps1), а не эвристика по имени: `Type` у
Windows уже надёжно отличает "Connection" (сетевое подключение к чужой
очереди) от локального порта. Эвристика по имени порта — только запасной
вариант, если по какой-то причине Type недоступен (например, старый драйвер)."""
import fnmatch
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

NETWORK_PORT_TYPE = "Connection"
LOCAL_PORT_TYPE = "Local"

# Признаки сетевого порта по имени — запасной эвристический вариант, когда
# Type недоступен. \\server\queue — классический UNC-порт клиентского
# подключения к чужой очереди.
_NETWORK_PORT_NAME_PREFIXES = ("\\\\",)


@dataclass(frozen=True)
class PortInfo:
    printer_name: str
    port_name: str
    port_type: Optional[str] = None  # "Local" | "Connection" | None (неизвестно)


def is_network_port(info: PortInfo) -> bool:
    if info.port_type:
        return info.port_type == NETWORK_PORT_TYPE
    return info.port_name.startswith(_NETWORK_PORT_NAME_PREFIXES)


REASON_OK = "ok"
REASON_DENYLISTED = "denylisted"
REASON_NOT_ALLOWLISTED = "not_allowlisted"
REASON_UNKNOWN_PORT = "unknown_port"
REASON_NETWORK_QUEUE_EXCLUDED = "network_queue_excluded"


def should_capture(
    printer_name: str,
    port_map: Dict[str, PortInfo],
    allowlist: List[str],
    denylist: List[str],
) -> Tuple[bool, str]:
    """Решает, учитывать ли задание, напечатанное на `printer_name` этого ПК.

    Порядок проверок намеренно консервативен: явный запрет и отсутствие в
    allowlist проверяются раньше классификации порта, а неизвестный порт
    (принтер не найден в снимке Get-Printer) по умолчанию ИСКЛЮЧАЕТСЯ —
    лучше молча пропустить job с диагностикой в логе, чем случайно задвоить
    его с Print Server, если снимок портов устарел/не удался."""
    for pattern in denylist:
        if fnmatch.fnmatch(printer_name, pattern):
            return False, REASON_DENYLISTED

    if allowlist and not any(fnmatch.fnmatch(printer_name, pattern) for pattern in allowlist):
        return False, REASON_NOT_ALLOWLISTED

    info = port_map.get(printer_name)
    if info is None:
        return False, REASON_UNKNOWN_PORT

    if is_network_port(info):
        return False, REASON_NETWORK_QUEUE_EXCLUDED

    return True, REASON_OK
