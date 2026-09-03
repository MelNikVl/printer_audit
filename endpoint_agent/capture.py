"""Захват новых событий печати и снимка портов принтеров этого ПК.

И `fetch_new_events`, и `fetch_port_map` принимают опциональный `runner`
(callable(list[str]) -> str, имитирующий subprocess.run(...).stdout) —
единственная точка, где тестам нужно подменить обращение к PowerShell/
Windows, весь остальной код (разбор JSON, field_map, классификация)
тестируется без реального Windows-окружения (тот же приём, что и в
collector/collect_print_events.py и printaudit/monitoring/snmp_adapter.py)."""
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from endpoint_agent.config import EndpointAgentConfig
from endpoint_agent.ports import PortInfo

_EXPORT_EVENTS_SCRIPT = Path(__file__).resolve().parent / "Export-PrintEvents.ps1"
_GET_PORTS_SCRIPT = Path(__file__).resolve().parent / "Get-PrinterPorts.ps1"

Runner = Callable[[List[str]], str]


class CaptureError(RuntimeError):
    pass


class FieldMapError(CaptureError):
    """См. collector/collect_print_events.py::FieldMapError — та же причина:
    индексы Properties события 307 различаются между версиями Windows/
    драйвера и должны быть откалиброваны (calibrate_event_fields.ps1)."""


@dataclass
class RawPrintEvent:
    record_id: int
    time_created: datetime
    job_id: Optional[str]
    document_name: Optional[str]
    user_name: str
    printer_name: str
    total_pages: int


def _run_powershell(script: Path, args: List[str]) -> str:
    cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise CaptureError(f"{script.name} завершился с ошибкой: {result.stderr.strip()}")
    return result.stdout


def _parse_json_array(raw: str, source_name: str) -> list:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CaptureError(f"{source_name} вернул невалидный JSON: {exc}. Начало вывода: {raw[:500]!r}") from exc
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise CaptureError(f"{source_name} вернул JSON неожиданного типа {type(data).__name__}")
    return data


def _get_field(properties: list, field_map: Dict[str, int], name: str, default=None):
    idx = field_map.get(name)
    if idx is None:
        return default
    if idx < 0 or idx >= len(properties):
        raise FieldMapError(
            f"field_map.{name}={idx} вне диапазона: событие содержит только {len(properties)} "
            f"свойств. Индексы полей отличаются между Windows/драйверами — перекалибруйте "
            f"FIELD_MAP_* в endpoint_agent.env (см. collector/calibrate_event_fields.ps1)."
        )
    return properties[idx]


def fetch_new_events(cfg: EndpointAgentConfig, after_record_id: int, runner: Optional[Runner] = None) -> List[dict]:
    """Возвращает СЫРЫЕ события (ещё не разобранные field_map) — тот же
    контракт, что у Export-PrintEvents.ps1: список {RecordId, TimeCreated,
    Properties}. Разбор в RawPrintEvent — отдельным шагом (parse_raw_event),
    чтобы одно плохое событие не прерывало обработку остальных пачки."""
    args = [
        "-LogName", cfg.event_log_name, "-EventId", str(cfg.event_id),
        "-AfterRecordId", str(after_record_id), "-MaxEvents", str(cfg.max_events_per_run),
    ]
    raw = runner(args) if runner is not None else _run_powershell(_EXPORT_EVENTS_SCRIPT, args)
    return _parse_json_array(raw, "Export-PrintEvents.ps1")


def parse_raw_event(evt: dict, field_map: Dict[str, int]) -> RawPrintEvent:
    record_id = evt["RecordId"]
    props = evt.get("Properties", [])

    job_id = _get_field(props, field_map, "job_id")
    document_name = _get_field(props, field_map, "document_name", "") or ""
    user_name = str(_get_field(props, field_map, "user_name", "") or "").strip()
    printer_name = str(_get_field(props, field_map, "printer_name", "") or "").strip()
    total_pages_raw = _get_field(props, field_map, "total_pages", 0)
    total_pages = int(total_pages_raw) if total_pages_raw not in (None, "") else 0

    if not user_name or not printer_name:
        raise CaptureError(f"RecordId={record_id}: пустой user_name или printer_name")

    time_created = datetime.strptime(evt["TimeCreated"], "%Y-%m-%dT%H:%M:%S.%f%z")

    return RawPrintEvent(
        record_id=record_id, time_created=time_created, job_id=str(job_id) if job_id is not None else None,
        document_name=document_name, user_name=user_name, printer_name=printer_name, total_pages=total_pages,
    )


def fetch_port_map(runner: Optional[Runner] = None) -> Dict[str, PortInfo]:
    raw = runner([]) if runner is not None else _run_powershell(_GET_PORTS_SCRIPT, [])
    items = _parse_json_array(raw, "Get-PrinterPorts.ps1")
    port_map: Dict[str, PortInfo] = {}
    for item in items:
        name = str(item.get("Name") or "").strip()
        if not name:
            continue
        port_map[name] = PortInfo(
            printer_name=name,
            port_name=str(item.get("PortName") or ""),
            port_type=(str(item.get("Type")) if item.get("Type") not in (None, "") else None),
        )
    return port_map
