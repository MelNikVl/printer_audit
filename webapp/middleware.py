"""ASGI-middleware, гарантирующая CSRF-cookie на любом ответе.

Генерирует токен ДО вызова роута (чтобы шаблон мог отрендерить его в форму
уже в первом же ответе, без лишнего редиректа) и выставляет cookie на выходе
только если её не было во входящем запросе — иначе она просто передаётся
дальше без изменений. Это специально сделано middleware, а не
per-route-зависимостью: если роут возвращает СВОЙ объект Response (например,
RedirectResponse при логине), любые cookie/заголовки, выставленные на
отдельном "инжектированном" Response той же FastAPI-зависимостью, были бы
потеряны, а middleware работает с тем же самым объектом ответа, который
реально уходит клиенту, независимо от того, как роут его построил."""
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from printaudit.ad_settings import get_session_settings
from printaudit.security.csrf import CSRF_COOKIE_NAME, generate_csrf_token


class CsrfCookieMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        existing_token = request.cookies.get(CSRF_COOKIE_NAME)
        request.state.csrf_token = existing_token or generate_csrf_token()

        response = await call_next(request)

        if not existing_token:
            session_settings = get_session_settings()
            response.set_cookie(
                CSRF_COOKIE_NAME,
                request.state.csrf_token,
                httponly=False,
                samesite="lax",
                secure=session_settings.cookie_secure,
                max_age=60 * 60 * 24,
            )
        return response


def _parse_trusted_proxy_ips() -> frozenset:
    raw = os.environ.get("TRUSTED_PROXY_IPS", "")
    return frozenset(ip.strip() for ip in raw.split(",") if ip.strip())


class TrustedProxyHeadersMiddleware:
    """Честно решает "запрос реально пришёл по HTTPS?", когда TLS
    завершается на реверс-прокси (nginx/IIS/...) перед uvicorn — без этого
    `request.url.scheme` внутри приложения ВСЕГДА будет "http" (соединение
    прокси -> uvicorn обычно plain HTTP), и любая проверка вида
    `if request.url.scheme != "https"` (см. webapp/agent_api.py,
    AGENT_REQUIRE_HTTPS) либо ошибочно блокирует легитимный трафик, либо её
    приходится выключать вовсе, теряя реальную защиту.

    БЕЗОПАСНОСТЬ: заголовку X-Forwarded-Proto доверяем ТОЛЬКО если
    непосредственный TCP-peer (scope["client"][0], т.е. тот, кто реально
    подключился к uvicorn — сам прокси, а не конечный клиент) входит в
    TRUSTED_PROXY_IPS (см. .env.example). Если список пуст (по умолчанию)
    или peer в него не входит — заголовок ПОЛНОСТЬЮ игнорируется и
    scope["scheme"] остаётся как есть: иначе любой внешний клиент мог бы
    сам прислать "X-Forwarded-Proto: https" в обход AGENT_REQUIRE_HTTPS.
    TRUSTED_PROXY_IPS читается заново на каждый запрос (не кэшируется в
    __init__), как и остальные настройки на основе os.environ в проекте
    (см. printaudit.ad_settings/agent_settings) — так же тестируется через
    monkeypatch.setenv, без пересборки приложения.

    Альтернатива/дополнение — встроенный в uvicorn `--proxy-headers
    --forwarded-allow-ips=<IP прокси>` (см. deploy/run_webapp.ps1): работает
    на уровень ниже (до ASGI-приложения), но её нельзя проверить через
    FastAPI TestClient (он вызывает ASGI-приложение напрямую, минуя сам
    uvicorn) — этот middleware решает ту же задачу и тестируем."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        trusted_ips = _parse_trusted_proxy_ips()
        if trusted_ips:
            client = scope.get("client")
            peer_ip = client[0] if client else None
            if peer_ip in trusted_ips:
                headers = scope.get("headers") or []
                proto_value = None
                for key, value in headers:
                    if key == b"x-forwarded-proto":
                        proto_value = value
                        break
                if proto_value is not None:
                    candidate = proto_value.decode("latin-1").split(",")[0].strip().lower()
                    if candidate in ("http", "https"):
                        scope = dict(scope)
                        scope["scheme"] = candidate

        await self.app(scope, receive, send)
