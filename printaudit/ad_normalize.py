"""Нормализация логина AD в единый ключ для сопоставления пользователей.

Поддерживаются три формата, которые встречаются и в самой AD, и в событии
печати 307 (User Name), и при ручном вводе логина в UI:
  - DOMAIN\\login
  - login@domain
  - login (без домена — например, если печатали локальным пользователем
    или домен не попал в событие)

Нормализованная форма — нижний регистр, "domain\\login" если домен известен,
иначе просто "login". Регистр в AD/Windows не имеет значения для сопоставления
логинов, поэтому регистронезависимость обязательна.
"""
from typing import Optional, Tuple


def split_login(raw: str) -> Tuple[Optional[str], str]:
    """Возвращает (домен_или_None, sam_account_name) как есть, без изменения регистра."""
    raw = (raw or "").strip()
    if "\\" in raw:
        domain, sam = raw.split("\\", 1)
        return domain.strip() or None, sam.strip()
    if "@" in raw:
        sam, domain = raw.split("@", 1)
        return domain.strip() or None, sam.strip()
    return None, raw


def normalize_login(raw: str) -> str:
    """Единый ключ для сравнения/поиска логина: нижний регистр,
    "domain\\login" при известном домене, иначе просто "login"."""
    domain, sam = split_login(raw)
    sam = sam.lower()
    if domain:
        return f"{domain.lower()}\\{sam}"
    return sam


def strip_domain(normalized_login: str) -> str:
    """Возвращает только sam-часть нормализованного логина (без домена)."""
    if "\\" in normalized_login:
        return normalized_login.split("\\", 1)[1]
    return normalized_login


def with_domain(raw_or_bare_login: str, default_domain: str) -> str:
    """Нормализует логин; если в нём не было домена, подставляет default_domain.
    Используется при сопоставлении заданий печати, где User Name из события
    307 иногда приходит без домена, а в ad_users логины хранятся с доменом."""
    domain, sam = split_login(raw_or_bare_login)
    domain = domain or default_domain
    sam = sam.lower()
    if domain:
        return f"{domain.lower()}\\{sam}"
    return sam
