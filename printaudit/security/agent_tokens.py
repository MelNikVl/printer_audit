"""Токены агентов Print Server для центрального API (webapp/agent_api.py).

Это НЕ пароли пользователей — это машинные bearer-токены с высокой
энтропией (secrets.token_urlsafe), сравниваемые на каждый API-запрос от
агента, потенциально часто. Поэтому вместо намеренно медленного Argon2id
(printaudit.security.passwords, для человеческих паролей) здесь обычный
SHA-256 + сравнение постоянным временем: у токена и так достаточно энтропии
против brute-force по хэшу, а медленный хэш только замедлил бы каждый
запрос агента без пользы для безопасности.

Сырой токен НИКОГДА не сохраняется — только его хэш (PrintServer.token_hash).
Показывается администратору РОВНО ОДИН раз, в момент создания регистрации
или ротации (см. webapp/print_servers_routes.py), и не выводится ни в один
лог/audit_log (printaudit.audit._scrub маскирует любой ключ, содержащий
"token", как last-resort защиту)."""
import hashlib
import hmac
import secrets

TOKEN_BYTES = 32


def generate_agent_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_agent_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def verify_agent_token(raw_token: str, token_hash: str) -> bool:
    if not raw_token or not token_hash:
        return False
    return hmac.compare_digest(hash_agent_token(raw_token), token_hash)
