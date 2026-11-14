"""Клиент ADB для безопасного получения списка устройств без shell/root/connect."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field


# Запрещенные команды для исключения любых модификаций и вторжений
FORBIDDEN_ADB_COMMANDS = {
    "connect",
    "disconnect",
    "tcpip",
    "shell",
    "root",
    "unroot",
    "remount",
    "reboot",
    "install",
    "uninstall",
    "push",
    "pull",
    "sync",
    "backup",
    "restore",
    "forward",
    "reverse",
}


@dataclass
class DeviceInfo:
    """Структурированная информация о подключенном Android-устройстве."""
    serial: str
    state: str  # 'device', 'unauthorized', 'offline', etc.
    state_display: str
    is_network: bool
    connection_type: str  # 'USB' или 'Сетевое (ADB TCP/IP)'
    is_ready: bool  # True если state == 'device'
    model: str | None = None
    product: str | None = None
    device_code: str | None = None
    transport_id: str | None = None
    usb_info: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        """Понятное имя устройства для интерфейса."""
        if self.model:
            cleaned = self.model.replace("_", " ")
            return f"{cleaned} ({self.serial})"
        if self.device_code:
            return f"{self.device_code} ({self.serial})"
        return self.serial

    @property
    def model_display(self) -> str:
        """Модель без серийника для таблиц."""
        if self.model:
            return self.model.replace("_", " ")
        if self.device_code:
            return self.device_code
        return "Неизвестная модель"


class AdbClientError(Exception):
    """Базовая ошибка клиента ADB."""


def is_network_serial(serial: str) -> bool:
    """Определяет, является ли серийный номер сетевым адресом (host:port или IP:port).

    USB-устройства имеют аппаратные серийные номера без двоеточия (буквенно-цифровые).
    Сетевые устройства содержат двоеточие с номером порта (например 192.168.1.50:5555).
    """
    if not serial or not isinstance(serial, str):
        return False
    # Проверка на host:port или IP:port
    if ":" in serial:
        host, _, port = serial.rpartition(":")
        if port.isdigit() and host:
            return True
    return False


def map_state_to_russian(state: str) -> str:
    """Преобразует статус ADB в понятную надпись на русском языке."""
    normalized = state.lower().strip()
    if normalized == "device":
        return "Готов к работе"
    if normalized == "unauthorized":
        return "Не авторизован (разрешите на телефоне)"
    if normalized == "offline":
        return "Офлайн (нет ответа)"
    if normalized == "authorizing":
        return "Авторизация..."
    if normalized == "connecting":
        return "Подключение..."
    if "no permissions" in normalized:
        return "Нет прав доступа USB (udev/права)"
    if normalized == "recovery":
        return "Режим восстановления (Recovery)"
    if normalized == "bootloader":
        return "Загрузчик (Bootloader)"
    if normalized == "sideload":
        return "Режим Sideload"
    return f"Статус: {state}"


def parse_adb_devices_output(output: str) -> list[DeviceInfo]:
    """Парсит вывод команды `adb devices -l`.

    Корректно обрабатывает:
    - Заголовок 'List of devices attached'
    - Сообщения запуска демона (* daemon not running...)
    - Устройства со статусами device, unauthorized, offline, no permissions
    - Ключи usb:, product:, model:, device:, transport_id:
    """
    devices: list[DeviceInfo] = []
    lines = output.splitlines()

    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Пропуск строк демона и заголовка
        if line.startswith("*") or line.startswith("List of devices attached"):
            continue

        parts = line.split()
        if len(parts) < 2:
            continue

        serial = parts[0]
        # Проверка на наличие "no permissions"
        raw_state = parts[1]
        kv_start_index = 2

        if raw_state == "no" and len(parts) > 2 and parts[2].startswith("permissions"):
            raw_state = "no permissions"
            # Найти, где начинаются ключ:значение (обычно transport_id:)
            for idx, p in enumerate(parts[3:], start=3):
                if ":" in p and any(p.startswith(k) for k in ("product:", "model:", "device:", "transport_id:", "usb:")):
                    kv_start_index = idx
                    break
            else:
                kv_start_index = len(parts)

        # Парсинг свойств key:value
        attrs: dict[str, str] = {}
        for token in parts[kv_start_index:]:
            if ":" in token:
                k, _, v = token.partition(":")
                attrs[k] = v

        is_net = is_network_serial(serial)
        conn_type = "Сетевое (ADB TCP/IP)" if is_net else "USB"
        state_display = map_state_to_russian(raw_state)
        is_ready = (raw_state.lower() == "device")

        devices.append(
            DeviceInfo(
                serial=serial,
                state=raw_state,
                state_display=state_display,
                is_network=is_net,
                connection_type=conn_type,
                is_ready=is_ready,
                model=attrs.get("model"),
                product=attrs.get("product"),
                device_code=attrs.get("device"),
                transport_id=attrs.get("transport_id"),
                usb_info=attrs.get("usb"),
                extra=attrs,
            )
        )

    return devices


class AdbClient:
    """Безопасный клиент для взаимодействия с локальным сервером ADB."""

    def __init__(self, adb_path: str = "adb") -> None:
        self.adb_path = adb_path

    def _execute(self, args: list[str], timeout: float = 10.0) -> str:
        """Безопасно запускает команду ADB списком аргументов без shell=True."""
        # Защита от недопустимых команд
        for arg in args:
            if arg.lower() in FORBIDDEN_ADB_COMMANDS:
                raise AdbClientError(
                    f"Команда '{arg}' запрещена из соображений безопасности. "
                    "Разрешены только безопасные команды просмотра устройств."
                )

        cmd = [self.adb_path, *args]
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                shell=False,
                check=False,
            )
            if proc.returncode != 0:
                err_msg = (proc.stderr or proc.stdout or "").strip()
                raise AdbClientError(
                    f"ADB вернул код ошибки {proc.returncode}: {err_msg}"
                )
            return proc.stdout or ""
        except FileNotFoundError as exc:
            raise AdbClientError(
                f"Исполняемый файл ADB не найден по пути: {self.adb_path}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbClientError(
                f"Превышено время ожидания ответа от ADB ({timeout} с)."
            ) from exc

    def list_devices(self) -> list[DeviceInfo]:
        """Возвращает список подключенных устройств с полной информацией."""
        raw_output = self._execute(["devices", "-l"], timeout=8.0)
        return parse_adb_devices_output(raw_output)

    def start_server(self) -> bool:
        """Запускает ADB-сервер при необходимости."""
        try:
            self._execute(["start-server"], timeout=10.0)
            return True
        except AdbClientError:
            return False

    def get_version(self) -> str:
        """Возвращает информацию о версии ADB."""
        return self._execute(["version"], timeout=3.0).strip()
