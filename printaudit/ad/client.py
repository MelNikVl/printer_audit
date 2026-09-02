"""Клиент Active Directory через ldap3. Пароль пользователя используется
ТОЛЬКО для bind на вход (authenticate) и никогда не логируется/не сохраняется.

Тестируемость: реальное соединение с AD создаётся через `Server`/`Connection`
из ldap3 (`_default_user_connection`/`_default_service_connection`). Тесты
подменяют оба фабричных метода на функции, возвращающие ldap3-соединение в
режиме `MOCK_SYNC` (встроенный в ldap3 in-memory LDAP-сервер для тестов,
без необходимости в реальном домене) — см. tests/test_ad_client.py.
"""
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from ldap3 import ALL, SUBTREE, Connection, Server
from ldap3.core.exceptions import LDAPBindError, LDAPException

from printaudit.ad_normalize import normalize_login, split_login
from printaudit.ad_settings import ADSettings

USER_ATTRIBUTES = [
    "sAMAccountName",
    "userPrincipalName",
    "displayName",
    "mail",
    "objectSid",
    "objectGUID",
    "distinguishedName",
    "memberOf",
]

GROUP_ATTRIBUTES = [
    "sAMAccountName",
    "displayName",
    "description",
    "distinguishedName",
]


class ADError(Exception):
    """Ошибка обращения к AD (сеть, поиск, конфигурация)."""


class ADAuthError(ADError):
    """Неверный логин/пароль или недоступный bind."""


def _as_str(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


@dataclass
class ADPrincipal:
    sid: Optional[str]
    object_guid: Optional[str]
    sam_account_name: str
    domain: Optional[str]
    display_name: Optional[str]
    email: Optional[str]
    dn: str
    group_dns: List[str] = field(default_factory=list)

    @property
    def login_normalized(self) -> str:
        raw = f"{self.domain}\\{self.sam_account_name}" if self.domain else self.sam_account_name
        return normalize_login(raw)


@dataclass
class ADGroupInfo:
    dn: str
    sam_account_name: Optional[str]
    display_name: Optional[str]
    description: Optional[str]


def _entry_to_principal(entry, domain: Optional[str]) -> ADPrincipal:
    attrs = entry.entry_attributes_as_dict
    sam = _as_str((attrs.get("sAMAccountName") or [None])[0])
    return ADPrincipal(
        sid=_as_str((attrs.get("objectSid") or [None])[0]),
        object_guid=_as_str((attrs.get("objectGUID") or [None])[0]),
        sam_account_name=sam or "",
        domain=domain,
        display_name=_as_str((attrs.get("displayName") or [None])[0]),
        email=_as_str((attrs.get("mail") or [None])[0]),
        dn=str(entry.entry_dn),
        group_dns=[_as_str(g) for g in (attrs.get("memberOf") or [])],
    )


def _entry_to_group(entry) -> ADGroupInfo:
    attrs = entry.entry_attributes_as_dict
    return ADGroupInfo(
        dn=str(entry.entry_dn),
        sam_account_name=_as_str((attrs.get("sAMAccountName") or [None])[0]),
        display_name=_as_str((attrs.get("displayName") or [None])[0]),
        description=_as_str((attrs.get("description") or [None])[0]),
    )


class ADClient:
    def __init__(
        self,
        settings: ADSettings,
        user_connection_factory: Optional[Callable[[str, str], Connection]] = None,
        service_connection_factory: Optional[Callable[[], Connection]] = None,
    ):
        self.settings = settings
        self._user_connection_factory = user_connection_factory or self._default_user_connection
        self._service_connection_factory = service_connection_factory or self._default_service_connection

    # -- реальные фабрики соединений (production) ---------------------------

    def _server(self) -> Server:
        return Server(self.settings.server, port=self.settings.port, use_ssl=self.settings.use_ssl, get_info=ALL)

    def _default_user_connection(self, upn: str, password: str) -> Connection:
        # Сознательно НЕ auto_bind=True: поведение при неверном пароле отличается
        # между реальным AD (бросает LDAPBindError) и тестовым MOCK_SYNC-сервером
        # (тихо оставляет `.bound = False`) — поэтому bind делаем сами и всегда
        # проверяем `.bound` явно, одинаково для обоих случаев.
        conn = Connection(self._server(), user=upn, password=password)
        conn.bind()
        return conn

    def _default_service_connection(self) -> Connection:
        if not self.settings.bind_user:
            raise ADError(
                "AD_BIND_USER/AD_BIND_PASSWORD не заданы — поиск пользователей/групп "
                "недоступен (вход по логину/паролю пользователя работать может, "
                "поиск и импорт — нет)."
            )
        conn = Connection(self._server(), user=self.settings.bind_user, password=self.settings.bind_password)
        if not conn.bind():
            raise ADError("Не удалось выполнить bind сервисным аккаунтом AD_BIND_USER")
        return conn

    # -- операции -------------------------------------------------------------

    def authenticate(self, login: str, password: str) -> ADPrincipal:
        """Проверяет логин/пароль прямым bind-ом к AD и возвращает карточку
        пользователя. Пароль передаётся только в этот вызов и не сохраняется.

        Логин принимается в любом из трёх форматов (DOMAIN\\login,
        login@domain, login) — какой бы NetBIOS-домен пользователь ни указал
        в DOMAIN\\login, для построения UPN используется ТОЛЬКО настроенный
        AD_DOMAIN (settings.domain), а не то, что ввёл пользователь: в пилоте
        предполагается один домен, и NetBIOS-префикс — это форма ввода, а не
        отдельный домен для bind-а."""
        _typed_domain, sam = split_login(login)
        sam = sam.lower()
        domain = self.settings.domain
        upn = f"{sam}@{domain}" if domain else sam

        try:
            conn = self._user_connection_factory(upn, password)
        except LDAPBindError as exc:
            raise ADAuthError("Неверный логин или пароль") from exc
        except LDAPException as exc:
            raise ADError(f"Не удалось подключиться к AD: {exc}") from exc

        if not conn.bound:
            raise ADAuthError("Неверный логин или пароль")

        try:
            search_base = self.settings.user_search_base or self.settings.base_dn
            ok = conn.search(
                search_base,
                f"(sAMAccountName={sam})",
                search_scope=SUBTREE,
                attributes=USER_ATTRIBUTES,
            )
            if not ok or not conn.entries:
                raise ADAuthError("Пользователь прошёл bind, но не найден в AD_USER_SEARCH_BASE")
            return _entry_to_principal(conn.entries[0], domain)
        finally:
            conn.unbind()

    def search_users(self, query: str, limit: int = 25) -> List[ADPrincipal]:
        """Ищет пользователей по логину, отображаемому имени или email
        (подстрока, регистр не важен — используется LDAP-фильтр с *)."""
        q = (query or "").strip()
        if not q:
            return []
        ldap_filter = (
            f"(&(objectClass=user)(objectCategory=person)(|"
            f"(sAMAccountName=*{q}*)(displayName=*{q}*)(mail=*{q}*)(userPrincipalName=*{q}*)))"
        )
        conn = self._service_connection_factory()
        try:
            conn.search(
                self.settings.user_search_base or self.settings.base_dn,
                ldap_filter,
                search_scope=SUBTREE,
                attributes=USER_ATTRIBUTES,
                size_limit=limit,
            )
            return [_entry_to_principal(e, self.settings.domain) for e in conn.entries]
        finally:
            conn.unbind()

    def get_user_by_login(self, login: str) -> Optional[ADPrincipal]:
        _domain, sam = split_login(login)
        conn = self._service_connection_factory()
        try:
            conn.search(
                self.settings.user_search_base or self.settings.base_dn,
                f"(sAMAccountName={sam})",
                search_scope=SUBTREE,
                attributes=USER_ATTRIBUTES,
            )
            if not conn.entries:
                return None
            return _entry_to_principal(conn.entries[0], self.settings.domain)
        finally:
            conn.unbind()

    def search_groups(self, query: str, limit: int = 25) -> List[ADGroupInfo]:
        q = (query or "").strip()
        if not q:
            return []
        ldap_filter = f"(&(objectClass=group)(|(sAMAccountName=*{q}*)(displayName=*{q}*)))"
        conn = self._service_connection_factory()
        try:
            conn.search(
                self.settings.group_search_base or self.settings.base_dn,
                ldap_filter,
                search_scope=SUBTREE,
                attributes=GROUP_ATTRIBUTES,
                size_limit=limit,
            )
            return [_entry_to_group(e) for e in conn.entries]
        finally:
            conn.unbind()

    def get_group_members(self, group_dn: str) -> List[ADPrincipal]:
        conn = self._service_connection_factory()
        try:
            conn.search(
                self.settings.user_search_base or self.settings.base_dn,
                f"(&(objectClass=user)(memberOf={group_dn}))",
                search_scope=SUBTREE,
                attributes=USER_ATTRIBUTES,
            )
            return [_entry_to_principal(e, self.settings.domain) for e in conn.entries]
        finally:
            conn.unbind()
