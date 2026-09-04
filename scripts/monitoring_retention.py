"""Retention для мониторинговых сэмплов принтеров — запускается по
расписанию (Task Scheduler, раз в сутки, см.
deploy/register_monitoring_retention_task.ps1). Агрегирует уровни
расходников старше окна хранения в printer_supply_daily_agg, затем удаляет
сырые сэмплы (health/counter/supply) и старые РЕШЁННЫЕ алерты — см.
printaudit/monitoring/retention.py за точными правилами и порядком.

Использование:
    python scripts\\monitoring_retention.py
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from printaudit.config import get_settings  # noqa: E402
from printaudit.database import SessionLocal  # noqa: E402
from printaudit.monitoring.retention import run_retention  # noqa: E402


def setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("monitoring_retention")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(log_dir / "monitoring_retention.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)
    return logger


def main() -> None:
    settings = get_settings()
    log = setup_logging(settings.log_dir)
    session = SessionLocal()
    try:
        result = run_retention(session)
        log.info(
            "Retention завершён: агрегировано дней расходников=%d, удалено health=%d counter=%d supply=%d, "
            "удалено решённых алертов=%d",
            result["aggregated_supply_days"], result["health"], result["counter"], result["supply"],
            result["purged_alerts"],
        )
    finally:
        session.close()


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    main()
