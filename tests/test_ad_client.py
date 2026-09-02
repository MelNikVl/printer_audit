"""Тесты ADClient на встроенном in-memory LDAP-сервере ldap3 (MOCK_SYNC) —
без реального домена. Мок хранит пользователей по DN; чтобы имитировать то,
что реальный AD принимает bind по UPN/sAMAccountName, тестовая фабрика
соединений сама транслирует UPN -> DN перед бинд-ом (это только тестовая
прослойка, в проде ADClient обращается к настоящему AD напрямую по UPN)."""
import pytest
from ldap3 import MOCK_SYNC, Connection, Server

from printaudit.ad.client import ADAuthError, ADClient, ADError
from printaudit.ad_settings import ADSettings

USERS_OU = "ou=users,dc=example,dc=local"
GROUPS_OU = "ou=groups,dc=example,dc=local"
IVANOV_DN = f"cn=ivan ivanov,{USERS_OU}"
PETROVA_DN = f"cn=petrova anna,{USERS_OU}"
ACCOUNTING_DN = f"cn=Accounting,{GROUPS_OU}"
SALES_DN = f"cn=Sales,{GROUPS_OU}"


@pytest.fixture
def mock_ad():
    server = Server("mock-dc")
    setup_conn = Connection(server, user="cn=admin,dc=example,dc=local", password="x", client_strategy=MOCK_SYNC)
    setup_conn.open()
    setup_conn.bind()

    setup_conn.strategy.add_entry(
        IVANOV_DN,
        {
            "objectClass": ["user", "person"],
            # ADClient.search_users фильтрует по (objectCategory=person) — так же,
            # как это принято делать против настоящей AD. Мок делает буквальное
            # сравнение значения атрибута, поэтому здесь достаточно строки "person".
            "objectCategory": "person",
            "sAMAccountName": "ivanov",
            "userPrincipalName": "ivanov@example.local",
            "displayName": "Ivan Ivanov",
            "mail": "ivanov@example.local",
            "objectSid": "S-1-5-21-111-222-333-1001",
            "objectGUID": "11111111-2222-3333-4444-555555555555",
            "userPassword": "CorrectPass1",
            "memberOf": [ACCOUNTING_DN],
        },
    )
    setup_conn.strategy.add_entry(
        PETROVA_DN,
        {
            "objectClass": ["user", "person"],
            "objectCategory": "person",
            "sAMAccountName": "petrova",
            "userPrincipalName": "petrova@example.local",
            "displayName": "Petrova Anna",
            "mail": "petrova@example.local",
            "objectSid": "S-1-5-21-111-222-333-1002",
            "objectGUID": "22222222-3333-4444-5555-666666666666",
            "userPassword": "AnnaPass1",
            "memberOf": [SALES_DN, ACCOUNTING_DN],
        },
    )
    setup_conn.strategy.add_entry(
        ACCOUNTING_DN,
        {"objectClass": ["group"], "sAMAccountName": "Accounting", "displayName": "Бухгалтерия"},
    )
    setup_conn.strategy.add_entry(
        SALES_DN,
        {"objectClass": ["group"], "sAMAccountName": "Sales", "displayName": "Отдел продаж"},
    )

    upn_to_dn = {
        "ivanov@example.local": IVANOV_DN,
        "petrova@example.local": PETROVA_DN,
    }

    def user_connection_factory(upn: str, password: str) -> Connection:
        dn = upn_to_dn.get(upn, upn)  # неизвестный UPN -> bind провалится (DN не существует)
        conn = Connection(server, user=dn, password=password, client_strategy=MOCK_SYNC)
        conn.bind()
        return conn

    def service_connection_factory() -> Connection:
        conn = Connection(server, user="cn=admin,dc=example,dc=local", password="x", client_strategy=MOCK_SYNC)
        conn.bind()
        return conn

    settings = ADSettings(
        server="mock-dc",
        port=636,
        use_ssl=True,
        domain="example.local",
        base_dn="dc=example,dc=local",
        user_search_base=USERS_OU,
        group_search_base=GROUPS_OU,
        bind_user="cn=admin,dc=example,dc=local",
        bind_password="x",
    )
    return ADClient(
        settings,
        user_connection_factory=user_connection_factory,
        service_connection_factory=service_connection_factory,
    )


def test_authenticate_success_returns_principal(mock_ad):
    principal = mock_ad.authenticate("ivanov", "CorrectPass1")
    assert principal.sam_account_name == "ivanov"
    assert principal.display_name == "Ivan Ivanov"
    assert principal.sid == "S-1-5-21-111-222-333-1001"
    assert ACCOUNTING_DN in principal.group_dns


@pytest.mark.parametrize(
    "login",
    ["ivanov", "EXAMPLE\\ivanov", "ivanov@example.local", "IVANOV"],
)
def test_authenticate_accepts_all_login_formats(mock_ad, login):
    principal = mock_ad.authenticate(login, "CorrectPass1")
    assert principal.sam_account_name == "ivanov"


def test_authenticate_wrong_password_raises_ad_auth_error(mock_ad):
    with pytest.raises(ADAuthError):
        mock_ad.authenticate("ivanov", "WrongPassword")


def test_authenticate_unknown_user_raises_ad_auth_error(mock_ad):
    with pytest.raises(ADAuthError):
        mock_ad.authenticate("nobody", "whatever")


def test_search_users_by_substring(mock_ad):
    results = mock_ad.search_users("ivan")
    assert any(r.sam_account_name == "ivanov" for r in results)


def test_search_users_empty_query_returns_empty(mock_ad):
    assert mock_ad.search_users("") == []


def test_search_groups_by_substring(mock_ad):
    groups = mock_ad.search_groups("Account")
    assert any(g.sam_account_name == "Accounting" for g in groups)


def test_get_group_members_returns_correct_users(mock_ad):
    members = mock_ad.get_group_members(SALES_DN)
    sams = {m.sam_account_name for m in members}
    assert sams == {"petrova"}


def test_get_group_members_multiple_users(mock_ad):
    members = mock_ad.get_group_members(ACCOUNTING_DN)
    sams = {m.sam_account_name for m in members}
    assert sams == {"ivanov", "petrova"}


def test_service_connection_required_for_search_without_bind_user():
    settings = ADSettings(
        server="mock-dc", port=636, use_ssl=True, domain="example.local",
        base_dn="dc=example,dc=local", user_search_base="", group_search_base="",
        bind_user=None, bind_password=None,
    )
    client = ADClient(settings)
    with pytest.raises(ADError):
        client.search_users("ivanov")
