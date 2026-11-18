"""
desktop/readiness_usb_probe.py — синхронный USB-пробник готовности Android-устройства.

Проверяет, что конкретное устройство подключено по USB, доступно через ADB,
и возвращает версию Android и уровень заряда батареи.

Не требует __init__.py в пакете desktop (namespace package).
Не использует asyncio — только subprocess.run с бюджетом времени.
"""
from __future__ import annotations

import enum
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Callable


# ---------------------------------------------------------------------------
# Ошибки
# ---------------------------------------------------------------------------

class ErrorCode(enum.Enum):
    USB_DISCONNECTED = "USB_DISCONNECTED"
    ADB_UNAUTHORIZED = "ADB_UNAUTHORIZED"
    TIMEOUT          = "TIMEOUT"
    PROBE_FAILED     = "PROBE_FAILED"


class USBProbeError(Exception):
    """Ошибка пробника USB-готовности."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code

    def __repr__(self) -> str:
        return f"USBProbeError({self.code!r}, {str(self)!r})"


# ---------------------------------------------------------------------------
# Результат
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    serial: str
    transport_id: int
    state: str
    android_version: str
    battery_level: int


# ---------------------------------------------------------------------------
# Вспомогательные типы
# ---------------------------------------------------------------------------

# Runner: вызывается вместо subprocess.run при тестировании.
# Принимает те же аргументы, что и subprocess.run; возвращает CompletedProcess.
Runner = Callable[..., subprocess.CompletedProcess]


# ---------------------------------------------------------------------------
# Парсинг вывода `adb devices -l`
# ---------------------------------------------------------------------------

@dataclass
class _DeviceEntry:
    serial: str
    state: str
    transport_id: int
    transport_type: str   # "usb" | "local" | "network" | "unknown"
    qualifier: str        # всё остальное (product:, model:, device:, …)


# Строки вывода `adb devices -l`:
# <serial>              <state>   transport_id:<n>  [ключи…]
# Состояние: device, offline, unauthorized, authorizing, connecting…
_DEVICES_LINE_RE = re.compile(
    r"^(?P<serial>\S+)\s+"
    r"(?P<state>\S+)"
    r"(?:\s+(?P<rest>.+))?$"
)
_TRANSPORT_ID_RE = re.compile(r"transport_id:(\d+)")


def _parse_devices(output: str) -> list[_DeviceEntry]:
    """Разбирает вывод `adb devices -l` в список записей."""
    entries: list[_DeviceEntry] = []
    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        if not line or line.startswith("List of devices"):
            continue
        m = _DEVICES_LINE_RE.match(line)
        if not m:
            continue
        serial = m.group("serial")
        state = m.group("state")
        rest = m.group("rest") or ""
        rest = rest.strip()

        tid_m = _TRANSPORT_ID_RE.search(rest)
        transport_id = int(tid_m.group(1)) if tid_m else -1

        # Определяем тип транспорта по виду serial:
        #   IP:port  → network/TCP
        #   emulator-N → local
        #   xxxxxxxx (8+ hex) → usb
        if re.match(r"^(\d{1,3}\.){3}\d{1,3}:\d+$", serial):
            ttype = "network"
        elif serial.startswith("emulator-"):
            ttype = "local"
        elif re.match(r"^[0-9A-Fa-f]{6,}$", serial) or re.match(r"^[A-Za-z0-9._:+-]+$", serial):
            # Серийник USB не содержит точек с портом и не начинается с "emulator-"
            # Дополнительно исключаем TCP-форматы выше
            ttype = "usb"
        else:
            ttype = "unknown"

        qualifier = rest
        entries.append(_DeviceEntry(
            serial=serial,
            state=state,
            transport_id=transport_id,
            transport_type=ttype,
            qualifier=qualifier,
        ))
    return entries


def _is_usb(entry: _DeviceEntry) -> bool:
    """True, если запись соответствует USB-транспорту."""
    # network-транспорты имеют serial вида IP:port или host:port
    if re.match(r".*:\d+$", entry.serial):
        return False
    # Эмуляторы — не USB
    if entry.serial.startswith("emulator-"):
        return False
    return True


# ---------------------------------------------------------------------------
# Парсинг вывода `dumpsys battery`
# ---------------------------------------------------------------------------

def _parse_battery_level(output: str) -> int | None:
    """Возвращает уровень заряда 0–100 или None, если не найдено."""
    for line in output.splitlines():
        m = re.match(r"\s*level:\s*(-?\d+)", line)
        if m:
            level = int(m.group(1))
            return max(0, min(100, level))
    return None


# ---------------------------------------------------------------------------
# Основной класс
# ---------------------------------------------------------------------------

class USBReadinessProbe:
    """
    Синхронный пробник USB-готовности Android-устройства.

    Параметры:
        adb_path  — путь к исполняемому файлу adb (по умолчанию «adb»).
        runner    — замена subprocess.run для тестирования.
    """

    def __init__(self, adb_path: str = "adb", runner: Runner | None = None) -> None:
        self._adb = adb_path
        self._run: Runner = runner if runner is not None else subprocess.run

    # ------------------------------------------------------------------
    # Вспомогательный метод: запуск adb с учётом бюджета времени
    # ------------------------------------------------------------------

    def _run_adb(
        self,
        args: list[str],
        *,
        deadline: float,
        capture: bool = True,
    ) -> subprocess.CompletedProcess:
        """
        Запускает adb с переданными аргументами.
        deadline — монотонное время (time.monotonic()), после которого
        команда считается просроченной.
        Выбрасывает USBProbeError(TIMEOUT), если время истекло.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise USBProbeError(
                ErrorCode.TIMEOUT,
                "Истёк бюджет времени до запуска ADB-команды",
            )

        cmd = [self._adb, *args]
        try:
            result = self._run(
                cmd,
                capture_output=capture,
                text=True,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired as exc:
            raise USBProbeError(
                ErrorCode.TIMEOUT,
                f"Команда ADB превысила таймаут ({remaining:.1f} с): {' '.join(cmd)}",
            ) from exc
        except FileNotFoundError as exc:
            raise USBProbeError(
                ErrorCode.PROBE_FAILED,
                f"Исполняемый файл ADB не найден: {self._adb!r}",
            ) from exc
        return result

    # ------------------------------------------------------------------
    # Основной метод
    # ------------------------------------------------------------------

    def check_transport(self, device_id: str, *, timeout: float) -> ProbeResult:
        """
        Проверяет USB-готовность устройства.

        Параметры:
            device_id — серийный номер USB-устройства (точное совпадение).
            timeout   — общий бюджет времени в секундах.

        Возвращает ProbeResult с полями:
            serial, transport_id, state, android_version, battery_level

        Выбрасывает USBProbeError с кодами:
            USB_DISCONNECTED — устройство не найдено или offline
            ADB_UNAUTHORIZED — устройство не авторизовано
            TIMEOUT          — истёк таймаут
            PROBE_FAILED     — прочие ошибки (парсинг, shell, …)
        """
        deadline = time.monotonic() + timeout

        # ---- 1. adb devices -l ----------------------------------------
        result = self._run_adb(["devices", "-l"], deadline=deadline)
        if result.returncode != 0:
            raise USBProbeError(
                ErrorCode.PROBE_FAILED,
                f"Команда 'adb devices -l' завершилась с ошибкой: {result.stderr.strip()}",
            )

        entries = _parse_devices(result.stdout)

        # ---- 2. Выбор устройства по device_id -------------------------

        # Ищем записи с точным совпадением serial == device_id
        matched = [e for e in entries if e.serial == device_id]

        if not matched:
            raise USBProbeError(
                ErrorCode.USB_DISCONNECTED,
                f"Устройство {device_id!r} не найдено в выводе 'adb devices'",
            )

        # Если нашли несколько — неоднозначность (не должно быть, но на всякий случай)
        if len(matched) > 1:
            raise USBProbeError(
                ErrorCode.PROBE_FAILED,
                f"Неоднозначный результат: устройство {device_id!r} встретилось {len(matched)} раза",
            )

        entry = matched[0]

        # ---- 3. Проверка транспорта -----------------------------------

        # Убеждаемся, что это USB-устройство, а не сеть/эмулятор
        if not _is_usb(entry):
            raise USBProbeError(
                ErrorCode.PROBE_FAILED,
                f"Устройство {device_id!r} подключено не по USB (serial: {entry.serial!r})",
            )

        # Проверяем состояние
        if entry.state == "unauthorized":
            raise USBProbeError(
                ErrorCode.ADB_UNAUTHORIZED,
                f"Устройство {device_id!r} не авторизовано. "
                "Разрешите отладку на экране телефона.",
            )

        if entry.state == "offline":
            raise USBProbeError(
                ErrorCode.USB_DISCONNECTED,
                f"Устройство {device_id!r} оффлайн (offline).",
            )

        if entry.state != "device":
            raise USBProbeError(
                ErrorCode.USB_DISCONNECTED,
                f"Устройство {device_id!r} в состоянии {entry.state!r}, "
                "ожидалось 'device'.",
            )

        # Нужен корректный transport_id для -t
        if entry.transport_id < 0:
            raise USBProbeError(
                ErrorCode.PROBE_FAILED,
                f"Не удалось определить transport_id для {device_id!r}",
            )

        tid = entry.transport_id

        # ---- 4. Получение версии Android ------------------------------
        ver_result = self._run_adb(
            ["-t", str(tid), "shell", "getprop", "ro.build.version.release"],
            deadline=deadline,
        )
        if ver_result.returncode != 0:
            stderr = ver_result.stderr.strip()
            # Проверяем на unauthorized в ответе shell
            if "unauthorized" in stderr.lower():
                raise USBProbeError(
                    ErrorCode.ADB_UNAUTHORIZED,
                    f"Устройство {device_id!r} отказало в shell: {stderr}",
                )
            raise USBProbeError(
                ErrorCode.PROBE_FAILED,
                f"Не удалось получить ro.build.version.release: {stderr}",
            )

        android_version = ver_result.stdout.strip()
        if not android_version:
            raise USBProbeError(
                ErrorCode.PROBE_FAILED,
                "Получена пустая версия Android от getprop",
            )

        # ---- 5. Получение уровня заряда батареи -----------------------
        bat_result = self._run_adb(
            ["-t", str(tid), "shell", "dumpsys", "battery"],
            deadline=deadline,
        )
        if bat_result.returncode != 0:
            raise USBProbeError(
                ErrorCode.PROBE_FAILED,
                f"Не удалось выполнить 'dumpsys battery': {bat_result.stderr.strip()}",
            )

        battery_level = _parse_battery_level(bat_result.stdout)
        if battery_level is None:
            raise USBProbeError(
                ErrorCode.PROBE_FAILED,
                "Не удалось разобрать уровень заряда батареи из вывода dumpsys battery",
            )

        # ---- 6. Результат ---------------------------------------------
        return ProbeResult(
            serial=entry.serial,
            transport_id=tid,
            state=entry.state,
            android_version=android_version,
            battery_level=battery_level,
        )
