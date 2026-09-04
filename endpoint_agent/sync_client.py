"""HTTP-клиент endpoint-агента — стандартная библиотека (urllib), без httpx/
requests, чтобы не тянуть лишние зависимости на пользовательский ПК (см.
endpoint_agent/__init__.py). Контракт запросов/ответов зеркалит
webapp/endpoint_api.py (EndpointEventsBatchIn/Out, EndpointHeartbeatIn/Out)."""
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

Transport = Callable[[str, dict, dict, float], "HttpResult"]


class SyncError(RuntimeError):
    """Сбой отправки (сеть/timeout/HTTP-ошибка целиком пакета) — не должен
    ронять процесс агента, только откладывать попытку (см. endpoint_agent.
    runner, тот же принцип, что и AgentSyncError в collector/agent_sync.py)."""


@dataclass
class HttpResult:
    status_code: int
    body: dict


def _default_transport(url: str, headers: dict, payload: dict, timeout: float) -> HttpResult:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={**headers, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
            return HttpResult(status_code=resp.status, body=body)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            body = {}
        return HttpResult(status_code=exc.code, body=body)
    except urllib.error.URLError as exc:
        # Таймаут/DNS/сеть недоступна — нет ответа сервера вообще, не то же
        # самое, что явный HTTP-статус ошибки (см. вызов ниже: это retryable).
        raise SyncError(f"Сеть недоступна: {exc.reason}") from exc


def post_json(
    base_url: str, path: str, token: str, payload: dict, timeout: float, transport: Optional[Transport] = None
) -> HttpResult:
    """НИКОГДА не включает токен в текст исключения — тот же принцип, что и
    в webapp/agent_api.py и collector/agent_sync.py (см. их комментарии)."""
    url = f"{base_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {token}"}
    send = transport or _default_transport
    return send(url, headers, payload, timeout)


def send_events_batch(
    base_url: str, token: str, payload: dict, timeout: float, transport: Optional[Transport] = None
) -> HttpResult:
    return post_json(base_url, "/api/v1/endpoint/events/batch", token, payload, timeout, transport)


def send_heartbeat(
    base_url: str, token: str, payload: dict, timeout: float, transport: Optional[Transport] = None
) -> HttpResult:
    return post_json(base_url, "/api/v1/endpoint/heartbeat", token, payload, timeout, transport)
