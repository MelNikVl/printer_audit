"""Исключения для управления ответом на не авторизованный/не разрешённый
доступ единообразно для HTML-страниц и /api/*, /export/csv (см.
webapp/main.py -- обработчики регистрируются там)."""


class NotAuthenticated(Exception):
    def __init__(self, next_path: str = "/"):
        self.next_path = next_path


class Forbidden(Exception):
    def __init__(self, message: str = "Доступ запрещён"):
        self.message = message
