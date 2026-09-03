"""endpoint_agent.config — свой минимальный KEY=VALUE парсер (без
python-dotenv), обязательные поля, значения по умолчанию, field_map
override."""
import pytest

from endpoint_agent.config import DEFAULT_FIELD_MAP, ConfigError, load_config


def _write_env(tmp_path, extra_lines=""):
    path = tmp_path / "endpoint_agent.env"
    path.write_text(
        "SERVER_BASE_URL=https://site.example.local:8443\n"
        "ENDPOINT_UUID=11111111-1111-1111-1111-111111111111\n"
        "ENDPOINT_TOKEN=secret-token\n" + extra_lines,
        encoding="utf-8",
    )
    return path


def test_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "does_not_exist.env")


def test_missing_required_fields_raises_config_error(tmp_path):
    path = tmp_path / "endpoint_agent.env"
    path.write_text("SERVER_BASE_URL=https://site.example.local\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="ENDPOINT_TOKEN"):
        load_config(path)


def test_loads_defaults_when_optional_fields_omitted(tmp_path):
    cfg = load_config(_write_env(tmp_path))
    assert cfg.server_base_url == "https://site.example.local:8443"
    assert cfg.endpoint_uuid == "11111111-1111-1111-1111-111111111111"
    assert cfg.token == "secret-token"
    assert cfg.poll_interval_seconds == 300
    assert cfg.field_map == DEFAULT_FIELD_MAP


def test_trailing_slash_stripped_from_server_base_url(tmp_path):
    path = tmp_path / "endpoint_agent.env"
    path.write_text(
        "SERVER_BASE_URL=https://site.example.local/\n"
        "ENDPOINT_UUID=u\nENDPOINT_TOKEN=t\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.server_base_url == "https://site.example.local"


def test_field_map_override(tmp_path):
    cfg = load_config(_write_env(tmp_path, "FIELD_MAP_PRINTER_NAME=7\n"))
    assert cfg.field_map["printer_name"] == 7
    assert cfg.field_map["user_name"] == DEFAULT_FIELD_MAP["user_name"]


def test_allowlist_denylist_parsed_as_comma_separated(tmp_path):
    cfg = load_config(_write_env(tmp_path, "PRINTER_ALLOWLIST=HP-*, Canon-*\nPRINTER_DENYLIST=Fax\n"))
    assert cfg.printer_allowlist == ["HP-*", "Canon-*"]
    assert cfg.printer_denylist == ["Fax"]


def test_comments_and_blank_lines_ignored(tmp_path):
    path = tmp_path / "endpoint_agent.env"
    path.write_text(
        "# комментарий\n\nSERVER_BASE_URL=https://site.example.local\n"
        "ENDPOINT_UUID=u\nENDPOINT_TOKEN=t\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.server_base_url == "https://site.example.local"
