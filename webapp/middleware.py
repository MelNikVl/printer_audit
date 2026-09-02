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
