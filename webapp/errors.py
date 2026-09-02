"""Исключения для управления ответом на не авторизованный/не разрешённый
доступ единообразно для HTML-страниц и /api/*, /export/csv (см.
webapp/main.py -- обработчики регистрируются там)."""
import logging
import secrets

logger = logging.getLogger("webapp.errors")


class NotAuthenticated(Exception):
    def __init__(self, next_path: str = "/"):
        self.next_path = next_path


class Forbidden(Exception):
    def __init__(self, message: str = "Доступ запрещён"):
        self.message = message


def safe_error_message(exc: Exception, context: str) -> str:
    """Логирует ПОЛНОЕ исключение (со стектрейсом) с коротким correlation ID
    и возвращает НЕЙТРАЛЬНОЕ сообщение для показа в UI -- без текста самого
    исключения. Использовать для любых ошибок, где exc может содержать
    внутренние детали (адрес/порт LDAP-сервера, код ошибки AD, путь к
    PowerShell-скрипту, вывод stderr и т.п.) -- то, что не должно попадать в
    браузер даже авторизованному admin/superadmin (отражённая диагностика в
    UI облегчает разведку для того, кто получил доступ к сессии/скриншоту).

    Полный текст ошибки остаётся в логе сервера, найти его по ID:
    `grep <error_id> logs/*.log` или в выводе uvicorn/NSSM."""
    error_id = secrets.token_hex(4)
    logger.error("[%s] %s", error_id, context, exc_info=exc)
    return f"Операция не выполнена ({context}). Обратитесь к администратору. ID ошибки: {error_id}"
