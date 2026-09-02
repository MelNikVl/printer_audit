"""CSRF-защита по схеме double-submit cookie.

Работает и для уже авторизованных запросов, и для формы логина (когда
серверной сессии ещё нет): сервер выставляет cookie `csrf_token` (обычную,
не HttpOnly — её значение должно быть доступно странице, чтобы вставить в
скрытое поле формы) при любом GET, а на изменяющий запрос (POST/PUT/PATCH/
DELETE) требуется, чтобы значение из тела формы (или заголовка X-CSRF-Token
для API) совпало со значением этой cookie. Значение cookie никак не
привязано к личности пользователя — это специально: сам факт совпадения
"что было в cookie" и "что вернула форма" доказывает, что запрос пришёл со
страницы этого сайта, а не с чужого домена по CSRF."""
import secrets

CSRF_COOKIE_NAME = "pa_csrf"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_tokens_match(cookie_value: str, submitted_value: str) -> bool:
    if not cookie_value or not submitted_value:
        return False
    return secrets.compare_digest(cookie_value, submitted_value)
