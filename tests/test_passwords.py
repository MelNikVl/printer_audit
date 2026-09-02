"""printaudit.security.passwords: Argon2id-хэширование, минимальная длина,
константное по времени сравнение (гарантируется самой Argon2, но проверяем
что verify действительно идёт через неё, а не через == сравнение строк)."""
import pytest

from printaudit.security.passwords import (
    MIN_PASSWORD_LENGTH,
    WeakPasswordError,
    hash_password,
    validate_password_strength,
    verify_password,
)


def test_hash_password_does_not_return_plaintext():
    password = "CorrectHorseBattery1"
    hashed = hash_password(password)
    assert hashed != password
    assert password not in hashed


def test_hash_password_uses_argon2id_format():
    hashed = hash_password("CorrectHorseBattery1")
    assert hashed.startswith("$argon2id$")


def test_verify_password_correct():
    password = "CorrectHorseBattery1"
    hashed = hash_password(password)
    assert verify_password(password, hashed) is True


def test_verify_password_incorrect():
    hashed = hash_password("CorrectHorseBattery1")
    assert verify_password("WrongPassword123", hashed) is False


def test_verify_password_empty_hash_returns_false_not_exception():
    assert verify_password("anything12345", "") is False
    assert verify_password("anything12345", None) is False


def test_verify_password_malformed_hash_returns_false_not_exception():
    assert verify_password("anything12345", "not-a-real-hash") is False


def test_two_hashes_of_same_password_differ():
    """Argon2 включает случайную соль -- одинаковый пароль не должен давать
    одинаковый хэш дважды (иначе можно было бы искать совпадения по хэшу)."""
    a = hash_password("CorrectHorseBattery1")
    b = hash_password("CorrectHorseBattery1")
    assert a != b
    assert verify_password("CorrectHorseBattery1", a)
    assert verify_password("CorrectHorseBattery1", b)


def test_validate_password_strength_rejects_short():
    with pytest.raises(WeakPasswordError):
        validate_password_strength("short1")


def test_validate_password_strength_accepts_min_length():
    validate_password_strength("a" * MIN_PASSWORD_LENGTH)  # не должно бросить


def test_validate_password_strength_rejects_one_below_min():
    with pytest.raises(WeakPasswordError):
        validate_password_strength("a" * (MIN_PASSWORD_LENGTH - 1))


def test_validate_password_strength_rejects_absurdly_long():
    with pytest.raises(WeakPasswordError):
        validate_password_strength("a" * 10_000)
