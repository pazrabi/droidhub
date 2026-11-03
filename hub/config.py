from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from dotenv import dotenv_values
except ImportError:
    dotenv_values = None

import yaml


@dataclass
class DeviceConfig:
    id: str
    name: str
    host: str
    port: int = 5555


@dataclass
class Settings:
    devices_file: str = "config/devices.yaml"
    db_path: str = "data/hub.db"
    admin_user: str = ""
    admin_password_hash: str = ""
    session_secret: str = ""
    mcp_token: str = ""
    api_token: str = ""
    adb_path: str = "adb"
    allow_shell: bool = False
    rate_limit_per_min: int = 60
    telegram_token: str = ""
    telegram_chat_id: str = ""
    temp_warn_c: float = 40.0
    temp_alert_c: float = 45.0
    temp_shutdown_c: float = 50.0
    full_charge_hours: float = 6.0
    offline_alert_min: float = 10.0
    alert_cooldown_min: float = 30.0
    alert_tag: str = "droidhub"
    public_url: str = ""
    client_tokens: str = ""

    def __post_init__(self) -> None:
        if self.public_url:
            self.public_url = self.public_url.strip().rstrip("/")


def load_devices(path: str | Path = "config/devices.yaml") -> list[DeviceConfig]:
    """Загружает список устройств из YAML-файла."""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"Файл конфигурации устройств не найден: {file_path}")

    with open(file_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not data or not isinstance(data, dict):
        return []

    raw_devices = data.get("devices")
    if not raw_devices or not isinstance(raw_devices, list):
        return []

    devices: list[DeviceConfig] = []
    for item in raw_devices:
        if not isinstance(item, dict):
            continue
        dev_id = str(item.get("id", "")).strip()
        if not dev_id:
            continue
        name = str(item.get("name", dev_id)).strip()
        host = str(item.get("host", "")).strip()
        port_raw = item.get("port", 5555)
        try:
            port = int(port_raw)
        except (ValueError, TypeError):
            port = 5555

        devices.append(
            DeviceConfig(
                id=dev_id,
                name=name,
                host=host,
                port=port,
            )
        )

    return devices


def _parse_env_file(path: str | Path) -> dict[str, str]:
    """Считывает переменные из .env файла."""
    file_path = Path(path)
    if not file_path.is_file():
        return {}

    if dotenv_values is not None:
        try:
            values = dotenv_values(file_path)
            return {k: str(v) for k, v in values.items() if v is not None}
        except Exception:
            pass

    res: dict[str, str] = {}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    res[key] = val
    except OSError:
        pass
    return res


def get_settings(env_file: str | Path | None = None) -> Settings:
    """
    Считывает настройки из переменных окружения HUB_* и .env.
    Переменные окружения процесса имеют приоритет над .env.
    """
    if env_file is None:
        env_file_path = os.environ.get("HUB_ENV_FILE")
        if env_file_path:
            env_file = Path(env_file_path)
        elif Path(".env").is_file():
            env_file = Path(".env")
        else:
            project_env = Path(__file__).resolve().parent.parent / ".env"
            if project_env.is_file():
                env_file = project_env
            else:
                env_file = Path(".env")

    env_vars = _parse_env_file(env_file)

    def get_val(key: str, default: str) -> str:
        if key in os.environ:
            return os.environ[key]
        if key in env_vars:
            return env_vars[key]
        return default

    allow_shell_raw = get_val("HUB_ALLOW_SHELL", "false").strip().lower()
    allow_shell = allow_shell_raw in ("1", "true", "yes", "on", "t")

    rate_limit_raw = get_val("HUB_RATE_LIMIT_PER_MIN", "60").strip()
    try:
        rate_limit_per_min = int(rate_limit_raw)
    except (ValueError, TypeError):
        rate_limit_per_min = 60

    def parse_float(key: str, default: float) -> float:
        val_str = get_val(key, str(default)).strip()
        try:
            return float(val_str)
        except (ValueError, TypeError):
            return default

    telegram_token = get_val("HUB_TELEGRAM_TOKEN", "").strip()
    telegram_chat_id = get_val("HUB_TELEGRAM_CHAT_ID", "").strip()
    temp_warn_c = parse_float("HUB_TEMP_WARN_C", 40.0)
    temp_alert_c = parse_float("HUB_TEMP_ALERT_C", 45.0)
    temp_shutdown_c = parse_float("HUB_TEMP_SHUTDOWN_C", 50.0)
    full_charge_hours = parse_float("HUB_FULL_CHARGE_HOURS", 6.0)
    offline_alert_min = parse_float("HUB_OFFLINE_ALERT_MIN", 10.0)
    alert_cooldown_min = parse_float("HUB_ALERT_COOLDOWN_MIN", 30.0)
    alert_tag = get_val("HUB_ALERT_TAG", "droidhub").strip() or "droidhub"
    public_url = get_val("HUB_PUBLIC_URL", "").strip().rstrip("/")
    client_tokens = get_val("HUB_CLIENT_TOKENS", "").strip()

    return Settings(
        devices_file=get_val("HUB_DEVICES_FILE", "config/devices.yaml"),
        db_path=get_val("HUB_DB_PATH", "data/hub.db"),
        admin_user=get_val("HUB_ADMIN_USER", ""),
        admin_password_hash=get_val("HUB_ADMIN_PASSWORD_HASH", ""),
        session_secret=get_val("HUB_SESSION_SECRET", ""),
        mcp_token=get_val("HUB_MCP_TOKEN", ""),
        api_token=get_val("HUB_API_TOKEN", ""),
        adb_path=get_val("HUB_ADB_PATH", "adb"),
        allow_shell=allow_shell,
        rate_limit_per_min=rate_limit_per_min,
        telegram_token=telegram_token,
        telegram_chat_id=telegram_chat_id,
        temp_warn_c=temp_warn_c,
        temp_alert_c=temp_alert_c,
        temp_shutdown_c=temp_shutdown_c,
        full_charge_hours=full_charge_hours,
        offline_alert_min=offline_alert_min,
        alert_cooldown_min=alert_cooldown_min,
        alert_tag=alert_tag,
        public_url=public_url,
        client_tokens=client_tokens,
    )


__all__ = ["Settings", "get_settings", "DeviceConfig", "load_devices"]
