import pytest

from printaudit.ad_normalize import normalize_login, split_login, strip_domain, with_domain


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("DOMAIN\\ivanov", "domain\\ivanov"),
        ("ivanov@domain", "domain\\ivanov"),
        ("ivanov@DOMAIN.LOCAL", "domain.local\\ivanov"),
        ("ivanov", "ivanov"),
        ("IVANOV", "ivanov"),
        ("Domain\\Ivanov", "domain\\ivanov"),
        ("  ivanov  ", "ivanov"),
    ],
)
def test_normalize_login(raw, expected):
    assert normalize_login(raw) == expected


def test_normalize_login_formats_are_all_equal_for_same_user():
    variants = ["DOMAIN\\ivanov", "ivanov@domain", "IVANOV@DOMAIN", "Domain\\IVANOV"]
    normalized = {normalize_login(v) for v in variants}
    assert normalized == {"domain\\ivanov"}


def test_split_login_backslash():
    assert split_login("DOMAIN\\ivanov") == ("DOMAIN", "ivanov")


def test_split_login_at():
    assert split_login("ivanov@domain") == ("domain", "ivanov")


def test_split_login_bare():
    assert split_login("ivanov") == (None, "ivanov")


def test_strip_domain():
    assert strip_domain("domain\\ivanov") == "ivanov"
    assert strip_domain("ivanov") == "ivanov"


def test_with_domain_adds_default_when_missing():
    assert with_domain("ivanov", "example.local") == "example.local\\ivanov"


def test_with_domain_keeps_explicit_domain():
    assert with_domain("OTHER\\ivanov", "example.local") == "other\\ivanov"
