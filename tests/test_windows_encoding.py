"""Регрессия: `python scripts\\init_db.py` падал на реальном Windows Server
(Python 3.12, `-X utf8` не передавался) с

    UnicodeDecodeError: cp1252 can't decode byte 0x81

Причина: Alembic на Python >= 3.10 читает alembic.ini через
`configparser.ConfigParser.read(..., encoding="locale")` -- это захардкожено
в `alembic.util.compat.read_config_parser` и НЕ настраивается через публичный
API `Config` (проверено чтением исходников установленной версии). "locale"
означает `locale.getpreferredencoding(False)` -- то есть активную ANSI-
кодовую страницу процесса (cp1252 на англ./зап.-европейской Windows, cp1251
на русской и т.д.), а НЕ UTF-8. `alembic.ini` в этом репозитории раньше
содержал кириллический комментарий в UTF-8 -- валидный для git/редакторов,
но не декодируемый как cp1252 (байт 0x81 там не определён).

Единственный надёжный фикс — держать alembic.ini чисто в ASCII: тогда его
байты идентично и однозначно декодируются ЛЮБОЙ однобайтовой кодовой
страницей и UTF-8 одновременно, независимо от локали машины, на которой
запущен `python scripts\\init_db.py` или просто `alembic upgrade head`
(последний тоже читает тот же файл тем же захардкоженным способом, минуя
любые обёртки на нашей стороне).

Эти тесты не требуют реальной Windows-машины: ASCII-only -- это ровно то
свойство файла, которое гарантирует отсутствие бага на любой Windows-локали,
поэтому проверяется явным decode('cp1252', strict) -- byte-for-byte то же
исключение, что видел пользователь -- а не косвенно через локаль текущей
машины (она может маскировать баг, как выяснилось на этой же машине: её
собственная локаль cp1251 "проглатывает" те же байты без ошибки, хотя cp1252
на реальном сервере -- нет)."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def test_alembic_ini_is_pure_ascii():
    """Основная защита: ASCII decodable => decodable identically under any
    single-byte Windows code page (cp1252, cp1251, cp1250, ...) AND UTF-8.
    Если это когда-нибудь снова перестанет быть true (кто-то добавит
    кириллицу/любые не-ASCII символы в комментарий), тест упадёт ДО того,
    как это дойдёт до реального Windows-сервера."""
    data = ALEMBIC_INI.read_bytes()
    try:
        data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise AssertionError(
            "alembic.ini содержит не-ASCII байты — Alembic читает этот файл "
            "через configparser с encoding=\"locale\" (Python >= 3.10, "
            "захардкожено в alembic.util.compat.read_config_parser, не "
            "настраивается через публичный Config API), поэтому не-ASCII "
            "содержимое здесь падает с UnicodeDecodeError на любой Windows-"
            "машине, чья активная кодовая страница не совпадает с тем, чем "
            "был сохранён файл (см. docstring модуля)."
        ) from exc


def test_alembic_ini_decodes_as_cp1252():
    """Детерминированное воспроизведение ИМЕННО того исключения, которое
    видел пользователь на реальном сервере (cp1252), а не просто общей
    ASCII-проверки -- если бы фикс был неполным (например, остался один
    не-ASCII байт, который случайно валиден в cp1252, но не в ascii),
    тест выше поймал бы это как AssertionError, а этот -- воспроизвёл бы
    сам оригинальный UnicodeDecodeError."""
    data = ALEMBIC_INI.read_bytes()
    data.decode("cp1252")  # бросит UnicodeDecodeError, если регрессия вернулась


def test_alembic_config_reads_real_ini_file_end_to_end():
    """Сквозная проверка (не только байты, но и что Alembic реально может
    прочитать секцию [alembic] из настоящего файла в этом репозитории)."""
    from alembic.config import Config

    cfg = Config(str(ALEMBIC_INI))
    assert cfg.get_main_option("script_location") == "alembic"


def test_init_db_module_imports_and_builds_config_without_x_utf8():
    """Регрессия конкретно для scripts/init_db.py: воспроизводит ровно то,
    что падало на сервере -- построение Config для alembic.ini этого
    репозитория -- без флага `-X utf8`/переменной PYTHONUTF8 (эта переменная
    здесь не проверяется и не требуется: если тест воспроизводит окружение,
    где к моменту его запуска PYTHONUTF8 уже была бы взведена pytest'ом или
    внешним раннером, alembic.ini достаточно устойчив (чистый ASCII), чтобы
    результат не зависел от этого флага вообще -- что и есть цель фикса)."""
    import sys

    sys.path.insert(0, str(REPO_ROOT))
    import scripts.init_db as init_db

    cfg = init_db._alembic_config()  # это и есть точка, где падало на реальном сервере
    assert cfg.get_main_option("script_location").endswith("alembic")
