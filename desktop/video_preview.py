"""Изолированный background worker для H264 видео-превью устройств через PyAV и Pillow.

Модуль предоставляет класс `VideoPreviewWorker`, который принимает поток H264 Annex B
пакетов (через абстракцию `packet_source`), декодирует их с помощью PyAV с минимальной
задержкой (один поток декодирования на кодек, без задержки межпоточного декодирования
FFmpeg), преобразует кадры в пропорциональные миниатюры 132x270 PNG через Pillow и
ограничивает частоту обновления отображения (~12 fps) без отбрасывания закодированных пакетов.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
import io
import threading
import time
from typing import Any

from PIL import Image

from desktop.adb_client import DeviceInfo
from desktop.link_service import is_valid_usb_device
from desktop.usb_preview import _device_connection_key, _get_valid_positive_transport_id

av: Any = None

# Границы уменьшения миниатюры без апскейла
THUMBNAIL_MAX_WIDTH: int = 132
THUMBNAIL_MAX_HEIGHT: int = 270

# Максимальное разрешение кадра в пикселях (до 20 Мп)
MAX_IMAGE_PIXELS: int = 20_000_000

# Максимальное количество одновременно опрашиваемых устройств
MAX_CONCURRENT_DEVICES: int = 2

# Ограничение частоты отображения (~12 кадров в секунду)
DISPLAY_CAP_FPS: float = 12.0
MIN_DISPLAY_INTERVAL: float = 1.0 / DISPLAY_CAP_FPS

# Параметры экспоненциального бэкоффа при сбоях источника
INITIAL_BACKOFF: float = 0.5
MAX_BACKOFF: float = 8.0
BACKOFF_FACTOR: float = 2.0

# Стандартная сигнатура (магические байты) формата PNG
PNG_SIGNATURE: bytes = b"\x89PNG\r\n\x1a\n"


def _close_resource(res: Any) -> None:
    """Безопасно вызывает метод close(), если он существует."""
    if res is not None:
        close_fn = getattr(res, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass


@dataclass
class _DeviceStream:
    """Контекст активного потока декодирования для одного устройства."""

    conn_key: tuple[str, str | None, str | None]
    stop_event: threading.Event
    thread: threading.Thread


class VideoPreviewWorker:
    """Фоновый воркер для декодирования H264 видеопотока устройств в Tk-миниатюры."""

    def __init__(
        self,
        packet_source: Callable[[DeviceInfo, threading.Event], Iterator[bytes]],
        *,
        av_module: Any = None,
        display_cap_fps: float = DISPLAY_CAP_FPS,
        initial_backoff: float = INITIAL_BACKOFF,
        max_backoff: float = MAX_BACKOFF,
        backoff_factor: float = BACKOFF_FACTOR,
        max_devices: int = MAX_CONCURRENT_DEVICES,
    ) -> None:
        self._packet_source = packet_source
        self._av_module = av_module
        self._display_cap_fps = display_cap_fps
        self._min_display_interval = 1.0 / display_cap_fps if display_cap_fps > 0 else 0.0
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._backoff_factor = backoff_factor
        self._max_devices = max_devices

        self._lock = threading.Lock()
        self._frame_event = threading.Event()
        self._closed: bool = False

        self._pending: dict[str, bytes | None] = {}
        self._status: dict[str, str] = {}
        self._streams: dict[str, _DeviceStream] = {}
        self._devices: dict[str, DeviceInfo] = {}

    def _get_av(self) -> Any:
        """Возвращает модуль av, если он доступен или инъецирован."""
        if self._av_module is not None:
            return self._av_module
        global av
        if av is not None:
            return av
        try:
            import av as _av

            av = _av
            return av
        except Exception:
            return None

    def _report_failure(self, serial: str, stop_event: threading.Event) -> None:
        """Фиксирует None при переходе в состояние сбоя."""
        with self._lock:
            if self._closed or stop_event.is_set():
                return
            prev_status = self._status.get(serial)
            if prev_status != "failed":
                self._status[serial] = "failed"
                self._pending[serial] = None
                self._frame_event.set()

    def _publish_frame(
        self, serial: str, stop_event: threading.Event, png_bytes: bytes
    ) -> None:
        """Публикует новый декодированный кадр."""
        with self._lock:
            if self._closed or stop_event.is_set():
                return
            self._status[serial] = "ok"
            self._pending[serial] = png_bytes
            self._frame_event.set()

    def _process_frame(self, frame: Any) -> bytes | None:
        """Преобразует декодированный кадр в миниатюру 132x270 PNG с сохранением пропорций."""
        try:
            if hasattr(frame, "to_image") and callable(frame.to_image):
                img = frame.to_image()
            elif isinstance(frame, Image.Image):
                img = frame
            else:
                return None

            if img is None:
                return None

            w, h = img.size
            if w <= 0 or h <= 0 or (w * h) > MAX_IMAGE_PIXELS:
                return None

            if w > THUMBNAIL_MAX_WIDTH or h > THUMBNAIL_MAX_HEIGHT:
                img.thumbnail((THUMBNAIL_MAX_WIDTH, THUMBNAIL_MAX_HEIGHT))

            out = io.BytesIO()
            img.save(out, format="PNG")
            return out.getvalue()
        except Exception:
            return None

    def _create_and_start_stream(
        self, device: DeviceInfo, conn_key: tuple[str, str | None, str | None]
    ) -> _DeviceStream:
        """Создает и запускает отдельный поток декодирования для устройства."""
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._run_and_retire,
            args=(device, stop_event),
            name=f"VideoPreview-{device.serial}",
            daemon=False,
        )
        stream = _DeviceStream(
            conn_key=conn_key,
            stop_event=stop_event,
            thread=thread,
        )
        thread.start()
        return stream

    def _run_and_retire(
        self,
        device: DeviceInfo,
        stop_event: threading.Event,
    ) -> None:
        try:
            self._run_stream(device, stop_event)
        finally:
            with self._lock:
                self._streams.pop(device.serial, None)
                if not self._closed:
                    self._start_available_streams()

    def _start_available_streams(self) -> None:
        for serial, device in self._devices.items():
            if len(self._streams) >= self._max_devices:
                break
            if serial not in self._streams:
                conn_key = _device_connection_key(device)
                self._streams[serial] = self._create_and_start_stream(device, conn_key)

    def _run_stream(
        self,
        device: DeviceInfo,
        stop_event: threading.Event,
    ) -> None:
        """Цикл декодирования одного устройства в отдельном потоке."""
        serial = device.serial
        backoff = self._initial_backoff

        while not stop_event.is_set() and not self._closed:
            av_mod = self._get_av()
            if av_mod is None:
                self._report_failure(serial, stop_event)
                if stop_event.wait(backoff):
                    break
                backoff = min(backoff * self._backoff_factor, self._max_backoff)
                continue

            codec_ctx = None
            stream_iter = None
            try:
                try:
                    codec_ctx = av_mod.CodecContext.create("h264", "r")
                    codec_ctx.thread_count = 1
                    try:
                        codec_ctx.thread_type = "NONE"
                    except Exception:
                        pass

                    stream_iter = self._packet_source(device, stop_event)
                    if stream_iter is None:
                        raise RuntimeError("packet_source returned None")

                    last_display_time: float = 0.0

                    for payload in stream_iter:
                        if stop_event.is_set() or self._closed:
                            break
                        if not payload or not isinstance(payload, (bytes, bytearray)):
                            continue

                        packet = av_mod.Packet(bytes(payload))
                        frames = codec_ctx.decode(packet)
                        if frames is not None:
                            if not isinstance(frames, (list, tuple)) and not hasattr(frames, "__iter__"):
                                frames = [frames]
                            for frame in frames:
                                now = time.monotonic()
                                if (now - last_display_time) >= self._min_display_interval:
                                    png_bytes = self._process_frame(frame)
                                    if png_bytes is not None:
                                        last_display_time = now
                                        self._publish_frame(serial, stop_event, png_bytes)
                                        backoff = self._initial_backoff

                    if not stop_event.is_set() and not self._closed:
                        raise RuntimeError("Stream ended unexpectedly")
                finally:
                    _close_resource(stream_iter)
                    _close_resource(codec_ctx)
            except Exception:
                if not stop_event.is_set() and not self._closed:
                    self._report_failure(serial, stop_event)
                    if stop_event.wait(backoff):
                        break
                    backoff = min(backoff * self._backoff_factor, self._max_backoff)

    def update_devices(self, devices: Sequence[DeviceInfo]) -> None:
        """Обновляет список активных локальных USB-устройств."""
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

            target_devices = valid_devices[: self._max_devices]
            new_connections = {d.serial: _device_connection_key(d) for d in target_devices}

            for serial, stream in self._streams.items():
                if new_connections.get(serial) != stream.conn_key:
                    stream.stop_event.set()
                    self._status.pop(serial, None)
                    self._pending.pop(serial, None)

            self._devices = {device.serial: device for device in target_devices}
            self._start_available_streams()

    def drain(self) -> dict[str, bytes | None]:
        """Возвращает только новые кадры с прошлого опроса и очищает очередь.

        При сбое возвращает None для соответствующего serial.
        """
        with self._lock:
            if self._closed:
                return {}
            result = self._pending
            self._pending = {}
            self._frame_event.clear()
            return result

    def has_streams_for(self, serials: set[str]) -> bool:
        """Учитывает также потоки, которые освобождают USB-ресурсы."""
        with self._lock:
            return not self._streams.keys().isdisjoint(serials)

    def close(self) -> None:
        """Неблокирующе останавливает все потоки декодирования и очищает состояние."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._pending.clear()
            self._status.clear()
            for stream in self._streams.values():
                stream.stop_event.set()
            self._streams.clear()
            self._devices.clear()
            self._frame_event.set()


__all__ = [
    "VideoPreviewWorker",
    "THUMBNAIL_MAX_WIDTH",
    "THUMBNAIL_MAX_HEIGHT",
    "MAX_IMAGE_PIXELS",
    "MAX_CONCURRENT_DEVICES",
    "DISPLAY_CAP_FPS",
    "MIN_DISPLAY_INTERVAL",
    "INITIAL_BACKOFF",
    "MAX_BACKOFF",
    "BACKOFF_FACTOR",
    "PNG_SIGNATURE",
    "_get_valid_positive_transport_id",
    "_device_connection_key",
]
