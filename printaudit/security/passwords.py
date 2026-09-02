"""Хэширование и проверка локальных паролей — Argon2id (argon2-cffi).

Открытый пароль НИКОГДА не сохраняется и не логируется: он существует только
внутри вызова hash_password()/verify_password() и сразу после возврата из
этих функций выходит из области видимости у вызывающего кода. Ни один модуль
проекта не должен писать пароль в logging, audit_log или куда-либо ещё —
это соглашение, которое проверяется тестами (grep по сообщениям/содержимому
БД после операций с паролем), а не enforced на уровне типов.

Верификация — через встроенный argon2.PasswordHasher.verify(), который
сравнивает хэши по константному времени на уровне C-реализации Argon2 (это
свойство самого алгоритма/референсной реализации, а не что-то, что нужно
добавлять вручную поверх)."""
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256  # защита от неоправданно дорогого хэширования гигантского ввода

_hasher = PasswordHasher()


class WeakPasswordError(ValueError):
    """Пароль не проходит минимальные требования (см. validate_password_strength)."""


def validate_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise WeakPasswordError(f"Пароль слишком длинный (максимум {MAX_PASSWORD_LENGTH} символов).")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """True, если пароль совпадает с хэшем. Никогда не бросает исключение
    наружу — неверный пароль, битый/чужого формата хэш и т.п. всё
    единообразно дают False, чтобы вызывающий код не мог случайно различить
    "неверный пароль" от "испорченный хэш в БД" по типу исключения."""
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# Предвычисленный хэш заведомо непригодного пароля — используется, чтобы
# authenticate_local() тратил сопоставимое время на "пользователь не найден"
# и на "пользователь найден, пароль неверный", не давая по времени ответа
# отличить существование логина (user enumeration через тайминг).
_DUMMY_HASH = hash_password("dummy-password-never-used-for-any-real-account")


def verify_against_dummy_hash(password: str) -> None:
    verify_password(password, _DUMMY_HASH)
