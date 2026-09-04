"""Точка входа endpoint-агента: консольный запуск (отладка/`--once` для
разовой проверки) и цикл, который использует endpoint_agent.service для
запуска как Windows Service в production (см. deploy/install_endpoint_agent.ps1,
docs/PRINTER_MONITORING_FORECASTING.md)."""
import argparse
import logging
import sys
import time
from pathlib import Path

from endpoint_agent import outbox
from endpoint_agent.config import ConfigError, load_config
from endpoint_agent.runner import run_cycle

DEFAULT_CONFIG_NAME = "endpoint_agent.env"


def setup_logging(log_dir: Path, level: str) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("endpoint_agent")
    if logger.handlers:
        return logger
    logger.setLevel(getattr(logging, level, logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(log_dir / "endpoint_agent.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)
    return logger


def run_forever(config_path: Path, stop_event=None) -> None:
    """stop_event — необязательный threading.Event/аналог с .is_set(),
    позволяющий Windows Service корректно останавливать цикл (см.
    endpoint_agent/service.py::SvcStop)."""
    cfg = load_config(config_path)
    log = setup_logging(cfg.log_dir, cfg.log_level)
    conn = outbox.open_db(cfg.db_path)
    log.info("endpoint_agent запущен: hostname=%s server=%s", cfg.hostname, cfg.server_base_url)
    try:
        while stop_event is None or not stop_event.is_set():
            try:
                run_cycle(cfg, conn, log)
            except Exception:  # noqa: BLE001 - один сбойный цикл не должен останавливать агента
                log.exception("Сбой цикла — будет повторён через %d сек.", cfg.poll_interval_seconds)
            waited = 0.0
            while waited < cfg.poll_interval_seconds:
                if stop_event is not None and stop_event.is_set():
                    break
                time.sleep(min(1.0, cfg.poll_interval_seconds - waited))
                waited += 1.0
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(DEFAULT_CONFIG_NAME))
    parser.add_argument("--once", action="store_true", help="Один цикл захвата+отправки и выход (для отладки).")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 1

    if not args.once:
        run_forever(args.config)
        return 0

    log = setup_logging(cfg.log_dir, cfg.log_level)
    conn = outbox.open_db(cfg.db_path)
    try:
        run_cycle(cfg, conn, log)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
