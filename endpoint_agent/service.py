"""Windows Service обёртка endpoint-агента (pywin32) — работает без
открытого окна, переживает logoff пользователя и перезапуск ПК (см.
docs/PRINTER_MONITORING_FORECASTING.md, часть 6). Регистрация — через
deploy/install_endpoint_agent.ps1 (вызывает `python service.py install` /
`--startup=auto`), или через тот же скрипт для установки из подписанного
MSI (см. deploy/endpoint_agent.wxs).

pywin32 — единственная сторонняя зависимость всего пакета endpoint_agent
(см. endpoint_agent/requirements.txt) и импортируется здесь, а не в
endpoint_agent.runner/capture/outbox/sync_client — те тестируются на любой
ОС без pywin32 установленного; только сама служба требует Windows."""
import sys
import threading
from pathlib import Path

try:
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil
except ImportError:  # pragma: no cover - pywin32 недоступен вне Windows/venv установки
    servicemanager = None
    win32event = None
    win32service = None
    win32serviceutil = None


_ServiceBase = win32serviceutil.ServiceFramework if win32serviceutil is not None else object


class EndpointAgentService(_ServiceBase):
    _svc_name_ = "PrintAuditEndpointAgent"
    _svc_display_name_ = "Print Audit Endpoint Agent"
    _svc_description_ = (
        "Учитывает печать через USB/WSD/прямые IP-принтеры этого ПК для Print Audit. "
        "Не собирает содержимое документов."
    )

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = threading.Event()
        self._thread = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
        win32event.SetEvent(self.hWaitStop) if hasattr(self, "hWaitStop") else None

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        from endpoint_agent.main import run_forever

        config_path = Path(__file__).resolve().parent / "endpoint_agent.env"
        self._thread = threading.Thread(
            target=run_forever, args=(config_path, self.stop_event), daemon=True,
        )
        self._thread.start()
        self._thread.join()


if __name__ == "__main__":
    if win32serviceutil is None:
        print("pywin32 не установлен — см. endpoint_agent/requirements.txt", file=sys.stderr)
        sys.exit(1)
    win32serviceutil.HandleCommandLine(EndpointAgentService)
