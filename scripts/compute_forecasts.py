"""Пересчёт прогнозов нагрузки/расходников/простоя — запускается по
расписанию (Task Scheduler, раз в сутки, см.
deploy/register_compute_forecasts_task.ps1), НЕ на каждый просмотр
страницы /printers (см. printaudit/forecasting/pipeline.py). Считает
job_count/total_pages/color_pages/bw_pages/cost на горизонтах 7/30/90 дней
по каждому устройству/очереди/площадке и в целом по организации, плюс
прогноз исчерпания расходников и риск простоя по каждому устройству.

Использование:
    python scripts\\compute_forecasts.py
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from printaudit.config import get_settings  # noqa: E402
from printaudit.database import SessionLocal  # noqa: E402
from printaudit.forecasting.pipeline import compute_all_forecasts  # noqa: E402


def setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("compute_forecasts")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(log_dir / "compute_forecasts.log", encoding="utf-8")
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
        counts = compute_all_forecasts(session)
        log.info(
            "Прогнозы пересчитаны: устройств=%d очередей=%d площадок=%d organization=%d",
            counts["devices"], counts["queues"], counts["sites"], counts["organization"],
        )
    except Exception:
        session.rollback()
        log.exception("Сбой пересчёта прогнозов")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    main()
