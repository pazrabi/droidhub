"""Автономный модуль для открытия реестра явных USB-привязок DroidHub.

Обеспечивает отдельное, изолированное хранилище связей для каждого HTTPS API
сервера DroidHub (на основе HubClient.devices_url).

Ключевые свойства:
- Строгая проверка входного HTTPS URL без сетевых вызовов.
- Запрет параметров запроса (query), фрагментов (fragment) и учетных данных (userinfo).
- Каноническая нормализация схемы (https), регистра хоста и порта 443.
- Изоляция разных развертываний по API путям и портам.
- Безопасное имя файла (хэш SHA-256): исходный URL никогда не попадает в имя файла,
  содержимое или текст сообщений об ошибках.
- Ленивое создание: вызов открытия не создает каталог или файл на диске до явной записи.
- Платформо-зависимый каталог настроек по умолчанию (macOS, Windows, Linux).
- Полная совместимость с существующими LinkRegistry и DeviceLinkService.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import posixpath
import sys
import urllib.parse

from desktop.device_links import (
    DeviceLinkError,
    DeviceLinkValidationError,
    LinkRegistry,
)
from desktop.link_service import DeviceLinkService


class LinkStorageError(DeviceLinkError):
    """Базовое исключение для ошибок хранилища связей."""
    pass


class LinkStorageValidationError(LinkStorageError, DeviceLinkValidationError, ValueError):
    """Ошибка валидации входного URL или параметров хранилища связей."""
    pass


def get_default_storage_dir() -> Path:
    """Возвращает каталог настроек пользователя DroidHub по умолчанию для текущей ОС.

    - macOS: ~/Library/Application Support/DroidHub
    - Windows: %LOCALAPPDATA%/DroidHub (с fallback на %APPDATA%/DroidHub или ~/AppData/Local/DroidHub)
    - Linux / POSIX: $XDG_CONFIG_HOME/DroidHub или ~/.config/DroidHub
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "DroidHub"
    elif sys.platform.startswith("win") or os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data and local_app_data.strip():
            return Path(local_app_data.strip()) / "DroidHub"
        app_data = os.environ.get("APPDATA")
        if app_data and app_data.strip():
            return Path(app_data.strip()) / "DroidHub"
        return Path.home() / "AppData" / "Local" / "DroidHub"
    else:
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        if xdg_config and xdg_config.strip():
            return Path(xdg_config.strip()) / "DroidHub"
        return Path.home() / ".config" / "DroidHub"


def normalize_devices_url(devices_url: str) -> str:
    """Валидирует и нормализует HTTPS URL API устройств DroidHub.

    Гарантии:
    - Без сетевых вызовов.
    - Требует строгий HTTPS.
    - Запрещает query, fragment и userinfo (логин/пароль).
    - Нормализует регистр схемы и хоста.
    - Нормализует порт (443 всегда явно присутствует в ключе, нестандартные порты сохраняются).
    - Нормализует путь API, сохраняя изоляцию разных развертываний.
    - Не выводит исходный URL в сообщения об ошибках при сбое валидации.
    """
    if not isinstance(devices_url, str) or isinstance(devices_url, bool):
        raise LinkStorageValidationError(
            f"devices_url must be a string, got {type(devices_url).__name__}"
        )

    raw = devices_url.strip()
    if not raw:
        raise LinkStorageValidationError("devices_url cannot be empty")

    for ch in raw:
        if ord(ch) < 32 or ord(ch) == 127:
            raise LinkStorageValidationError(
                "devices_url contains forbidden control characters"
            )

    if "?" in raw:
        raise LinkStorageValidationError(
            "devices_url must not contain query parameters"
        )

    if "#" in raw:
        raise LinkStorageValidationError(
            "devices_url must not contain fragments"
        )

    parsed = urllib.parse.urlsplit(raw)

    scheme = (parsed.scheme or "").lower()
    if scheme != "https":
        raise LinkStorageValidationError("devices_url scheme must be 'https'")

    if (
        parsed.username is not None
        or parsed.password is not None
        or "@" in (parsed.netloc or "")
    ):
        raise LinkStorageValidationError(
            "devices_url must not contain user credentials"
        )

    hostname = parsed.hostname
    if not hostname or not hostname.strip():
        raise LinkStorageValidationError("devices_url host is missing")

    if any(c in (parsed.netloc or "") for c in (" ", "\t")):
        raise LinkStorageValidationError(
            "devices_url contains invalid whitespace characters"
        )

    if (parsed.netloc or "").endswith(":"):
        raise LinkStorageValidationError(
            "devices_url port is not a valid integer"
        )

    norm_host = hostname.strip().lower()

    try:
        port = parsed.port
    except ValueError:
        raise LinkStorageValidationError(
            "devices_url port is not a valid integer"
        )

    if port is not None:
        if port <= 0 or port > 65535:
            raise LinkStorageValidationError(
                "devices_url port is out of valid range (1-65535)"
            )
        canonical_port = port
    else:
        canonical_port = 443

    raw_path = parsed.path or "/"
    if raw_path.startswith("/"):
        clean_path = "/" + raw_path.lstrip("/")
    else:
        clean_path = "/" + raw_path
    norm_path = posixpath.normpath(clean_path)
    if not norm_path.startswith("/"):
        norm_path = "/" + norm_path

    if ":" in norm_host:
        host_str = f"[{norm_host}]"
    else:
        host_str = norm_host

    return f"https://{host_str}:{canonical_port}{norm_path}"


get_canonical_url = normalize_devices_url


def get_storage_key(devices_url: str) -> str:
    """Вычисляет устойчивый криптографический ключ (хэш SHA-256) для URL устройств хаба."""
    canonical = normalize_devices_url(devices_url)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_storage_path(
    devices_url: str,
    *,
    storage_dir: Path | None = None,
) -> Path:
    """Возвращает путь к файлу реестра связей для указанного devices_url.

    Вызов НЕ создает каталог или файл на диске.
    """
    base_dir = (
        Path(storage_dir).expanduser()
        if storage_dir is not None
        else get_default_storage_dir()
    )
    key = get_storage_key(devices_url)
    return base_dir / f"links_{key}.json"


def open_link_service(
    devices_url: str,
    *,
    storage_dir: Path | None = None,
) -> DeviceLinkService:
    """Открывает сервис явных USB-привязок DroidHub для указанного HTTPS API сервера.

    Параметры:
        devices_url: Проверенный HTTPS URL эндпоинта API устройств (HubClient.devices_url).
        storage_dir: Каталог хранения файлов реестра связей (для тестов или portable-режима).
                     Если None, используется системный каталог настроек DroidHub.

    Возвращает:
        Экземпляр DeviceLinkService, подключенный к отдельному реестру для данного сервера.

    Гарантии:
        - Изоляция по схеме, хосту, порту и пути API.
        - Вызов открытия НЕ создает каталог или файл на диске до явной записи.
        - Исходный URL не попадает в имя файла, его содержимое или тексты ошибок.
        - Полная транзакционность через LinkRegistry.
    """
    path = get_storage_path(devices_url, storage_dir=storage_dir)
    registry = LinkRegistry(path)
    return DeviceLinkService(registry=registry)


__all__ = [
    "LinkStorageError",
    "LinkStorageValidationError",
    "get_canonical_url",
    "get_default_storage_dir",
    "get_storage_key",
    "get_storage_path",
    "normalize_devices_url",
    "open_link_service",
]
