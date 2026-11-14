"""Тесты для изолированного worker обновления миниатюр H264 видеопотока (desktop/video_preview.py).

Проверяют:
1. Готовность (readiness): отбор только аппаратных готовых USB-устройств (is_valid_usb_device),
   игнорирование offline/unauthorized, сетевых устройств и отсутствия usb_info.
2. Дубликаты и лимит: исключение неоднозначных serial, ограничение до 2 устройств одновременно.
3. Кодек PyAV и единый поток: создание av.CodecContext.create('h264', 'r'), thread_count=1,
   thread_type='NONE' (отсутствие задержки frame threading), вызов av.Packet(payload) и decode().
4. Уменьшение и Pillow: пропорциональное уменьшение в границы 132x270 без апскейла,
   проверка лимита пикселей, возврат валидного PNG.
5. Ограничение частоты отображения (display cap): ограничение до ~12fps без отбрасывания
   закодированных пакетов (все пакеты передаются в decode).
6. Опциональный / ленивый импорт av: при отсутствии av откат и фиксация None в drain().
7. Обработка сбоев и бэкофф: фиксация None при переходе в сбой, бэкофф с прерыванием по stop,
   обязательное закрытие итератора при ошибке декодирования, восстановление после сбоя.
8. Идентификация подключения и hotplug: сохранение потока при смене незначимых полей,
   инвалидация старых кадров и перезапуск при смене transport_id/usb_info, предотвращение
   устаревших кадров.
9. Drain и Close: drain() возвращает только новейший кадр и очищает очередь, close() неблокирующий,
   без дублирования активных потоков для одного serial.
"""

from __future__ import annotations

from collections.abc import Iterator
import io
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

from PIL import Image
import pytest

from desktop.adb_client import DeviceInfo
from desktop.video_preview import (
    DISPLAY_CAP_FPS,
    MAX_CONCURRENT_DEVICES,
    MAX_IMAGE_PIXELS,
    MIN_DISPLAY_INTERVAL,
    PNG_SIGNATURE,
    THUMBNAIL_MAX_HEIGHT,
    THUMBNAIL_MAX_WIDTH,
    VideoPreviewWorker,
    _device_connection_key,
    _get_valid_positive_transport_id,
)


def make_test_device(
    serial: str = "USB_SERIAL_1",
    state: str = "device",
    usb_info: str | None = "1-1.2",
    is_network: bool = False,
    is_ready: bool | None = None,
    model: str | None = "Pixel 8a",
    transport_id: str | int | None = None,
) -> DeviceInfo:
    """Создает валидный объект DeviceInfo для тестирования."""
    norm_state = state.lower().strip()
    ready = (norm_state == "device") if is_ready is None else is_ready
    conn_type = "Сетевое (ADB TCP/IP)" if is_network else "USB"
    return DeviceInfo(
        serial=serial,
        state=state,
        state_display="Готов к работе" if ready else f"Статус: {state}",
        is_network=is_network,
        connection_type=conn_type,
        is_ready=ready,
        model=model,
        transport_id=str(transport_id) if transport_id is not None else None,
        usb_info=usb_info,
    )


class MockPacket:
    """Имитация av.Packet."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload


class MockCodecContext:
    """Имитация av.CodecContext с поддержкой инъекции поведения decode."""

    def __init__(
        self,
        codec_name: str = "h264",
        mode: str = "r",
        default_frame: Any = None,
        decode_side_effect: Exception | None = None,
    ) -> None:
        self.codec_name = codec_name
        self.mode = mode
        self.thread_count: int | None = None
        self.thread_type: str | None = None
        self.closed: bool = False
        self.decode_calls: list[Any] = []
        self.decode_side_effect: Exception | None = decode_side_effect
        self.default_frame: Any = default_frame
        self.custom_frames: list[Any] | None = None

    def decode(self, packet: Any) -> list[Any]:
        self.decode_calls.append(packet)
        if self.decode_side_effect is not None:
            raise self.decode_side_effect
        if self.custom_frames is not None:
            return self.custom_frames
        if self.default_frame is not None:
            return [self.default_frame]
        mock_frame = MagicMock()
        mock_frame.to_image.return_value = Image.new("RGB", (100, 100), color="blue")
        return [mock_frame]

    def close(self) -> None:
        self.closed = True


class MockAvModule:
    """Имитация модуля av для тестирования без внешней зависимости."""

    def __init__(
        self,
        default_frame: Any = None,
        decode_side_effect: Exception | None = None,
    ) -> None:
        self.contexts: list[MockCodecContext] = []
        self.default_frame = default_frame
        self.decode_side_effect = decode_side_effect
        self.CodecContext = MagicMock()
        self.CodecContext.create.side_effect = self._create_codec_ctx
        self.Packet = MagicMock(side_effect=lambda payload: MockPacket(payload))

    def _create_codec_ctx(self, name: str, mode: str) -> MockCodecContext:
        ctx = MockCodecContext(
            name,
            mode,
            default_frame=self.default_frame,
            decode_side_effect=self.decode_side_effect,
        )
        self.contexts.append(ctx)
        return ctx


def wait_for_drain(worker: VideoPreviewWorker, timeout: float = 1.0) -> dict[str, bytes | None]:
    """Ожидает события появления кадра или ошибки и возвращает результат drain()."""
    worker._frame_event.wait(timeout)
    return worker.drain()


# ===========================================================================
# 1. Тесты готовности устройств (Readiness)
# ===========================================================================


class TestDeviceReadiness:
    """Проверка фильтрации устройств по правилам готовности аппаратного USB."""

    def test_valid_usb_device_is_processed(self) -> None:
        """Готовое аппаратное USB-устройство успешно обрабатывается."""
        dev = make_test_device("DEV_OK", state="device", usb_info="1-1.1", is_ready=True)
        mock_av = MockAvModule()

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            yield b"\x00\x00\x00\x01\x67fake_sps"
            while not stop.is_set():
                stop.wait(0.01)

        worker = VideoPreviewWorker(source, av_module=mock_av)
        try:
            worker.update_devices([dev])
            res = wait_for_drain(worker)
            assert "DEV_OK" in res
            assert res["DEV_OK"] is not None
            assert res["DEV_OK"].startswith(PNG_SIGNATURE)
        finally:
            worker.close()

    def test_unauthorized_device_ignored(self) -> None:
        """Устройство со статусом unauthorized не обрабатывается."""
        dev = make_test_device("DEV_UNAUTH", state="unauthorized", is_ready=False)
        called = False

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            nonlocal called
            called = True
            yield b"packet"

        worker = VideoPreviewWorker(source, av_module=MockAvModule())
        try:
            worker.update_devices([dev])
            time.sleep(0.05)
            assert not called
            assert worker.drain() == {}
        finally:
            worker.close()

    def test_offline_device_ignored(self) -> None:
        """Устройство со статусом offline не обрабатывается."""
        dev = make_test_device("DEV_OFFLINE", state="offline", is_ready=False)
        called = False

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            nonlocal called
            called = True
            yield b"packet"

        worker = VideoPreviewWorker(source, av_module=MockAvModule())
        try:
            worker.update_devices([dev])
            time.sleep(0.05)
            assert not called
            assert worker.drain() == {}
        finally:
            worker.close()

    def test_network_device_ignored(self) -> None:
        """Сетевое ADB-устройство (is_network=True) не обрабатывается."""
        dev = make_test_device("DEV_NET", is_network=True, usb_info=None)
        called = False

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            nonlocal called
            called = True
            yield b"packet"

        worker = VideoPreviewWorker(source, av_module=MockAvModule())
        try:
            worker.update_devices([dev])
            time.sleep(0.05)
            assert not called
            assert worker.drain() == {}
        finally:
            worker.close()

    def test_network_serial_ignored(self) -> None:
        """Устройство с сетевым serial (IP:порт) не обрабатывается."""
        dev = make_test_device("192.168.1.50:5555", is_network=False, usb_info="1-1")
        called = False

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            nonlocal called
            called = True
            yield b"packet"

        worker = VideoPreviewWorker(source, av_module=MockAvModule())
        try:
            worker.update_devices([dev])
            time.sleep(0.05)
            assert not called
            assert worker.drain() == {}
        finally:
            worker.close()

    def test_missing_usb_info_ignored(self) -> None:
        """Устройство без usb_info не обрабатывается."""
        dev = make_test_device("DEV_NO_USB", usb_info=None)
        called = False

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            nonlocal called
            called = True
            yield b"packet"

        worker = VideoPreviewWorker(source, av_module=MockAvModule())
        try:
            worker.update_devices([dev])
            time.sleep(0.05)
            assert not called
            assert worker.drain() == {}
        finally:
            worker.close()

    def test_empty_devices_list_starts_no_streams(self) -> None:
        """При пустом списке устройств потоки не запускаются."""
        called = False

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            nonlocal called
            called = True
            yield b"packet"

        worker = VideoPreviewWorker(source, av_module=MockAvModule())
        try:
            worker.update_devices([])
            time.sleep(0.05)
            assert not called
            assert worker.drain() == {}
        finally:
            worker.close()


# ===========================================================================
# 2. Тесты обработки дубликатов и ограничения устройств
# ===========================================================================


class TestDuplicatesAndLimits:
    """Проверка исключения дубликатов и ограничения числа устройств (до 2)."""

    def test_duplicate_serial_both_rejected(self) -> None:
        """Дубликат serial в списке устройств исключает оба устройства."""
        dev1 = make_test_device("DUP_DEV", usb_info="1-1")
        dev2 = make_test_device("DUP_DEV", usb_info="1-2")
        called_serials: list[str] = []

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            called_serials.append(d.serial)
            yield b"packet"

        worker = VideoPreviewWorker(source, av_module=MockAvModule())
        try:
            worker.update_devices([dev1, dev2])
            time.sleep(0.05)
            assert called_serials == []
            assert worker.drain() == {}
        finally:
            worker.close()

    def test_max_concurrent_devices_limit(self) -> None:
        """Обрабатываются максимум 2 локальных USB-устройства одновременно."""
        dev1 = make_test_device("DEV_1", usb_info="1-1")
        dev2 = make_test_device("DEV_2", usb_info="1-2")
        dev3 = make_test_device("DEV_3", usb_info="1-3")
        active_serials: list[str] = []
        lock = threading.Lock()

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            with lock:
                active_serials.append(d.serial)
            yield b"packet"
            while not stop.is_set():
                stop.wait(0.01)

        worker = VideoPreviewWorker(source, av_module=MockAvModule())
        try:
            worker.update_devices([dev1, dev2, dev3])
            time.sleep(0.1)
            with lock:
                assert len(active_serials) == 2
                assert set(active_serials) == {"DEV_1", "DEV_2"}
                assert "DEV_3" not in active_serials
        finally:
            worker.close()

    def test_concurrent_two_devices_independent(self) -> None:
        """Два устройства декодируются независимо."""
        dev1 = make_test_device("DEV_A", usb_info="1-1")
        dev2 = make_test_device("DEV_B", usb_info="1-2")

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            yield f"packet_{d.serial}".encode()
            while not stop.is_set():
                stop.wait(0.01)

        worker = VideoPreviewWorker(source, av_module=MockAvModule())
        try:
            worker.update_devices([dev1, dev2])
            time.sleep(0.1)
            res = worker.drain()
            assert "DEV_A" in res and res["DEV_A"] is not None
            assert "DEV_B" in res and res["DEV_B"] is not None
        finally:
            worker.close()


# ===========================================================================
# 3. Тесты создания кодека PyAV и однопоточного декодирования
# ===========================================================================


class TestDecoderCreationAndSingleThread:
    """Проверка правильной инициализации av.CodecContext и потоков."""

    def test_codec_context_initialization(self) -> None:
        """Создается кодек h264 r с single decode thread (thread_count=1, thread_type=NONE)."""
        dev = make_test_device("DEV_CODEC")
        mock_av = MockAvModule()

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            yield b"h264_payload"
            while not stop.is_set():
                stop.wait(0.01)

        worker = VideoPreviewWorker(source, av_module=mock_av)
        try:
            worker.update_devices([dev])
            res = wait_for_drain(worker)
            assert "DEV_CODEC" in res

            # Проверяем вызовы создания кодека
            mock_av.CodecContext.create.assert_called_with("h264", "r")
            assert len(mock_av.contexts) == 1
            ctx = mock_av.contexts[0]
            assert ctx.thread_count == 1
            assert ctx.thread_type == "NONE"

            # Проверяем вызовы av.Packet
            mock_av.Packet.assert_called_with(b"h264_payload")
            assert len(ctx.decode_calls) == 1
        finally:
            worker.close()

    def test_worker_thread_is_daemon_and_named(self) -> None:
        """Поток декодирования является daemon и имеет имя VideoPreview-<serial>."""
        dev = make_test_device("DEV_THREAD")
        mock_av = MockAvModule()

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            yield b"packet"
            while not stop.is_set():
                stop.wait(0.01)

        worker = VideoPreviewWorker(source, av_module=mock_av)
        try:
            worker.update_devices([dev])
            wait_for_drain(worker)
            stream = worker._streams.get("DEV_THREAD")
            assert stream is not None
            assert stream.thread.is_alive()
            assert stream.thread.daemon is True
            assert stream.thread.name == "VideoPreview-DEV_THREAD"
        finally:
            worker.close()


# ===========================================================================
# 4. Тесты уменьшения и Pillow
# ===========================================================================


class TestDownscaleAndPillow:
    """Проверка пропорционального уменьшения в 132x270 и обработки Pillow."""

    def test_downscale_large_portrait_image(self) -> None:
        """Полноразмерный скриншот 1080x2400 уменьшается без искажения в границы 132x270."""
        dev = make_test_device("DEV_P1")
        mock_frame = MagicMock()
        mock_frame.to_image.return_value = Image.new("RGB", (1080, 2400), color="red")
        mock_av = MockAvModule(default_frame=mock_frame)

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            yield b"payload"
            while not stop.is_set():
                stop.wait(0.01)

        worker = VideoPreviewWorker(source, av_module=mock_av)
        try:
            worker.update_devices([dev])
            res = wait_for_drain(worker)
            frame_bytes = res.get("DEV_P1")
            assert frame_bytes is not None
            with Image.open(io.BytesIO(frame_bytes)) as img:
                assert img.format == "PNG"
                w, h = img.size
                assert w <= 132
                assert h <= 270
                # 1080 / 2400 = 0.45; 270 * 0.45 = 121.5 -> 122
                assert (w, h) == (122, 270)
        finally:
            worker.close()

    def test_downscale_landscape_image(self) -> None:
        """Альбомное изображение 200x100 уменьшается пропорционально в 132x66."""
        dev = make_test_device("DEV_LAND")
        mock_frame = MagicMock()
        mock_frame.to_image.return_value = Image.new("RGB", (200, 100), color="green")
        mock_av = MockAvModule(default_frame=mock_frame)

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            yield b"payload"
            while not stop.is_set():
                stop.wait(0.01)

        worker = VideoPreviewWorker(source, av_module=mock_av)
        try:
            worker.update_devices([dev])
            res = wait_for_drain(worker)
            frame_bytes = res.get("DEV_LAND")
            assert frame_bytes is not None
            with Image.open(io.BytesIO(frame_bytes)) as img:
                assert img.format == "PNG"
                assert img.size == (132, 66)
        finally:
            worker.close()

    def test_no_upscale_for_small_image(self) -> None:
        """Маленькое изображение (50x50) не увеличивается (no upscale)."""
        dev = make_test_device("DEV_SMALL")
        mock_frame = MagicMock()
        mock_frame.to_image.return_value = Image.new("RGB", (50, 50), color="yellow")
        mock_av = MockAvModule(default_frame=mock_frame)

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            yield b"payload"
            while not stop.is_set():
                stop.wait(0.01)

        worker = VideoPreviewWorker(source, av_module=mock_av)
        try:
            worker.update_devices([dev])
            res = wait_for_drain(worker)
            frame_bytes = res.get("DEV_SMALL")
            assert frame_bytes is not None
            with Image.open(io.BytesIO(frame_bytes)) as img:
                assert img.format == "PNG"
                assert img.size == (50, 50)
        finally:
            worker.close()

    def test_excessive_pixel_count_rejected(self) -> None:
        """Изображение, превышающее MAX_IMAGE_PIXELS, игнорируется."""
        dev = make_test_device("DEV_HUGE")
        # 5000 x 5000 = 25 000 000 > 20 000 000
        mock_frame = MagicMock()
        mock_frame.to_image.return_value = Image.new("RGB", (5000, 5000), color="white")
        mock_av = MockAvModule(default_frame=mock_frame)

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            yield b"payload"
            while not stop.is_set():
                stop.wait(0.01)

        worker = VideoPreviewWorker(source, av_module=mock_av)
        try:
            worker.update_devices([dev])
            time.sleep(0.05)
            assert worker.drain() == {}
        finally:
            worker.close()


# ===========================================================================
# 5. Тест ограничения частоты отображения (Display cap ~12fps)
# ===========================================================================


class TestDisplayCap:
    """Проверка ограничения частоты отображения без отбрасывания закодированных пакетов."""

    def test_all_packets_decoded_and_display_rate_capped(self) -> None:
        """Все H264 пакеты передаются в decode, но отображение ограничено ~12fps."""
        dev = make_test_device("DEV_FPS")
        mock_av = MockAvModule()

        total_packets = 24
        packets_data = [f"nalu_{i}".encode() for i in range(total_packets)]

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            for p in packets_data:
                yield p
            while not stop.is_set():
                stop.wait(0.01)

        worker = VideoPreviewWorker(source, av_module=mock_av, display_cap_fps=12.0)
        try:
            worker.update_devices([dev])
            time.sleep(0.1)

            # Проверяем, что ВСЕ пакеты декодированы декодером (ни один не отброшен)
            assert len(mock_av.contexts) == 1
            ctx = mock_av.contexts[0]
            assert len(ctx.decode_calls) == total_packets

            # Проверяем, что кадр доступен в drain
            res = worker.drain()
            assert "DEV_FPS" in res
            assert res["DEV_FPS"] is not None
        finally:
            worker.close()


# ===========================================================================
# 6. Тест опционального/ленивого импорта av
# ===========================================================================


class TestLazyOptionalAv:
    """Проверка поведения при недоступности модуля av."""

    def test_missing_av_reports_none_failure(self) -> None:
        """Если av недоступен, drain() возвращает None для устройства."""
        dev = make_test_device("DEV_NO_AV")

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            yield b"payload"

        # Создаем worker без инъекции av и с блокировкой системного import av
        with patch.object(VideoPreviewWorker, "_get_av", return_value=None):
            worker = VideoPreviewWorker(source)
            try:
                worker.update_devices([dev])
                res = wait_for_drain(worker)
                assert "DEV_NO_AV" in res
                assert res["DEV_NO_AV"] is None
            finally:
                worker.close()


# ===========================================================================
# 7. Тесты обработки ошибок и bounded backoff
# ===========================================================================


class TestFailureHandlingAndRetry:
    """Проверка повторов при сбоях источника, бэкоффа и закрытия итератора."""

    def test_packet_source_exception_reports_none_and_retries(self) -> None:
        """Исключение в источнике пакетов переводит в сбой (None) и выполняет retry."""
        dev = make_test_device("DEV_FAIL")
        mock_av = MockAvModule()
        attempts = 0
        lock = threading.Lock()

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            nonlocal attempts
            with lock:
                attempts += 1
                curr = attempts
            if curr == 1:
                raise RuntimeError("ADB transport connection lost")
            yield b"recovered_payload"
            while not stop.is_set():
                stop.wait(0.01)

        worker = VideoPreviewWorker(
            source,
            av_module=mock_av,
            initial_backoff=0.02,
            max_backoff=0.05,
        )
        try:
            worker.update_devices([dev])

            # 1. Первая попытка упала -> переход в сбой, reports None
            res1 = wait_for_drain(worker, timeout=1.0)
            assert "DEV_FAIL" in res1
            assert res1["DEV_FAIL"] is None

            # 2. Worker делает retry после backoff -> успешное восстановление
            res2 = wait_for_drain(worker, timeout=1.0)
            assert "DEV_FAIL" in res2
            assert res2["DEV_FAIL"] is not None
            assert res2["DEV_FAIL"].startswith(PNG_SIGNATURE)
        finally:
            worker.close()

    def test_iterator_closed_on_decode_failure(self) -> None:
        """Итератор обязательно закрывается при ошибке декодирования."""
        dev = make_test_device("DEV_DECODE_ERR")
        mock_av = MockAvModule(decode_side_effect=RuntimeError("H264 corrupt frame error"))
        closed_event = threading.Event()

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            try:
                yield b"corrupted_nalu"
                while not stop.is_set():
                    stop.wait(0.01)
            finally:
                closed_event.set()

        worker = VideoPreviewWorker(source, av_module=mock_av, initial_backoff=0.1)
        try:
            worker.update_devices([dev])

            # Ждем фиксации ошибки в drain
            res = wait_for_drain(worker, timeout=1.0)
            assert "DEV_DECODE_ERR" in res
            assert res["DEV_DECODE_ERR"] is None

            # Проверяем, что генератор закрылся (вызвался finally)
            assert closed_event.wait(timeout=1.0)
        finally:
            worker.close()

    def test_backoff_interruptible_by_stop(self) -> None:
        """Бэкофф не блокирует завершение потока при выставлении stop."""
        dev = make_test_device("DEV_STOP")
        mock_av = MockAvModule()

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            raise RuntimeError("Fatal source error")

        # Большой бэкофф (5 секунд)
        worker = VideoPreviewWorker(source, av_module=mock_av, initial_backoff=5.0)
        try:
            worker.update_devices([dev])
            wait_for_drain(worker, timeout=1.0)
            stream = worker._streams.get("DEV_STOP")
            assert stream is not None
            assert stream.thread.is_alive()

            # close() должен завершить поток немедленно, не дожидаясь 5 секунд
            t0 = time.monotonic()
            worker.close()
            stream.thread.join(timeout=1.0)
            t_elapsed = time.monotonic() - t0
            assert t_elapsed < 1.0
            assert not stream.thread.is_alive()
        finally:
            worker.close()

    def test_unexpected_stream_termination_retries(self) -> None:
        """Неожиданное завершение итератора без stop приводит к сбою и retry."""
        dev = make_test_device("DEV_EOF")
        mock_av = MockAvModule()
        runs = 0

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            nonlocal runs
            runs += 1
            yield b"one_packet"
            # итератор просто заканчивается

        worker = VideoPreviewWorker(
            source, av_module=mock_av, initial_backoff=0.02, max_backoff=0.05
        )
        try:
            worker.update_devices([dev])
            # Получаем первый кадр
            res1 = wait_for_drain(worker, timeout=1.0)
            assert "DEV_EOF" in res1
            # После завершения стрима воркер падает в ошибку
            res2 = wait_for_drain(worker, timeout=1.0)
            assert "DEV_EOF" in res2
            assert runs >= 2
        finally:
            worker.close()

    def test_consecutive_retry_failures_report_none_only_once_on_transition(self) -> None:
        """При нескольких сбоях ретрая подряд drain() возвращает None только 1 раз при переходе."""
        dev = make_test_device("DEV_FAIL_MULTI")
        mock_av = MockAvModule()
        attempts = 0
        lock = threading.Lock()
        should_succeed = False

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            nonlocal attempts
            with lock:
                attempts += 1
                succeed = should_succeed
            if not succeed:
                raise RuntimeError("Persistent source error")
            yield b"recovered_frame"
            while not stop.is_set():
                stop.wait(0.01)

        worker = VideoPreviewWorker(
            source,
            av_module=mock_av,
            initial_backoff=0.01,
            max_backoff=0.02,
        )
        try:
            worker.update_devices([dev])

            # 1. Первая ошибка переводит состояние в failed -> drain() возвращает None
            res1 = wait_for_drain(worker, timeout=1.0)
            assert res1 == {"DEV_FAIL_MULTI": None}

            # 2. Ждем, пока воркер сделает еще несколько ретраев (attempts >= 3)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                with lock:
                    if attempts >= 3:
                        break
                time.sleep(0.01)

            with lock:
                assert attempts >= 3

            # 3. Последующие вызовы drain() должны возвращать пустой словарь,
            # так как повторные сбои без промежуточного успеха НЕ вызывают повторный transition
            res2 = worker.drain()
            assert res2 == {}
            assert not worker._frame_event.is_set()

            # 4. Переход в успех -> drain возвращает кадр
            with lock:
                should_succeed = True
            res3 = wait_for_drain(worker, timeout=1.0)
            assert "DEV_FAIL_MULTI" in res3
            assert res3["DEV_FAIL_MULTI"] is not None
            assert res3["DEV_FAIL_MULTI"].startswith(PNG_SIGNATURE)
        finally:
            worker.close()

    def test_retry_uses_updated_device_info(self) -> None:
        """При ретрае после сбоя в packet_source передается актуализированный DeviceInfo."""
        dev1 = make_test_device("DEV_UPDATE", model="InitialModel", usb_info="1-1")
        dev2 = make_test_device("DEV_UPDATE", model="UpdatedModel", usb_info="1-1")
        mock_av = MockAvModule()
        passed_models: list[str | None] = []
        lock = threading.Lock()

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            with lock:
                passed_models.append(d.model)
                curr = len(passed_models)
            if curr == 1:
                raise RuntimeError("Initial failure")
            yield b"frame"
            while not stop.is_set():
                stop.wait(0.01)

        worker = VideoPreviewWorker(
            source,
            av_module=mock_av,
            initial_backoff=0.02,
            max_backoff=0.05,
        )
        try:
            worker.update_devices([dev1])
            res1 = wait_for_drain(worker, timeout=1.0)
            assert res1 == {"DEV_UPDATE": None}

            # Обновляем метаданные устройства (model изменилась, usb_info прежний -> тот же conn_key)
            worker.update_devices([dev2])

            res2 = wait_for_drain(worker, timeout=1.0)
            assert "DEV_UPDATE" in res2
            assert res2["DEV_UPDATE"] is not None

            with lock:
                assert len(passed_models) >= 2
                assert passed_models[0] == "InitialModel"
                assert passed_models[1] == "UpdatedModel"
        finally:
            worker.close()


# ===========================================================================
# 8. Тесты идентификации подключения и hotplug
# ===========================================================================


class TestConnectionIdentityAndHotplug:
    """Проверка правильного отслеживания USB-подключения и инвалидации кадров."""

    def test_minor_metadata_change_does_not_restart_stream(self) -> None:
        """Изменение незначимых полей (model, state_display) не перезапускает поток."""
        dev1 = make_test_device("DEV_SAME", usb_info="1-1.2", transport_id=10, model="Model A")
        stream_count = 0

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            nonlocal stream_count
            stream_count += 1
            yield b"payload"
            while not stop.is_set():
                stop.wait(0.01)

        worker = VideoPreviewWorker(source, av_module=MockAvModule())
        try:
            worker.update_devices([dev1])
            wait_for_drain(worker)
            assert stream_count == 1

            # Меняем модель и статус отображения
            dev2 = make_test_device("DEV_SAME", usb_info="1-1.2", transport_id=10, model="Model B")
            worker.update_devices([dev2])
            time.sleep(0.05)
            # Новый поток НЕ должен был создаваться
            assert stream_count == 1
        finally:
            worker.close()

    def test_transport_id_change_invalidates_old_frames_and_restarts(self) -> None:
        """Смена transport_id (reconnect) инвалидирует старые кадры и запускает новый поток."""
        dev1 = make_test_device("DEV_RECONN", usb_info="1-1.2", transport_id=10)
        first_stopped = threading.Event()

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            tid = getattr(d, "transport_id", None)
            if str(tid) == "10":
                yield b"packet_old"
                while not stop.is_set():
                    stop.wait(0.01)
                first_stopped.set()
            else:
                yield b"packet_new"
                while not stop.is_set():
                    stop.wait(0.01)

        worker = VideoPreviewWorker(source, av_module=MockAvModule())
        try:
            worker.update_devices([dev1])
            res1 = wait_for_drain(worker)
            assert "DEV_RECONN" in res1

            # Смена transport_id
            dev2 = make_test_device("DEV_RECONN", usb_info="1-1.2", transport_id=20)
            worker.update_devices([dev2])

            # Первый поток должен быть остановлен
            assert first_stopped.wait(timeout=1.0)

            # Приходит новый кадр
            res2 = wait_for_drain(worker)
            assert "DEV_RECONN" in res2
            assert res2["DEV_RECONN"] is not None
        finally:
            worker.close()

    def test_usb_info_change_invalidates_and_restarts(self) -> None:
        """Смена USB-порта (usb_info) перезапускает поток."""
        dev1 = make_test_device("DEV_PORT", usb_info="1-1.1", transport_id=5)
        dev2 = make_test_device("DEV_PORT", usb_info="1-1.2", transport_id=5)

        stream_count = 0

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            nonlocal stream_count
            stream_count += 1
            yield b"payload"
            while not stop.is_set():
                stop.wait(0.01)

        worker = VideoPreviewWorker(source, av_module=MockAvModule())
        try:
            worker.update_devices([dev1])
            wait_for_drain(worker)
            assert stream_count == 1

            worker.update_devices([dev2])
            wait_for_drain(worker)
            assert stream_count == 2
        finally:
            worker.close()

    def test_device_removal_clears_frames_and_stops_stream(self) -> None:
        """Удаление устройства останавливает поток и очищает очередь кадров."""
        dev = make_test_device("DEV_RM", usb_info="1-1.1")
        stopped = threading.Event()

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            yield b"payload"
            while not stop.is_set():
                stop.wait(0.01)
            stopped.set()

        worker = VideoPreviewWorker(source, av_module=MockAvModule())
        try:
            worker.update_devices([dev])
            wait_for_drain(worker)

            worker.update_devices([])
            assert stopped.wait(timeout=1.0)
            assert worker.drain() == {}
        finally:
            worker.close()

    @pytest.mark.parametrize(
        "raw_tid, expected",
        [
            (1, "1"),
            (42, "42"),
            ("1", "1"),
            ("  100  ", "100"),
            (0, None),
            ("0", None),
            ("  0  ", None),
            (-1, None),
            ("-1", None),
            ("-999", None),
            ("", None),
            ("   ", None),
            ("abc", None),
            ("12a", None),
            (None, None),
            (True, None),
            (False, None),
        ],
    )
    def test_get_valid_positive_transport_id(self, raw_tid: Any, expected: str | None) -> None:
        """_get_valid_positive_transport_id возвращает строковый ID только для положительных чисел (> 0)."""
        dev = make_test_device("DEV_TID", transport_id=None)
        dev.transport_id = raw_tid
        assert _get_valid_positive_transport_id(dev) == expected

    def test_device_connection_key_normalizes_invalid_transport_ids_to_none(self) -> None:
        """_device_connection_key нормализует 0, отрицательные числа и пустые строки transport_id в None."""
        dev_none = make_test_device("DEV_KEY", usb_info="1-1.2", transport_id=None)
        expected_key = ("DEV_KEY", "1-1.2", None)
        assert _device_connection_key(dev_none) == expected_key

        for invalid_tid in [0, "0", " 0 ", -1, "-1", "-100", "", "   ", "xyz", True, False]:
            dev = make_test_device("DEV_KEY", usb_info="1-1.2", transport_id=None)
            dev.transport_id = invalid_tid
            assert _device_connection_key(dev) == expected_key, f"Failed for invalid transport_id: {invalid_tid!r}"

    def test_device_connection_key_includes_valid_transport_id(self) -> None:
        """_device_connection_key включает строковое значение валидного положительного transport_id."""
        dev1 = make_test_device("DEV_KEY", usb_info="1-1.2", transport_id=None)
        dev1.transport_id = 15
        assert _device_connection_key(dev1) == ("DEV_KEY", "1-1.2", "15")

        dev2 = make_test_device("DEV_KEY", usb_info="1-1.2", transport_id=None)
        dev2.transport_id = " 100 "
        assert _device_connection_key(dev2) == ("DEV_KEY", "1-1.2", "100")

    def test_invalid_transport_id_transitions_do_not_restart_stream(self) -> None:
        """Переход между невалидными значениями transport_id (None, 0, '', -1) не перезапускает поток."""
        dev1 = make_test_device("DEV_TID_SAME", usb_info="1-1", transport_id=None)
        stream_count = 0

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            nonlocal stream_count
            stream_count += 1
            yield b"payload"
            while not stop.is_set():
                stop.wait(0.01)

        worker = VideoPreviewWorker(source, av_module=MockAvModule())
        try:
            worker.update_devices([dev1])
            wait_for_drain(worker)
            assert stream_count == 1

            # Смена transport_id на 0
            dev2 = make_test_device("DEV_TID_SAME", usb_info="1-1", transport_id=None)
            dev2.transport_id = 0
            worker.update_devices([dev2])
            time.sleep(0.05)
            assert stream_count == 1

            # Смена transport_id на пустую строку
            dev3 = make_test_device("DEV_TID_SAME", usb_info="1-1", transport_id=None)
            dev3.transport_id = ""
            worker.update_devices([dev3])
            time.sleep(0.05)
            assert stream_count == 1

            # Смена transport_id на отрицательное число
            dev4 = make_test_device("DEV_TID_SAME", usb_info="1-1", transport_id=None)
            dev4.transport_id = -5
            worker.update_devices([dev4])
            time.sleep(0.05)
            assert stream_count == 1

            # Теперь обновляем на валидный положительный transport_id=42 -> должен произойти перезапуск
            dev5 = make_test_device("DEV_TID_SAME", usb_info="1-1", transport_id=None)
            dev5.transport_id = 42
            worker.update_devices([dev5])
            wait_for_drain(worker)
            assert stream_count == 2
        finally:
            worker.close()


# ===========================================================================
# 9. Тесты Drain и Close
# ===========================================================================


class TestDrainAndClose:
    """Проверка методов drain() и nonblocking close()."""

    def test_drain_returns_only_newest_frame(self) -> None:
        """drain() возвращает только один новейший кадр на serial."""
        dev = make_test_device("DEV_NEWEST")
        mock_av = MockAvModule()

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            yield b"p1"
            yield b"p2"
            while not stop.is_set():
                stop.wait(0.01)

        worker = VideoPreviewWorker(source, av_module=mock_av, display_cap_fps=0.0)
        try:
            worker.update_devices([dev])
            time.sleep(0.05)
            res = worker.drain()
            assert len(res) == 1
            assert "DEV_NEWEST" in res
            # Повторный вызов drain возвращает пустой словарь
            assert worker.drain() == {}
        finally:
            worker.close()

    def test_close_nonblocking_and_cleans_up(self) -> None:
        """close() отрабатывает неблокирующе и полностью очищает состояние."""
        dev = make_test_device("DEV_CLOSE")
        mock_av = MockAvModule()
        stream_stopped = threading.Event()

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            yield b"p"
            while not stop.is_set():
                stop.wait(0.01)
            stream_stopped.set()

        worker = VideoPreviewWorker(source, av_module=mock_av)
        worker.update_devices([dev])
        wait_for_drain(worker)

        t0 = time.monotonic()
        worker.close()
        t_close = time.monotonic() - t0
        assert t_close < 0.1  # Неблокирующий вызов
        assert worker.drain() == {}

        # Поток устройства останавливается
        assert stream_stopped.wait(timeout=1.0)

    def test_close_is_idempotent(self) -> None:
        """Повторные вызовы close() безопасны."""
        worker = VideoPreviewWorker(MagicMock(), av_module=MockAvModule())
        worker.close()
        worker.close()
        assert worker.drain() == {}

    def test_no_duplicate_streams_for_same_serial(self) -> None:
        """Многократный вызов update_devices с тем же устройством не плодит потоки."""
        dev = make_test_device("DEV_NO_DUP_STREAM")
        threads_created = 0
        lock = threading.Lock()

        def source(d: DeviceInfo, stop: threading.Event) -> Iterator[bytes]:
            nonlocal threads_created
            with lock:
                threads_created += 1
            yield b"p"
            while not stop.is_set():
                stop.wait(0.01)

        worker = VideoPreviewWorker(source, av_module=MockAvModule())
        try:
            for _ in range(5):
                worker.update_devices([dev])
                time.sleep(0.01)

            wait_for_drain(worker)
            with lock:
                assert threads_created == 1
        finally:
            worker.close()
