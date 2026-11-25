"""Standalone macOS bundle integration module for DroidHub Hub Helper.

Provides integration with the bundled macOS hub-helper service,
offering a drop-in inventory fetcher compatible with HubClient.
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

from desktop.hub_client import HubClientError, HubDevice
from desktop.macos_hub_transport import (
    ErrorKind,
    MacOSHubTransport,
    MacOSHubTransportError,
)

__all__ = [
    "CANONICAL_KEY",
    "CANONICAL_URL",
    "MacOSHubInventory",
    "MacOSHubInventoryError",
    "bundled_inventory",
]

CANONICAL_KEY: str = "https://droidhub.molokohub.site:443/api/devices"
CANONICAL_URL: str = CANONICAL_KEY


class MacOSHubInventoryError(HubClientError):
    """Исключение для ошибок инвентаризации macOS Hub Helper."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable: bool = retryable


def _make_error(message: str, *, retryable: bool = False) -> HubClientError:
    """Создает безопасный HubClientError с атрибутом retryable."""
    err = HubClientError(message)
    err.retryable = retryable
    return err


class MacOSHubInventory:
    """Интеграция с локальной службой инвентаризации macOS Hub Helper."""

    canonical_key: str = CANONICAL_KEY
    devices_url: str = CANONICAL_KEY
    base_url: str = "https://droidhub.molokohub.site:443"

    def __init__(
        self,
        transport_or_library_path: MacOSHubTransport | pathlib.Path | str | None = None,
        signing_cert_sha1: str | None = None,
        *,
        transport: MacOSHubTransport | None = None,
        library_path: pathlib.Path | str | None = None,
    ) -> None:
        if transport is not None:
            self._transport = transport
        elif isinstance(transport_or_library_path, MacOSHubTransport):
            self._transport = transport_or_library_path
        elif (transport_or_library_path is not None or library_path is not None) and (signing_cert_sha1 is not None):
            lib_path = pathlib.Path(library_path or transport_or_library_path)  # type: ignore[arg-type]
            self._transport = MacOSHubTransport(
                library_path=lib_path,
                signing_cert_sha1=signing_cert_sha1,
            )
        else:
            raise ValueError("Требуется transport или пара library_path и signing_cert_sha1")

    @property
    def transport(self) -> MacOSHubTransport:
        """Используемый транспорт для обращения к macOS Hub Helper."""
        return self._transport

    def __enter__(self) -> MacOSHubInventory:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """Закрывает ресурсы клиента (резервный метод)."""
        pass

    def list_devices(self) -> list[HubDevice]:
        """Запрашивает список устройств через macOS Hub Transport.

        Возвращает:
            list[HubDevice]: список обнаруженных устройств.

        Исключения:
            HubClientError: при ошибках транспорта, разбора ответа или валидации.
                Атрибут retryable равен True только для временных ошибок (temporary)
                или недоступности службы (unavailable).
        """
        try:
            raw_payload = self._transport.list_devices_payload()
        except MacOSHubTransportError as exc:
            kind = getattr(exc, "kind", None)
            is_retryable = kind in (
                ErrorKind.temporary,
                ErrorKind.unavailable,
                "temporary",
                "unavailable",
            )

            if kind == ErrorKind.unavailable:
                msg = "Служба macOS hub helper недоступна на этой системе"
            elif kind == ErrorKind.temporary:
                msg = "Временная ошибка службы macOS hub helper; повторите попытку позже"
            elif kind == ErrorKind.authorization:
                msg = "Ошибка авторизации: доступ к службе macOS hub helper запрещён"
            elif kind == ErrorKind.trust:
                msg = "Ошибка доверия TLS-сертификату службы macOS hub helper"
            elif kind == ErrorKind.invalid:
                msg = "Некорректный запрос или ответ службы macOS hub helper"
            else:
                msg = "Непредвиденная ошибка службы macOS hub helper"

            raise _make_error(msg, retryable=is_retryable) from None
        except HubClientError:
            raise
        except Exception as exc:
            raise _make_error(
                f"Ошибка при обращении к службе macOS hub helper: {type(exc).__name__}",
                retryable=False,
            ) from None

        try:
            text = raw_payload.decode("utf-8")
        except UnicodeDecodeError:
            raise _make_error(
                "Ошибка декодирования ответа службы macOS hub helper как UTF-8",
                retryable=False,
            ) from None

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise _make_error(
                "Некорректный JSON в ответе службы macOS hub helper",
                retryable=False,
            ) from None

        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            if "devices" in data and isinstance(data["devices"], list):
                items = data["devices"]
            else:
                raise _make_error(
                    "Некорректный формат ответа службы macOS hub helper: отсутствует список устройств",
                    retryable=False,
                )
        else:
            raise _make_error(
                "Некорректный формат ответа службы macOS hub helper: ожидался список устройств",
                retryable=False,
            )

        devices: list[HubDevice] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise _make_error(
                    f"Некорректный элемент #{index} в списке устройств: ожидался словарь",
                    retryable=False,
                )
            if "id" not in item or not item["id"] or not isinstance(item["id"], (str, int)):
                raise _make_error(
                    f"В элементе #{index} списка устройств отсутствует обязательное поле 'id'",
                    retryable=False,
                )
            try:
                device = HubDevice.from_dict(item)
            except HubClientError as exc:
                raise _make_error(str(exc), retryable=False) from None
            except Exception:
                raise _make_error(
                    f"Некорректная структура элемента #{index} в списке устройств",
                    retryable=False,
                ) from None
            devices.append(device)

        return devices


def bundled_inventory(
    *,
    _meipass: str | pathlib.Path | None = None,
) -> MacOSHubInventory | None:
    """Создает и возвращает экземпляр MacOSHubInventory для packaged macOS-приложения.

    Выполняет проверку окружения:
    - sys.platform == "darwin"
    - sys.frozen == True
    - sys._MEIPASS задан и является абсолютным путем

    Загружает:
    - hub/signing-cert.sha1 (ASCII)
    - hub/macos_hub_xpc_client.dylib

    Возвращает:
    - None, если окружение не соответствует требованиям или ни один из файлов не существует.

    Исключения:
    - HubClientError (retryable=False) при частичной или невалидной конфигурации (fallback запрещен).
    """
    if sys.platform != "darwin":
        return None
    if not getattr(sys, "frozen", False):
        return None

    raw_meipass = _meipass if _meipass is not None else getattr(sys, "_MEIPASS", None)
    if not raw_meipass:
        return None

    try:
        base_dir = pathlib.Path(raw_meipass)
    except Exception:
        return None

    if not base_dir.is_absolute():
        return None

    cert_path = base_dir / "hub" / "signing-cert.sha1"
    dylib_path = base_dir / "hub" / "macos_hub_xpc_client.dylib"

    cert_exists = cert_path.exists()
    dylib_exists = dylib_path.exists()

    if not cert_exists and not dylib_exists:
        return None

    if not cert_exists or not dylib_exists:
        missing = "hub/signing-cert.sha1" if not cert_exists else "hub/macos_hub_xpc_client.dylib"
        raise _make_error(
            f"Неполная конфигурация пакета macOS: отсутствует {missing}",
            retryable=False,
        )

    if not cert_path.is_file():
        raise _make_error(
            "Конфигурация пакета macOS повреждена: hub/signing-cert.sha1 не является файлом",
            retryable=False,
        )

    if not dylib_path.is_file():
        raise _make_error(
            "Конфигурация пакета macOS повреждена: hub/macos_hub_xpc_client.dylib не является файлом",
            retryable=False,
        )

    try:
        cert_text = cert_path.read_text(encoding="ascii")
    except (UnicodeDecodeError, OSError) as exc:
        raise _make_error(
            f"Ошибка чтения hub/signing-cert.sha1: {exc}",
            retryable=False,
        ) from None

    sha1 = cert_text.strip()
    if len(sha1) != 40 or any(c not in "0123456789abcdefABCDEF" for c in sha1):
        raise _make_error(
            "Некорректный SHA-1 отпечаток сертификата в hub/signing-cert.sha1 (ожидалось 40 hex символов)",
            retryable=False,
        )

    try:
        transport = MacOSHubTransport(library_path=dylib_path, signing_cert_sha1=sha1)
    except Exception as exc:
        raise _make_error(
            f"Не удалось инициализировать транспорт macOS hub helper: {exc}",
            retryable=False,
        ) from None

    return MacOSHubInventory(transport=transport)
