"""Опрос физических принтеров ЭТОЙ площадки (Zabbix API и/или direct SNMP) —
запускается по расписанию (Task Scheduler, по умолчанию каждые 5 минут, см.
deploy/register_monitor_printers_task.ps1), НА СЕРВЕРЕ ПЛОЩАДКИ. Опрос
никогда не выполняется из центра — центр не открывает исходящих SNMP-
соединений и не имеет входящих к площадкам (см.
docs/PRINTER_MONITORING_FORECASTING.md).

Каждый запуск, отдельно для каждого настроенного источника
(monitoring_source=zabbix_api / direct_snmp — "manual"/"disabled"
устройства пропускаются автоматически):
  1. берёт все активные PrinterDevice этой площадки с этим источником;
  2. опрашивает каждое (см. printaudit.monitoring.zabbix_adapter /
     printaudit.monitoring.snmp_adapter) — один недоступный/неподдерживаемый
     показатель не проваливает весь опрос устройства, а недоступное
     устройство не проваливает весь прогон;
  3. пишет нормализованные показания идемпотентно (см.
     printaudit.monitoring.ingest);
  4. фиксирует прогон в monitoring_runs (аналог sync_runs для печати).

Не требует Zabbix ИЛИ SNMP одновременно — площадка может использовать
только один источник, оба, или ни одного (тогда скрипт просто ничего не
опрашивает и завершается штатно)."""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from printaudit.config import get_settings  # noqa: E402
from printaudit.database import SessionLocal  # noqa: E402
from printaudit.models import MonitoringRun, PrinterDevice  # noqa: E402
from printaudit.monitoring import MONITORING_SOURCE_SNMP, MONITORING_SOURCE_ZABBIX  # noqa: E402
from printaudit.monitoring import snmp_adapter, zabbix_adapter  # noqa: E402
from printaudit.monitoring.ingest import ingest_reading  # noqa: E402
from printaudit.sites import get_or_create_local_print_server  # noqa: E402
from printaudit.timeutil import utcnow  # noqa: E402


def setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("monitor_printers")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(log_dir / "monitor_printers.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)
    return logger


def _get_zabbix_client(client=None):
    if client is not None:
        return client
    import os

    base_url = os.environ.get("ZABBIX_API_URL")
    token = os.environ.get("ZABBIX_API_TOKEN")
    if not base_url or not token:
        return None
    return zabbix_adapter.ZabbixClient(base_url, token)


def _poll_zabbix_devices(session, site, devices, log, client=None) -> None:
    started = utcnow()
    run = MonitoringRun(site_id=site.id, source=MONITORING_SOURCE_ZABBIX, started_at=started, status="running")
    session.add(run)
    # commit (не только flush) -- ниже, если опрос устройства провалится,
    # session.rollback() откатывает ВЕСЬ текущий незакоммиченный transaction;
    # если бы run был только flush'нут, этот rollback стирал бы и саму
    # запись MonitoringRun (баг, вскрытый требованием не иметь неявных
    # fallback-ов при ошибках конфигурации SNMP -- см. snmp_adapter.py).
    session.commit()

    zbx = _get_zabbix_client(client)
    if zbx is None:
        run.status = "failed"
        run.finished_at = utcnow()
        run.error_message = "ZABBIX_API_URL/ZABBIX_API_TOKEN не заданы в .env — опрос через Zabbix пропущен"
        session.commit()
        log.error(run.error_message)
        return

    ok = failed = 0
    for device in devices:
        if not device.zabbix_host_id:
            failed += 1
            log.warning("Устройство %s: monitoring_source=zabbix_api, но zabbix_host_id не задан", device.id)
            continue
        try:
            reading = zabbix_adapter.poll_device(zbx, device.zabbix_host_id)
            ingest_reading(session, device, reading, monitoring_run_id=run.id)
            session.commit()
            ok += 1
        except Exception as exc:  # noqa: BLE001 - один принтер не должен ронять весь опрос
            session.rollback()
            failed += 1
            log.warning("Опрос Zabbix для устройства %s не удался: %s", device.id, exc)

    run = session.get(MonitoringRun, run.id)
    run.devices_polled = len(devices)
    run.devices_ok = ok
    run.devices_failed = failed
    run.status = "success"
    run.finished_at = utcnow()
    session.commit()
    log.info("Zabbix: опрошено=%d ок=%d ошибок=%d", len(devices), ok, failed)


def _poll_snmp_devices(session, site, devices, log, getter=None) -> None:
    started = utcnow()
    run = MonitoringRun(site_id=site.id, source=MONITORING_SOURCE_SNMP, started_at=started, status="running")
    session.add(run)
    # commit, не flush -- см. комментарий в _poll_zabbix_devices выше (тот
    # же паттерн, тот же риск потерять саму строку MonitoringRun при
    # rollback после сбоя опроса одного устройства).
    session.commit()

    ok = failed = 0
    for device in devices:
        try:
            reading = snmp_adapter.poll_device(device, device.snmp_profile, getter=getter)
            ingest_reading(session, device, reading, monitoring_run_id=run.id)
            session.commit()
            ok += 1
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            failed += 1
            log.warning("Опрос SNMP для устройства %s не удался: %s", device.id, exc)

    run = session.get(MonitoringRun, run.id)
    run.devices_polled = len(devices)
    run.devices_ok = ok
    run.devices_failed = failed
    run.status = "success"
    run.finished_at = utcnow()
    session.commit()
    log.info("direct_snmp: опрошено=%d ок=%d ошибок=%d", len(devices), ok, failed)


def run_once(zabbix_client=None, snmp_getter=None) -> None:
    settings = get_settings()
    log = setup_logging(settings.log_dir)
    session = SessionLocal()
    try:
        local_server = get_or_create_local_print_server(session, settings)
        site = local_server.site

        zabbix_devices = (
            session.query(PrinterDevice)
            .filter_by(site_id=site.id, is_active=True, monitoring_source=MONITORING_SOURCE_ZABBIX)
            .all()
        )
        snmp_devices = (
            session.query(PrinterDevice)
            .filter_by(site_id=site.id, is_active=True, monitoring_source=MONITORING_SOURCE_SNMP)
            .all()
        )

        if zabbix_devices:
            _poll_zabbix_devices(session, site, zabbix_devices, log, client=zabbix_client)
        if snmp_devices:
            _poll_snmp_devices(session, site, snmp_devices, log, getter=snmp_getter)
        if not zabbix_devices and not snmp_devices:
            log.info("Нет устройств с monitoring_source=zabbix_api/direct_snmp на этой площадке — опрос пропущен.")
    finally:
        session.close()


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    run_once()
