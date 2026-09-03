"""CRUD для SnmpProfile — переиспользуемый набор OID/учётных данных SNMP
для одного семейства/модели принтеров (см. printaudit.models.SnmpProfile,
printaudit.monitoring.snmp_adapter за тем, как профиль используется при
опросе). Секреты (community/auth key/priv key) сюда НЕ передаются — только
ИМЕНА переменных окружения, где они реально лежат; сам секрет никогда не
попадает в эти функции, в БД или в аудит-лог. Каждое изменение аудируется —
тот же принцип, что и у printaudit.monitoring.devices."""
from typing import Optional

from sqlalchemy.orm import Session

from printaudit import audit
from printaudit.models import AppUser, SnmpProfile
from printaudit.monitoring.snmp_adapter import V3_AUTH_PROTOCOLS, V3_PRIV_PROTOCOLS


class SnmpProfileError(ValueError):
    pass


def _normalize_and_validate(
    *, name: str, snmp_version: str, credentials_env_var: Optional[str], snmp_v3_username: Optional[str],
    snmp_v3_auth_protocol: Optional[str], snmp_v3_auth_key_env_var: Optional[str],
    snmp_v3_priv_protocol: Optional[str], snmp_v3_priv_key_env_var: Optional[str],
) -> dict:
    """Возвращает нормализованные поля (версия в нижнем регистре,
    протоколы в верхнем, пустые строки -> None) или бросает
    SnmpProfileError с понятным сообщением — те же правила, что и
    resolve_snmp_security на момент реального опроса, но проверенные СРАЗУ
    при сохранении профиля, а не только при первом опросе устройства."""
    if not name or not name.strip():
        raise SnmpProfileError("Имя профиля обязательно")

    version = (snmp_version or "").strip().lower()
    if version not in ("v2c", "v3"):
        raise SnmpProfileError("snmp_version должен быть 'v3' или явным legacy 'v2c'")

    credentials_env_var = (credentials_env_var or "").strip() or None
    snmp_v3_username = (snmp_v3_username or "").strip() or None
    auth_protocol = (snmp_v3_auth_protocol or "").strip().upper() or None
    auth_key_env_var = (snmp_v3_auth_key_env_var or "").strip() or None
    priv_protocol = (snmp_v3_priv_protocol or "").strip().upper() or None
    priv_key_env_var = (snmp_v3_priv_key_env_var or "").strip() or None

    if version == "v2c":
        if not credentials_env_var:
            raise SnmpProfileError(
                "Для snmp_version=v2c обязательно указать имя переменной окружения с community."
            )
    else:
        if not snmp_v3_username:
            raise SnmpProfileError("Для snmp_version=v3 обязательно указать имя пользователя (snmp_v3_username).")
        if auth_protocol and auth_protocol not in V3_AUTH_PROTOCOLS:
            raise SnmpProfileError(f"Неизвестный auth_protocol (допустимо: {', '.join(V3_AUTH_PROTOCOLS)}).")
        if auth_protocol and not auth_key_env_var:
            raise SnmpProfileError(
                "При заданном auth_protocol обязательно имя переменной окружения с ключом аутентификации."
            )
        if priv_protocol and priv_protocol not in V3_PRIV_PROTOCOLS:
            raise SnmpProfileError(f"Неизвестный priv_protocol (допустимо: {', '.join(V3_PRIV_PROTOCOLS)}).")
        if priv_protocol and not auth_protocol:
            raise SnmpProfileError(
                "priv_protocol требует заданного auth_protocol — SNMPv3 (USM) не допускает "
                "privacy без authentication."
            )
        if priv_protocol and not priv_key_env_var:
            raise SnmpProfileError(
                "При заданном priv_protocol обязательно имя переменной окружения с ключом приватности."
            )

    return {
        "name": name.strip(), "snmp_version": version, "credentials_env_var": credentials_env_var,
        "snmp_v3_username": snmp_v3_username, "snmp_v3_auth_protocol": auth_protocol,
        "snmp_v3_auth_key_env_var": auth_key_env_var, "snmp_v3_priv_protocol": priv_protocol,
        "snmp_v3_priv_key_env_var": priv_key_env_var,
    }


def create_snmp_profile(
    session: Session, *, actor: AppUser, name: str, description: str = "", snmp_version: str = "v3",
    port: int = 161, timeout_seconds: float = 2.0, retries: int = 1, oid_map_json: str = "{}",
    credentials_env_var: Optional[str] = None, snmp_v3_username: Optional[str] = None,
    snmp_v3_auth_protocol: Optional[str] = None, snmp_v3_auth_key_env_var: Optional[str] = None,
    snmp_v3_priv_protocol: Optional[str] = None, snmp_v3_priv_key_env_var: Optional[str] = None,
) -> SnmpProfile:
    fields = _normalize_and_validate(
        name=name, snmp_version=snmp_version, credentials_env_var=credentials_env_var,
        snmp_v3_username=snmp_v3_username, snmp_v3_auth_protocol=snmp_v3_auth_protocol,
        snmp_v3_auth_key_env_var=snmp_v3_auth_key_env_var, snmp_v3_priv_protocol=snmp_v3_priv_protocol,
        snmp_v3_priv_key_env_var=snmp_v3_priv_key_env_var,
    )
    if session.query(SnmpProfile).filter_by(name=fields["name"]).first():
        raise SnmpProfileError(f"Профиль SNMP с именем «{fields['name']}» уже существует.")

    profile = SnmpProfile(
        description=(description or "").strip() or None, port=port, timeout_seconds=timeout_seconds,
        retries=retries, oid_map_json=oid_map_json or "{}", **fields,
    )
    session.add(profile)
    session.flush()
    audit.record(
        session, actor_app_user_id=actor.id, action="snmp_profile.create", object_type="snmp_profile",
        object_id=profile.id, new_value={"name": profile.name, "snmp_version": profile.snmp_version},
    )
    return profile


def update_snmp_profile(
    session: Session, *, actor: AppUser, profile: SnmpProfile, name: str, description: str = "",
    snmp_version: str = "v3", port: int = 161, timeout_seconds: float = 2.0, retries: int = 1,
    oid_map_json: str = "{}", credentials_env_var: Optional[str] = None, snmp_v3_username: Optional[str] = None,
    snmp_v3_auth_protocol: Optional[str] = None, snmp_v3_auth_key_env_var: Optional[str] = None,
    snmp_v3_priv_protocol: Optional[str] = None, snmp_v3_priv_key_env_var: Optional[str] = None,
) -> SnmpProfile:
    fields = _normalize_and_validate(
        name=name, snmp_version=snmp_version, credentials_env_var=credentials_env_var,
        snmp_v3_username=snmp_v3_username, snmp_v3_auth_protocol=snmp_v3_auth_protocol,
        snmp_v3_auth_key_env_var=snmp_v3_auth_key_env_var, snmp_v3_priv_protocol=snmp_v3_priv_protocol,
        snmp_v3_priv_key_env_var=snmp_v3_priv_key_env_var,
    )
    existing = session.query(SnmpProfile).filter(SnmpProfile.name == fields["name"], SnmpProfile.id != profile.id).first()
    if existing:
        raise SnmpProfileError(f"Профиль SNMP с именем «{fields['name']}» уже существует.")

    old = {"name": profile.name, "snmp_version": profile.snmp_version}
    profile.description = (description or "").strip() or None
    profile.port = port
    profile.timeout_seconds = timeout_seconds
    profile.retries = retries
    profile.oid_map_json = oid_map_json or "{}"
    for key, value in fields.items():
        setattr(profile, key, value)

    audit.record(
        session, actor_app_user_id=actor.id, action="snmp_profile.update", object_type="snmp_profile",
        object_id=profile.id, old_value=old, new_value={"name": profile.name, "snmp_version": profile.snmp_version},
    )
    return profile


def set_snmp_profile_active(session: Session, *, actor: AppUser, profile: SnmpProfile, is_active: bool) -> None:
    old = {"is_active": profile.is_active}
    profile.is_active = is_active
    audit.record(
        session, actor_app_user_id=actor.id, action="snmp_profile.set_active", object_type="snmp_profile",
        object_id=profile.id, old_value=old, new_value={"is_active": is_active},
    )
