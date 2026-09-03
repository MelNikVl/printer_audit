"""Диагностика соединения агента (APP_MODE=agent) с центральным Print Audit.

Проверяет, по порядку, и печатает [OK]/[FAIL] на каждый шаг, не прерываясь
на первой ошибке (чтобы сразу увидеть все проблемы конфигурации разом):
  1. APP_MODE=agent и все обязательные переменные заданы в .env;
  2. DNS резолвится для хоста из CENTRAL_BASE_URL;
  3. TCP-соединение с этим хостом:портом устанавливается;
  4. если https — TLS handshake проходит;
  5. реальный вызов POST /api/v1/agent/heartbeat токеном из AGENT_TOKEN
     (без побочных эффектов, кроме обновления heartbeat на центральном
     сервере — это ожидаемо и безопасно).

Ничего из секретов (AGENT_TOKEN) не печатается и не попадает в вывод даже
при ошибке. Использование:

    python scripts\\agent_diagnose.py
"""
import socket
import ssl
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from printaudit.agent_settings import AGENT_VERSION, PROTOCOL_VERSION, get_agent_settings  # noqa: E402


def _ok(label: str) -> None:
    print(f"[OK]   {label}")


def _fail(label: str, detail: str = "") -> None:
    suffix = f" — {detail}" if detail else ""
    print(f"[FAIL] {label}{suffix}")


def main() -> int:
    agent_settings = get_agent_settings()
    had_failure = False

    if agent_settings.mode != "agent":
        _fail("APP_MODE=agent", f"текущее значение: {agent_settings.mode!r}")
        print("Остальные проверки бессмысленны без APP_MODE=agent — исправьте .env и повторите.")
        return 1
    _ok("APP_MODE=agent")

    missing = [
        name
        for name, value in (
            ("CENTRAL_BASE_URL", agent_settings.central_base_url),
            ("AGENT_SITE_UUID", agent_settings.site_uuid),
            ("AGENT_PRINT_SERVER_UUID", agent_settings.print_server_uuid),
            ("AGENT_TOKEN", agent_settings.token),
        )
        if not value
    ]
    if missing:
        _fail("Обязательные переменные .env заданы", f"отсутствуют: {', '.join(missing)}")
        return 1
    _ok("Обязательные переменные .env заданы (CENTRAL_BASE_URL/AGENT_SITE_UUID/AGENT_PRINT_SERVER_UUID/AGENT_TOKEN)")

    parsed = urlparse(agent_settings.central_base_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        socket.getaddrinfo(host, port)
        _ok(f"DNS резолвится: {host}")
    except OSError as exc:
        _fail(f"DNS резолвится: {host}", str(exc))
        had_failure = True

    try:
        with socket.create_connection((host, port), timeout=10):
            pass
        _ok(f"TCP-соединение: {host}:{port}")
    except OSError as exc:
        _fail(f"TCP-соединение: {host}:{port}", str(exc))
        had_failure = True
        # Без TCP TLS/heartbeat всё равно провалятся — но продолжаем, чтобы
        # видеть полную картину, а не только первую ошибку.

    if parsed.scheme == "https":
        try:
            context = ssl.create_default_context()
            with socket.create_connection((host, port), timeout=10) as sock:
                with context.wrap_socket(sock, server_hostname=host):
                    pass
            _ok(f"TLS handshake: {host}:{port}")
        except (OSError, ssl.SSLError) as exc:
            _fail(f"TLS handshake: {host}:{port}", str(exc))
            had_failure = True
    else:
        _fail("CENTRAL_BASE_URL использует https", "используется http — недопустимо для продакшена")
        had_failure = True

    try:
        import httpx

        with httpx.Client() as client:
            resp = client.post(
                f"{agent_settings.central_base_url.rstrip('/')}/api/v1/agent/heartbeat",
                json={
                    "protocol_version": PROTOCOL_VERSION,
                    "site_uuid": agent_settings.site_uuid,
                    "print_server_uuid": agent_settings.print_server_uuid,
                    "agent_version": AGENT_VERSION,
                    "pending_queue_size": 0,
                },
                headers={"Authorization": f"Bearer {agent_settings.token}"},
                timeout=15,
            )
        if resp.status_code == 200:
            _ok(f"Heartbeat принят центром, статус сервера: {resp.json().get('server_status')}")
        elif resp.status_code == 401:
            _fail("Heartbeat принят центром", "401 — токен неверный, отозван или сервер отключён в /admin/print-servers")
            had_failure = True
        else:
            _fail("Heartbeat принят центром", f"HTTP {resp.status_code}: {resp.text[:200]}")
            had_failure = True
    except Exception as exc:  # noqa: BLE001 - последняя проверка, важно не уронить сам диагностический скрипт
        _fail("Heartbeat принят центром", str(exc))
        had_failure = True

    return 1 if had_failure else 0


if __name__ == "__main__":
    sys.exit(main())
