from __future__ import annotations

from pathlib import Path

import pytest

from hub.config import DeviceConfig, Settings, get_settings, load_devices


def test_device_config_dataclass() -> None:
    dev = DeviceConfig(id="test_dev", name="Test Device", host="192.168.1.100")
    assert dev.id == "test_dev"
    assert dev.name == "Test Device"
    assert dev.host == "192.168.1.100"
    assert dev.port == 5555

    dev_custom_port = DeviceConfig(
        id="dev2", name="Device 2", host="192.168.1.101", port=5556
    )
    assert dev_custom_port.port == 5556


def test_load_devices_real_file() -> None:
    devices = load_devices("config/devices.yaml")
    assert len(devices) == 2
    assert devices[0].id == "realme"
    assert devices[0].name == "Realme C21Y"
    assert devices[0].host == "100.64.0.1"
    assert devices[0].port == 5555

    assert devices[1].id == "redmi"
    assert devices[1].name == "Redmi Note 11"
    assert devices[1].host == "100.64.0.2"
    assert devices[1].port == 5555


def test_load_devices_custom_yaml(tmp_path: Path) -> None:
    yaml_content = """
devices:
  - id: custom1
    name: Custom Phone 1
    host: 10.0.0.1
  - id: custom2
    name: Custom Phone 2
    host: 10.0.0.2
    port: 5560
"""
    custom_yaml = tmp_path / "devices.yaml"
    custom_yaml.write_text(yaml_content, encoding="utf-8")

    devices = load_devices(custom_yaml)
    assert len(devices) == 2
    assert devices[0].id == "custom1"
    assert devices[0].name == "Custom Phone 1"
    assert devices[0].host == "10.0.0.1"
    assert devices[0].port == 5555

    assert devices[1].id == "custom2"
    assert devices[1].name == "Custom Phone 2"
    assert devices[1].host == "10.0.0.2"
    assert devices[1].port == 5560


def test_load_devices_empty_and_missing(tmp_path: Path) -> None:
    empty_yaml = tmp_path / "empty.yaml"
    empty_yaml.write_text("", encoding="utf-8")
    assert load_devices(empty_yaml) == []

    non_existent = tmp_path / "non_existent.yaml"
    with pytest.raises(FileNotFoundError):
        load_devices(non_existent)


def test_settings_defaults() -> None:
    settings = Settings()
    assert settings.devices_file == "config/devices.yaml"
    assert settings.db_path == "data/hub.db"
    assert settings.admin_user == ""
    assert settings.admin_password_hash == ""
    assert settings.session_secret == ""
    assert settings.mcp_token == ""
    assert settings.api_token == ""
    assert settings.adb_path == "adb"
    assert settings.allow_shell is False
    assert settings.rate_limit_per_min == 60
    assert settings.telegram_token == ""
    assert settings.telegram_chat_id == ""
    assert settings.temp_warn_c == 40.0
    assert settings.temp_alert_c == 45.0
    assert settings.temp_shutdown_c == 50.0
    assert settings.full_charge_hours == 6.0
    assert settings.offline_alert_min == 10.0
    assert settings.alert_cooldown_min == 30.0
    assert settings.alert_tag == "droidhub"
    assert settings.public_url == ""
    assert settings.client_tokens == ""


def test_get_settings_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    # Очищаем возможные переменные окружения
    for key in (
        "HUB_ADMIN_USER",
        "HUB_ADMIN_PASSWORD_HASH",
        "HUB_SESSION_SECRET",
        "HUB_MCP_TOKEN",
        "HUB_API_TOKEN",
        "HUB_ADB_PATH",
        "HUB_ALLOW_SHELL",
        "HUB_RATE_LIMIT_PER_MIN",
        "HUB_DEVICES_FILE",
        "HUB_DB_PATH",
        "HUB_ENV_FILE",
        "HUB_CLIENT_TOKENS",
    ):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("HUB_ADMIN_USER", "superadmin")
    monkeypatch.setenv("HUB_ADMIN_PASSWORD_HASH", "hashed_pwd_123")
    monkeypatch.setenv("HUB_SESSION_SECRET", "super_secret_session")
    monkeypatch.setenv("HUB_MCP_TOKEN", "token_mcp_xyz")
    monkeypatch.setenv("HUB_API_TOKEN", "token_api_abc")
    monkeypatch.setenv("HUB_ADB_PATH", "/usr/local/bin/adb")
    monkeypatch.setenv("HUB_ALLOW_SHELL", "true")
    monkeypatch.setenv("HUB_RATE_LIMIT_PER_MIN", "120")
    monkeypatch.setenv("HUB_DEVICES_FILE", "custom/devices.yaml")
    monkeypatch.setenv("HUB_DB_PATH", "custom/hub.db")
    monkeypatch.setenv("HUB_CLIENT_TOKENS", "pool:Abc-123_x:30:nopower")

    settings = get_settings()
    assert settings.admin_user == "superadmin"
    assert settings.admin_password_hash == "hashed_pwd_123"
    assert settings.session_secret == "super_secret_session"
    assert settings.mcp_token == "token_mcp_xyz"
    assert settings.api_token == "token_api_abc"
    assert settings.adb_path == "/usr/local/bin/adb"
    assert settings.allow_shell is True
    assert settings.rate_limit_per_min == 120
    assert settings.devices_file == "custom/devices.yaml"
    assert settings.db_path == "custom/hub.db"
    assert settings.client_tokens == "pool:Abc-123_x:30:nopower"


def test_get_settings_default_call(monkeypatch: pytest.MonkeyPatch) -> None:
    # Очищаем все возможные переменные окружения
    for key in (
        "HUB_ADMIN_USER",
        "HUB_ADMIN_PASSWORD_HASH",
        "HUB_SESSION_SECRET",
        "HUB_MCP_TOKEN",
        "HUB_API_TOKEN",
        "HUB_ADB_PATH",
        "HUB_ALLOW_SHELL",
        "HUB_RATE_LIMIT_PER_MIN",
        "HUB_DEVICES_FILE",
        "HUB_DB_PATH",
        "HUB_ENV_FILE",
        "HUB_CLIENT_TOKENS",
    ):
        monkeypatch.delenv(key, raising=False)

    # Вызов get_settings() без аргументов (основной контракт)
    settings = get_settings()
    assert isinstance(settings, Settings)
    assert settings.devices_file == "config/devices.yaml"
    assert settings.db_path == "data/hub.db"
    assert settings.admin_user == ""
    assert settings.admin_password_hash == ""
    assert settings.session_secret == ""
    assert settings.mcp_token == ""
    assert settings.api_token == ""
    assert settings.adb_path == "adb"
    assert settings.allow_shell is False
    assert settings.rate_limit_per_min == 60
    assert settings.client_tokens == ""

    # Проверяем чтение переменных окружения при вызове без аргументов
    monkeypatch.setenv("HUB_ADMIN_USER", "default_caller")
    monkeypatch.setenv("HUB_ALLOW_SHELL", "true")
    monkeypatch.setenv("HUB_RATE_LIMIT_PER_MIN", "45")
    monkeypatch.setenv("HUB_CLIENT_TOKENS", "default:tok_123")

    settings_env = get_settings()
    assert settings_env.admin_user == "default_caller"
    assert settings_env.allow_shell is True
    assert settings_env.rate_limit_per_min == 45
    assert settings_env.client_tokens == "default:tok_123"


def test_get_settings_from_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "HUB_ADMIN_USER",
        "HUB_ADMIN_PASSWORD_HASH",
        "HUB_SESSION_SECRET",
        "HUB_MCP_TOKEN",
        "HUB_API_TOKEN",
        "HUB_ADB_PATH",
        "HUB_ALLOW_SHELL",
        "HUB_RATE_LIMIT_PER_MIN",
        "HUB_DEVICES_FILE",
        "HUB_DB_PATH",
    ):
        monkeypatch.delenv(key, raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text(
        """
HUB_ADMIN_USER=admin_from_file
HUB_SESSION_SECRET=secret_from_file
HUB_ALLOW_SHELL=1
HUB_RATE_LIMIT_PER_MIN=90
""",
        encoding="utf-8",
    )

    settings = get_settings(env_file=env_file)
    assert settings.admin_user == "admin_from_file"
    assert settings.session_secret == "secret_from_file"
    assert settings.allow_shell is True
    assert settings.rate_limit_per_min == 90
    assert settings.devices_file == "config/devices.yaml"
    assert settings.db_path == "data/hub.db"


def test_get_settings_env_var_overrides_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        """
HUB_ADMIN_USER=admin_from_file
HUB_ALLOW_SHELL=false
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("HUB_ADMIN_USER", "admin_from_env")
    monkeypatch.setenv("HUB_ALLOW_SHELL", "yes")

    settings = get_settings(env_file=env_file)
    assert settings.admin_user == "admin_from_env"
    assert settings.allow_shell is True


def test_get_settings_alert_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_TELEGRAM_TOKEN", "123456:ABC-DEF")
    monkeypatch.setenv("HUB_TELEGRAM_CHAT_ID", "-100123456789")
    monkeypatch.setenv("HUB_TEMP_WARN_C", "42.5")
    monkeypatch.setenv("HUB_TEMP_ALERT_C", "47.0")
    monkeypatch.setenv("HUB_TEMP_SHUTDOWN_C", "52.0")
    monkeypatch.setenv("HUB_FULL_CHARGE_HOURS", "8.5")
    monkeypatch.setenv("HUB_OFFLINE_ALERT_MIN", "15.0")
    monkeypatch.setenv("HUB_ALERT_COOLDOWN_MIN", "45.0")
    monkeypatch.setenv("HUB_ALERT_TAG", "testhub")
    monkeypatch.setenv("HUB_PUBLIC_URL", "https://hub.example.com/")

    settings = get_settings()
    assert settings.telegram_token == "123456:ABC-DEF"
    assert settings.telegram_chat_id == "-100123456789"
    assert settings.temp_warn_c == 42.5
    assert settings.temp_alert_c == 47.0
    assert settings.temp_shutdown_c == 52.0
    assert settings.full_charge_hours == 8.5
    assert settings.offline_alert_min == 15.0
    assert settings.alert_cooldown_min == 45.0
    assert settings.alert_tag == "testhub"
    assert settings.public_url == "https://hub.example.com"


def test_settings_public_url_strips_slash() -> None:
    settings = Settings(public_url="https://hub.example.com/")
    assert settings.public_url == "https://hub.example.com"
    settings_no_slash = Settings(public_url="https://hub.example.com")
    assert settings_no_slash.public_url == "https://hub.example.com"
    settings_empty = Settings(public_url="")
    assert settings_empty.public_url == ""


def test_get_settings_client_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("HUB_CLIENT_TOKENS", "HUB_ENV_FILE"):
        monkeypatch.delenv(key, raising=False)

    # 1. Default empty
    settings = get_settings()
    assert settings.client_tokens == ""

    # 2. From env file
    env_file = tmp_path / ".env"
    env_file.write_text("HUB_CLIENT_TOKENS=pool:tok1:30:nopower,claude:tok2\n", encoding="utf-8")
    settings_file = get_settings(env_file=env_file)
    assert settings_file.client_tokens == "pool:tok1:30:nopower,claude:tok2"

    # 3. Process env overrides env file
    monkeypatch.setenv("HUB_CLIENT_TOKENS", "custom:env_override_tok")
    settings_override = get_settings(env_file=env_file)
    assert settings_override.client_tokens == "custom:env_override_tok"
