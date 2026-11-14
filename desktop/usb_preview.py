"""Автономный worker для обновления миниатюр экранов Android-устройств по прямому USB.

Модуль предоставляет класс `USBPreviewWorker`, предназначенный для последовательного
снятия снимков экрана с подключенных локальных аппаратных USB-устройств через
команду `adb exec-out screencap -p` без использования Tkinter, сети, ввода или scrcpy.
"""

from __future__ import annotations

from collections.abc import Sequence
import io
import subprocess
import threading
import time
from typing import Any

from PIL import Image

from desktop.link_service import DeviceInfo, is_valid_usb_device

# Максимальный допустимый размер сырого вывода команды (8 МиБ)
MAX_RAW_BYTES: int = 8 * 1024 * 1024

# Максимальное разрешение в пикселях (до 20 Мп)
MAX_IMAGE_PIXELS: int = 20_000_000

# Границы уменьшения миниатюры без апскейла
THUMBNAIL_MAX_WIDTH: int = 132
THUMBNAIL_MAX_HEIGHT: int = 270

# Таймаут вызова подпроцесса ADB (3 секунды)
SUBPROCESS_TIMEOUT: float = 3.0

# Фиксированные аргументы команды снятия экрана
SCREEN_ARGV: tuple[str, ...] = ("exec-out", "screencap", "-p")

# Стандартная сигнатура (магические байты) формата PNG
PNG_SIGNATURE: bytes = b"\x89PNG\r\n\x1a\n"

# Максимальное количество одновременно опрашиваемых телефонов для прототипа
MAX_CONCURRENT_DEVICES: int = 2

# Целевой интервал между обновлениями одного телефона (~2 обновления в секунду)
TARGET_INTERVAL: float = 0.5

# Пауза перед повторной попыткой при сбое
FAILURE_PAUSE: float = 1.0


def _get_valid_positive_transport_id(device: DeviceInfo) -> str | None:
    """Возвращает строковое представление transport_id, если он валиден и положителен (> 0).

    Игнорирует булевы значения, отрицательные числа, 0 и нечисловые строки.
    """
    tid = getattr(device, "transport_id", None)
    if tid is None or isinstance(tid, bool):
        return None
    try:
        val = int(str(tid).strip())
        if val > 0:
            return str(val)
    except (ValueError, TypeError):
        return None
    return None


def _device_connection_key(device: DeviceInfo) -> tuple[str, str | None, str | None]:
    """Возвращает кортеж значимых атрибутов физического/логического USB-соединения.

    Смена незначимых полей (model, product, device_code, state_display, extra)
    не изменяет данный ключ.
    """
    serial = str(getattr(device, "serial", "")).strip()
    usb_info = getattr(device, "usb_info", None)
    usb_info_str = str(usb_info).strip() if usb_info is not None else None
    tid = getattr(device, "transport_id", None)
    tid_str = str(tid).strip() if tid is not None and not isinstance(tid, bool) else None
    return (serial, usb_info_str, tid_str)


class USBPreviewWorker:
    """Фоновый воркер для периодического создания миниатюр экранов USB-телефонов."""

    def __init__(self, adb_path: str = "adb") -> None:
        if not isinstance(adb_path, str) or not adb_path.strip():
            self._adb_path = "adb"
        else:
            self._adb_path = adb_path.strip()

        self._lock = threading.Lock()
        self._wake_event = threading.Event()
        self._frame_event = threading.Event()
        self._closed: bool = False

        self._pending: dict[str, bytes | None] = {}
        self._devices: list[DeviceInfo] = []
        self._active_connections: dict[str, tuple[str, str | None, str | None]] = {}

        self._thread = threading.Thread(
            target=self._worker_loop,
            name="USBPreviewWorker",
            daemon=True,
        )
        self._thread.start()

    def update_devices(self, devices: Sequence[DeviceInfo]) -> None:
        """Принимает свежий снимок устройств от главного потока."""
        with self._lock:
            if self._closed:
                return

            if devices is None or not isinstance(devices, Sequence) or isinstance(devices, (str, bytes)):
                safe_devices: Sequence[DeviceInfo] = []
            else:
                safe_devices = devices

            # Подсчёт встречаемости serial для исключения дубликатов
            serial_counts: dict[str, int] = {}
            for d in safe_devices:
                if d is not None:
                    s = getattr(d, "serial", None)
                    if isinstance(s, str) and s:
                        serial_counts[s] = serial_counts.get(s, 0) + 1

            # Отбор готовых аппаратных USB устройств без дубликатов serial
            valid_devices: list[DeviceInfo] = []
            seen_serials: set[str] = set()
            for d in safe_devices:
                if d is None:
                    continue
                s = getattr(d, "serial", None)
                if not isinstance(s, str) or not s:
                    continue
                if serial_counts.get(s, 0) != 1:
                    continue
                try:
                    if is_valid_usb_device(d):
                        if s not in seen_serials:
                            seen_serials.add(s)
                            valid_devices.append(d)
                except Exception:
                    continue

            # Ограничение прототипа: до 2 одновременно подключённых телефонов
            target_devices = valid_devices[:MAX_CONCURRENT_DEVICES]
            new_connections = {
                d.serial: _device_connection_key(d) for d in target_devices
            }

            # 1. При исчезновении старых устройств забудь кадры
            for serial in list(self._pending.keys()):
                if serial not in new_connections:
                    del self._pending[serial]

            # 2. При смене параметров USB-соединения (reconnect, новый transport_id или порт)
            # инвалидируй устаревшие кадры
            for serial, new_key in new_connections.items():
                old_key = self._active_connections.get(serial)
                if old_key is not None and old_key != new_key:
                    self._pending.pop(serial, None)

            # 3. Смена незначимых полей при том же ключе соединения кадры НЕ инвалидирует

            self._active_connections = new_connections
            self._devices = target_devices
            self._wake_event.set()

    def drain(self) -> dict[str, bytes | None]:
        """Возвращает только новые кадры с прошлого опроса и очищает очередь.

        При сбое возвращает None для соответствующего serial.
        Максимум один кадр на serial, без очереди полноразмерных кадров.
        """
        with self._lock:
            if self._closed:
                return {}
            result = self._pending
            self._pending = {}
            self._frame_event.clear()
            return result

    def close(self) -> None:
        """Неблокирующе ставит отмену и предотвращает запуск новых команд."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._pending.clear()
            self._devices.clear()
            self._active_connections.clear()
            self._wake_event.set()
            self._frame_event.set()

    def _worker_loop(self) -> None:
        """Главный цикл демона: последовательный опрос до 2 телефонов по расписанию."""
        next_allowed_time: dict[str, float] = {}

        while not self._closed:
            with self._lock:
                if self._closed:
                    break
                if not self._devices:
                    # Без процессов когда список пуст
                    self._wake_event.clear()
                    is_empty = True
                else:
                    is_empty = False
                    devices = list(self._devices)
                    active_keys = dict(self._active_connections)

            if is_empty:
                self._wake_event.wait()
                self._wake_event.clear()
                continue

            now = time.monotonic()

            # Удаляем тайминги для отключившихся устройств
            for s in list(next_allowed_time.keys()):
                if s not in active_keys:
                    del next_allowed_time[s]

            # Ищем устройство, готовое к снятию кадра прямо сейчас
            ready_device: DeviceInfo | None = None
            min_wait: float | None = None
            for dev in devices:
                allowed = next_allowed_time.get(dev.serial, 0.0)
                if allowed <= now:
                    ready_device = dev
                    break
                wait_for = allowed - now
                if min_wait is None or wait_for < min_wait:
                    min_wait = wait_for

            if ready_device is None:
                # Ни одно устройство не готово прямо сейчас - ожидаем до времени готовности
                if min_wait is not None and min_wait > 0:
                    if self._wake_event.wait(min_wait):
                        self._wake_event.clear()
                continue

            serial = ready_device.serial
            conn_key = active_keys.get(serial)

            t_start = time.monotonic()

            # Проверка отмены перед выполнением
            if self._closed:
                break

            processed_frame, success = self._capture_device(ready_device)

            now = time.monotonic()
            if success:
                # Ориентир около 2 обновлений в секунду на телефон когда ADB успевает
                next_allowed_time[serial] = max(now, t_start + TARGET_INTERVAL)
            else:
                # Пауза при сбоях
                next_allowed_time[serial] = now + FAILURE_PAUSE

            # Сохранение результата под блокировкой
            with self._lock:
                if self._closed:
                    break
                # Сохраняем только если устройство всё ещё активно и имеет то же USB-соединение
                if self._active_connections.get(serial) == conn_key:
                    self._pending[serial] = processed_frame
                    self._frame_event.set()

    def _capture_device(self, device: DeviceInfo) -> tuple[bytes | None, bool]:
        """Выполняет вызов ADB screencap и преобразование через Pillow."""
        if self._closed:
            return None, False

        tid_str = _get_valid_positive_transport_id(device)
        if tid_str is not None:
            target_args = ["-t", tid_str]
        else:
            target_args = ["-s", device.serial]

        cmd = [self._adb_path, *target_args, *SCREEN_ARGV]

        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=SUBPROCESS_TIMEOUT,
                shell=False,
                check=False,
            )
        except Exception:
            return None, False

        if proc.returncode != 0:
            return None, False

        raw_stdout = proc.stdout
        if isinstance(raw_stdout, (bytes, bytearray)):
            raw_bytes = bytes(raw_stdout)
        elif isinstance(raw_stdout, str):
            raw_bytes = raw_stdout.encode("latin1", errors="replace")
        else:
            return None, False

        thumbnail = self._process_image(raw_bytes)
        if thumbnail is None:
            return None, False

        return thumbnail, True

    def _process_image(self, raw_bytes: bytes) -> bytes | None:
        """Проверяет PNG и ограничения размера через Pillow, уменьшает без upscale в границы 132x270."""
        if not raw_bytes:
            return None

        # Проверка лимита размера сырых данных (< 8 MiB)
        if len(raw_bytes) >= MAX_RAW_BYTES:
            return None

        # Быстрая проверка сигнатуры PNG
        if not raw_bytes.startswith(PNG_SIGNATURE):
            return None

        try:
            with Image.open(io.BytesIO(raw_bytes)) as img:
                if img.format != "PNG":
                    return None

                w, h = img.size
                if w <= 0 or h <= 0:
                    return None

                # Лимит разрешения (до 20 Мп)
                if (w * h) > MAX_IMAGE_PIXELS:
                    return None

                # Уменьшай без upscale по пропорциям в границы 132x270
                if w > THUMBNAIL_MAX_WIDTH or h > THUMBNAIL_MAX_HEIGHT:
                    img.thumbnail((THUMBNAIL_MAX_WIDTH, THUMBNAIL_MAX_HEIGHT))

                out = io.BytesIO()
                img.save(out, format="PNG")
                return out.getvalue()
        except Exception:
            return None


__all__ = [
    "USBPreviewWorker",
    "MAX_RAW_BYTES",
    "MAX_IMAGE_PIXELS",
    "THUMBNAIL_MAX_WIDTH",
    "THUMBNAIL_MAX_HEIGHT",
    "SUBPROCESS_TIMEOUT",
    "SCREEN_ARGV",
    "PNG_SIGNATURE",
    "MAX_CONCURRENT_DEVICES",
    "TARGET_INTERVAL",
    "FAILURE_PAUSE",
]
