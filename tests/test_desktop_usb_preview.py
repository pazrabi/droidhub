"""Тесты для изолированного worker обновления миниатюр экранов (desktop/usb_preview.py).

Проверяют:
1. Готовность (readiness): отбор только аппаратных готовых USB-устройств (is_valid_usb_device),
   игнорирование offline/unauthorized, сетевых устройств и отсутствия usb_info.
2. Дубликаты (duplicates): исключение неоднозначных серийных номеров, ограничение до 2 устройств.
3. Правильный argv: использование -t <transport_id> для валидного положительного числового
   transport_id, откат к -s <serial> для остальных, фиксированные аргументы ['exec-out', 'screencap', '-p'],
   shell=False, timeout=3.0.
4. Уменьшение и Pillow: проверка лимитов (< 8 MiB, до 20 Мп), пропорциональное уменьшение
   в границы 132x270 без upscale, возврат валидного PNG.
5. Отключение: исчезновение устройств приводит к забыванию кадров, отсутствие процессов при пустом списке.
6. Отсутствие накопления: drain() возвращает только новые кадры с прошлого опроса, максимум один
   кадр на serial, без очереди полноразмерных кадров.
7. Закрытие: close() неблокирующе ставит отмену, не запускает новые команды, не выдает устаревшие кадры.
8. Смена снимка: смена незначимых полей не инвалидирует кадры, смена параметров USB инвалидирует.
9. Пауза при сбоях: фиксация None в drain(), пауза перед повтором, независимость второго телефона.
"""

from __future__ import annotations

import io
import subprocess
import time
from unittest.mock import MagicMock, patch

from PIL import Image
import pytest

from desktop.adb_client import DeviceInfo
from desktop.usb_preview import (
    FAILURE_PAUSE,
    MAX_IMAGE_PIXELS,
    MAX_RAW_BYTES,
    PNG_SIGNATURE,
    SCREEN_ARGV,
    SUBPROCESS_TIMEOUT,
    TARGET_INTERVAL,
    THUMBNAIL_MAX_HEIGHT,
    THUMBNAIL_MAX_WIDTH,
    USBPreviewWorker,
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


def make_png_bytes(width: int = 100, height: int = 100, color: str = "blue") -> bytes:
    """Генерирует валидные байты PNG заданного размера."""
    buf = io.BytesIO()
    im = Image.new("RGB", (width, height), color=color)
    im.save(buf, format="PNG")
    return buf.getvalue()


def wait_for_drain(worker: USBPreviewWorker, timeout: float = 1.0) -> dict[str, bytes | None]:
    """Ожидает события появления кадра и возвращает результат drain()."""
    worker._frame_event.wait(timeout)
    return worker.drain()


# ===========================================================================
# 1. Тесты готовности устройств (Readiness)
# ===========================================================================


class TestDeviceReadiness:
    """Проверка фильтрации устройств по правилам готовности аппаратного USB."""

    def test_valid_usb_device_is_processed(self) -> None:
        """Готовое аппаратное USB-устройство успешно опрашивается."""
        dev = make_test_device("DEV_OK", state="device", usb_info="1-1.1", is_ready=True)
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = make_png_bytes(100, 100)

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev])
                res = wait_for_drain(worker)
                assert "DEV_OK" in res
                assert res["DEV_OK"] is not None
                assert mock_run.call_count >= 1
            finally:
                worker.close()

    def test_unauthorized_device_ignored(self) -> None:
        """Устройство со статусом unauthorized не опрашивается."""
        dev = make_test_device("DEV_UNAUTH", state="unauthorized", is_ready=False)
        with patch("subprocess.run") as mock_run:
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev])
                time.sleep(0.05)
                mock_run.assert_not_called()
                assert worker.drain() == {}
            finally:
                worker.close()

    def test_offline_device_ignored(self) -> None:
        """Устройство со статусом offline не опрашивается."""
        dev = make_test_device("DEV_OFFLINE", state="offline", is_ready=False)
        with patch("subprocess.run") as mock_run:
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev])
                time.sleep(0.05)
                mock_run.assert_not_called()
                assert worker.drain() == {}
            finally:
                worker.close()

    def test_network_device_ignored(self) -> None:
        """Сетевое ADB-устройство (is_network=True) не опрашивается."""
        dev = make_test_device("DEV_NET", is_network=True, usb_info=None)
        with patch("subprocess.run") as mock_run:
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev])
                time.sleep(0.05)
                mock_run.assert_not_called()
                assert worker.drain() == {}
            finally:
                worker.close()

    def test_network_serial_ignored(self) -> None:
        """Устройство с сетевым serial (IP:порт) не опрашивается."""
        dev = make_test_device("192.168.1.50:5555", is_network=False, usb_info="1-1")
        with patch("subprocess.run") as mock_run:
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev])
                time.sleep(0.05)
                mock_run.assert_not_called()
                assert worker.drain() == {}
            finally:
                worker.close()

    def test_missing_usb_info_ignored(self) -> None:
        """Устройство без usb_info не опрашивается."""
        dev = make_test_device("DEV_NO_USB", usb_info=None)
        with patch("subprocess.run") as mock_run:
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev])
                time.sleep(0.05)
                mock_run.assert_not_called()
                assert worker.drain() == {}
            finally:
                worker.close()

    def test_empty_devices_list_spawns_no_subprocesses(self) -> None:
        """При пустом списке устройств процессы не запускаются."""
        with patch("subprocess.run") as mock_run:
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([])
                time.sleep(0.05)
                mock_run.assert_not_called()
                assert worker.drain() == {}
            finally:
                worker.close()


# ===========================================================================
# 2. Тесты обработки дубликатов (Duplicates) и лимита устройств
# ===========================================================================


class TestDuplicatesAndLimits:
    """Проверка исключения дубликатов и ограничения числа устройств."""

    def test_duplicate_serial_both_valid_excluded(self) -> None:
        """Дубликат serial в ADB-снимке (оба valid) приводит к исключению обоих."""
        dev1 = make_test_device("DEV_DUP", usb_info="1-1.1")
        dev2 = make_test_device("DEV_DUP", usb_info="1-1.2")

        with patch("subprocess.run") as mock_run:
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev1, dev2])
                time.sleep(0.05)
                mock_run.assert_not_called()
                assert worker.drain() == {}
            finally:
                worker.close()

    def test_duplicate_serial_mixed_states_excluded(self) -> None:
        """Дубликат serial с разными статусами (valid + offline) исключается полностью."""
        dev1 = make_test_device("DEV_DUP", state="device", is_ready=True)
        dev2 = make_test_device("DEV_DUP", state="offline", is_ready=False)

        with patch("subprocess.run") as mock_run:
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev1, dev2])
                time.sleep(0.05)
                mock_run.assert_not_called()
                assert worker.drain() == {}
            finally:
                worker.close()

    def test_unique_device_processed_alongside_duplicates(self) -> None:
        """Неоднозначность одного serial не ломает опрос другого валидного устройства."""
        dev_dup1 = make_test_device("DEV_DUP", usb_info="1-1.1")
        dev_dup2 = make_test_device("DEV_DUP", usb_info="1-1.2")
        dev_good = make_test_device("DEV_GOOD", usb_info="1-1.3")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = make_png_bytes(50, 50)

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev_dup1, dev_dup2, dev_good])
                res = wait_for_drain(worker)
                assert "DEV_GOOD" in res
                assert "DEV_DUP" not in res
            finally:
                worker.close()

    def test_max_two_devices_sequentially(self) -> None:
        """Опрашиваются последовательно не более 2 телефонов, 3-й игнорируется."""
        dev1 = make_test_device("DEV_1", usb_info="1-1")
        dev2 = make_test_device("DEV_2", usb_info="1-2")
        dev3 = make_test_device("DEV_3", usb_info="1-3")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = make_png_bytes(50, 50)

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev1, dev2, dev3])
                # Ждем, пока оба устройства получат кадры
                deadline = time.monotonic() + 1.0
                all_res: dict[str, bytes | None] = {}
                while time.monotonic() < deadline and len(all_res) < 2:
                    d = wait_for_drain(worker, timeout=0.2)
                    all_res.update(d)

                assert "DEV_1" in all_res
                assert "DEV_2" in all_res
                assert "DEV_3" not in all_res

                # Проверяем, что в вызовах mock_run фигурируют только DEV_1 и DEV_2
                called_serials = set()
                for call in mock_run.call_args_list:
                    argv = call[0][0]
                    for s in ["DEV_1", "DEV_2", "DEV_3"]:
                        if s in argv:
                            called_serials.add(s)
                assert called_serials == {"DEV_1", "DEV_2"}
            finally:
                worker.close()


# ===========================================================================
# 3. Тесты правильности argv и вызова subprocess
# ===========================================================================


class TestArgvAndSubprocess:
    """Проверка построения команды ADB и параметров subprocess.run."""

    def test_argv_with_valid_positive_transport_id(self) -> None:
        """Положительный числовой transport_id адресуется через -t <id>."""
        dev = make_test_device("DEV_TID", transport_id="42")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = make_png_bytes(100, 100)

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            worker = USBPreviewWorker(adb_path="/custom/bin/adb")
            try:
                worker.update_devices([dev])
                wait_for_drain(worker)
                assert mock_run.called
                args, kwargs = mock_run.call_args
                cmd = args[0]
                assert cmd == ["/custom/bin/adb", "-t", "42", "exec-out", "screencap", "-p"]
                assert kwargs["shell"] is False
                assert kwargs["timeout"] == 3.0
                assert kwargs["stdout"] == subprocess.PIPE
                assert kwargs["stderr"] == subprocess.PIPE
            finally:
                worker.close()

    def test_argv_with_integer_transport_id(self) -> None:
        """Целочисленный положительный transport_id форматируется корректно."""
        dev = make_test_device("DEV_TID_INT")
        dev.transport_id = 101  # type: ignore
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = make_png_bytes(50, 50)

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev])
                wait_for_drain(worker)
                cmd = mock_run.call_args[0][0]
                assert cmd == ["adb", "-t", "101", "exec-out", "screencap", "-p"]
            finally:
                worker.close()

    def test_argv_fallback_to_serial_when_no_transport_id(self) -> None:
        """При отсутствии transport_id происходит откат к -s <serial>."""
        dev = make_test_device("DEV_NO_TID", transport_id=None)
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = make_png_bytes(50, 50)

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev])
                wait_for_drain(worker)
                cmd = mock_run.call_args[0][0]
                assert cmd == ["adb", "-s", "DEV_NO_TID", "exec-out", "screencap", "-p"]
            finally:
                worker.close()

    @pytest.mark.parametrize(
        "invalid_tid",
        ["", "   ", "0", 0, "-1", -5, "abc", True, False],
    )
    def test_argv_fallback_to_serial_on_invalid_transport_ids(self, invalid_tid: object) -> None:
        """Невалидный, нулевой, отрицательный или булев transport_id откатывается к -s <serial>."""
        dev = make_test_device("DEV_BAD_TID")
        dev.transport_id = invalid_tid  # type: ignore
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = make_png_bytes(50, 50)

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev])
                wait_for_drain(worker)
                cmd = mock_run.call_args[0][0]
                assert cmd == ["adb", "-s", "DEV_BAD_TID", "exec-out", "screencap", "-p"]
            finally:
                worker.close()

    def test_argv_with_whitespace_padded_positive_tid(self) -> None:
        """Transport_id с пробелами типа '  99  ' очищается и используется в -t 99."""
        dev = make_test_device("DEV_SPACE_TID", transport_id="  99  ")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = make_png_bytes(50, 50)

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev])
                wait_for_drain(worker)
                cmd = mock_run.call_args[0][0]
                assert cmd == ["adb", "-t", "99", "exec-out", "screencap", "-p"]
            finally:
                worker.close()


# ===========================================================================
# 4. Тесты уменьшения изображений и Pillow-проверок
# ===========================================================================


class TestDownscaleAndPillow:
    """Проверка пропорционального уменьшения в 132x270, лимитов размера и форматов."""

    def test_downscale_large_portrait_image(self) -> None:
        """Полноразмерный скриншот 1080x2400 уменьшается без искажения в границы 132x270."""
        dev = make_test_device("DEV_P1")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = make_png_bytes(1080, 2400, color="red")

        with patch("subprocess.run", return_value=mock_proc):
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev])
                res = wait_for_drain(worker)
                frame = res.get("DEV_P1")
                assert frame is not None
                # Проверяем Pillow
                with Image.open(io.BytesIO(frame)) as img:
                    assert img.format == "PNG"
                    w, h = img.size
                    assert w <= 132
                    assert h <= 270
                    # Пропорция 1080/2400 = 0.45; при высоте 270 ширина 121.5 -> 122
                    assert (w, h) == (122, 270)
            finally:
                worker.close()

    def test_downscale_landscape_image(self) -> None:
        """Альбомное изображение 200x100 уменьшается пропорционально в 132x66."""
        dev = make_test_device("DEV_LAND")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = make_png_bytes(200, 100, color="green")

        with patch("subprocess.run", return_value=mock_proc):
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev])
                res = wait_for_drain(worker)
                frame = res.get("DEV_LAND")
                assert frame is not None
                with Image.open(io.BytesIO(frame)) as img:
                    assert img.format == "PNG"
                    assert img.size == (132, 66)
            finally:
                worker.close()

    def test_no_upscale_for_small_image(self) -> None:
        """Маленькое изображение (50x50) не увеличивается (no upscale)."""
        dev = make_test_device("DEV_SMALL")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = make_png_bytes(50, 50, color="yellow")

        with patch("subprocess.run", return_value=mock_proc):
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev])
                res = wait_for_drain(worker)
                frame = res.get("DEV_SMALL")
                assert frame is not None
                with Image.open(io.BytesIO(frame)) as img:
                    assert img.format == "PNG"
                    assert img.size == (50, 50)
            finally:
                worker.close()

    def test_reject_size_exceeding_8_mib(self) -> None:
        """Вывод размером >= 8 МиБ отбраковывается и фиксирует сбой (None)."""
        dev = make_test_device("DEV_HUGE")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        # Ровно 8 МиБ или больше
        mock_proc.stdout = PNG_SIGNATURE + b"x" * (8 * 1024 * 1024)

        with patch("subprocess.run", return_value=mock_proc):
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev])
                res = wait_for_drain(worker)
                assert "DEV_HUGE" in res
                assert res["DEV_HUGE"] is None
            finally:
                worker.close()

    def test_reject_resolution_over_20_mp(self) -> None:
        """Изображение с разрешением более 20 Мп отбраковывается (возвращает None)."""
        dev = make_test_device("DEV_20MP")
        # 5000 x 4001 = 20_005_000 (> 20 MP)
        # Создаем валидный PNG заголовок IHDR для большого размера без расхода памяти
        buf = io.BytesIO()
        im = Image.new("1", (5000, 4001))
        im.save(buf, format="PNG")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = buf.getvalue()

        with patch("subprocess.run", return_value=mock_proc):
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev])
                res = wait_for_drain(worker)
                assert "DEV_20MP" in res
                assert res["DEV_20MP"] is None
            finally:
                worker.close()

    def test_reject_corrupt_or_non_png_data(self) -> None:
        """Невалидные байты или чужой формат приводят к фиксации None."""
        dev = make_test_device("DEV_CORRUPT")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = b"NOT_A_PNG_FILE"

        with patch("subprocess.run", return_value=mock_proc):
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev])
                res = wait_for_drain(worker)
                assert "DEV_CORRUPT" in res
                assert res["DEV_CORRUPT"] is None
            finally:
                worker.close()

    def test_subprocess_failure_produces_none(self) -> None:
        """Ненулевой код возврата команды adb screencap фиксирует None."""
        dev = make_test_device("DEV_ERR")
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = b""

        with patch("subprocess.run", return_value=mock_proc):
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev])
                res = wait_for_drain(worker)
                assert "DEV_ERR" in res
                assert res["DEV_ERR"] is None
            finally:
                worker.close()

    def test_subprocess_timeout_produces_none(self) -> None:
        """Таймаут выполнения команды adb приводит к фиксации None."""
        dev = make_test_device("DEV_TIMEOUT")

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["adb"], timeout=3.0)):
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev])
                res = wait_for_drain(worker)
                assert "DEV_TIMEOUT" in res
                assert res["DEV_TIMEOUT"] is None
            finally:
                worker.close()


# ===========================================================================
# 5. Тесты отключения устройств (Disconnection)
# ===========================================================================


class TestDeviceDisconnection:
    """Проверка очистки состояния при отключении устройств."""

    def test_disappearing_device_frames_forgotten(self) -> None:
        """При исчезновении устройства из снимка его неотправленные кадры забываются."""
        dev = make_test_device("DEV_GONE")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = make_png_bytes(50, 50)

        with patch("subprocess.run", return_value=mock_proc):
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev])
                # Дожидаемся кадра в _pending, но НЕ вызываем drain()
                worker._frame_event.wait(1.0)
                assert "DEV_GONE" in worker._pending

                # Устройство отключается (пустой снимок)
                worker.update_devices([])
                res = worker.drain()
                assert "DEV_GONE" not in res
                assert res == {}
            finally:
                worker.close()

    def test_idle_after_all_devices_disconnected(self) -> None:
        """После отключения всех телефонов воркер засыпает и не вызывает процессов."""
        dev = make_test_device("DEV_TEMP")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = make_png_bytes(50, 50)

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev])
                wait_for_drain(worker)
                initial_count = mock_run.call_count

                # Отключаем
                worker.update_devices([])
                time.sleep(0.1)
                # Никаких новых процессов не запускается
                assert mock_run.call_count == initial_count
            finally:
                worker.close()


# ===========================================================================
# 6. Тесты отсутствия накопления (No Accumulation)
# ===========================================================================


class TestNoAccumulation:
    """Проверка отсутствия очередей и возврата только новых кадров."""

    def test_drain_clears_queue_subsequent_drain_empty(self) -> None:
        """Повторный drain() сразу после первого возвращает пустой словарь."""
        dev = make_test_device("DEV_ONCE")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = make_png_bytes(50, 50)

        with patch("subprocess.run", return_value=mock_proc):
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev])
                first_drain = wait_for_drain(worker)
                assert "DEV_ONCE" in first_drain

                # Сразу второй drain()
                second_drain = worker.drain()
                assert second_drain == {}
            finally:
                worker.close()

    def test_at_most_one_frame_per_serial(self) -> None:
        """В словаре pending всегда хранится максимум один кадр на серийный номер."""
        dev = make_test_device("DEV_SINGLE")
        frame1 = make_png_bytes(50, 50, color="red")
        frame2 = make_png_bytes(50, 50, color="blue")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = frame1

        with patch("subprocess.run", return_value=mock_proc):
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev])
                worker._frame_event.wait(1.0)

                # Имитируем появление второго кадра до вызова drain()
                mock_proc.stdout = frame2
                worker._frame_event.clear()
                # Принудительно вызываем цикл захвата
                worker._capture_device(dev)

                with worker._lock:
                    assert len(worker._pending) <= 1
            finally:
                worker.close()


# ===========================================================================
# 7. Тесты закрытия воркера (Close)
# ===========================================================================


class TestWorkerClose:
    """Проверка неблокирующего закрытия и отсутствия новых команд."""

    def test_close_is_non_blocking_and_stops_thread(self) -> None:
        """Вызов close() завершается мгновенно и останавливает поток демона."""
        worker = USBPreviewWorker(adb_path="adb")
        t0 = time.monotonic()
        worker.close()
        elapsed = time.monotonic() - t0
        assert elapsed < 0.1
        # Поток завершается
        worker._thread.join(timeout=0.5)
        assert not worker._thread.is_alive()

    def test_no_new_commands_after_close(self) -> None:
        """После close() новые команды subprocess не запускаются."""
        dev = make_test_device("DEV_CLOSED")
        with patch("subprocess.run") as mock_run:
            worker = USBPreviewWorker(adb_path="adb")
            worker.close()
            worker.update_devices([dev])
            time.sleep(0.05)
            mock_run.assert_not_called()
            assert worker.drain() == {}

    def test_drain_returns_empty_after_close(self) -> None:
        """После закрытия drain() не выдает устаревшие кадры."""
        dev = make_test_device("DEV_STALE")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = make_png_bytes(50, 50)

        with patch("subprocess.run", return_value=mock_proc):
            worker = USBPreviewWorker(adb_path="adb")
            worker.update_devices([dev])
            worker._frame_event.wait(1.0)
            assert "DEV_STALE" in worker._pending

            # Закрываем
            worker.close()
            assert worker.drain() == {}

    def test_close_is_idempotent(self) -> None:
        """Повторный вызов close() безопасен."""
        worker = USBPreviewWorker(adb_path="adb")
        worker.close()
        worker.close()
        assert worker._closed is True


# ===========================================================================
# 8. Тесты смены снимка vs незначимых полей
# ===========================================================================


class TestSnapshotChangeAndInsignificantFields:
    """Проверка инвалидации кадров при смене соединения и сохранения при незначимых полях."""

    def test_insignificant_field_change_does_not_invalidate_frame(self) -> None:
        """Смена модели или display_name при том же USB-соединении не инвалидирует кадр."""
        dev1 = make_test_device("DEV_SAME", usb_info="1-1.1", transport_id="5", model="Pixel 7")
        dev2 = make_test_device("DEV_SAME", usb_info="1-1.1", transport_id="5", model="Pixel 7 Pro")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = make_png_bytes(50, 50)

        with patch("subprocess.run", return_value=mock_proc):
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev1])
                worker._frame_event.wait(1.0)
                assert "DEV_SAME" in worker._pending

                # Передаем обновленное устройство с измененной моделью
                worker.update_devices([dev2])
                # Кадр остался в pending
                res = worker.drain()
                assert "DEV_SAME" in res
                assert res["DEV_SAME"] is not None
            finally:
                worker.close()

    def test_transport_id_change_invalidates_stale_frame(self) -> None:
        """Смена transport_id (переподключение USB) инвалидирует устаревший кадр."""
        dev1 = make_test_device("DEV_RECONN", usb_info="1-1.1", transport_id="5")
        dev2 = make_test_device("DEV_RECONN", usb_info="1-1.1", transport_id="6")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = make_png_bytes(50, 50)

        with patch("subprocess.run", return_value=mock_proc):
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev1])
                worker._frame_event.wait(1.0)
                assert "DEV_RECONN" in worker._pending

                # Останавливаем worker._capture_device перед обновлением, чтобы проверить инвалидацию
                with patch.object(worker, "_capture_device", return_value=(None, False)):
                    worker.update_devices([dev2])
                    # Старый кадр от transport_id 5 должен быть сброшен
                    assert "DEV_RECONN" not in worker._pending
            finally:
                worker.close()

    def test_usb_info_change_invalidates_stale_frame(self) -> None:
        """Смена usb_info (переключение порта) инвалидирует устаревший кадр."""
        dev1 = make_test_device("DEV_PORT", usb_info="1-1.1", transport_id="5")
        dev2 = make_test_device("DEV_PORT", usb_info="1-1.2", transport_id="5")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = make_png_bytes(50, 50)

        with patch("subprocess.run", return_value=mock_proc):
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev1])
                worker._frame_event.wait(1.0)
                assert "DEV_PORT" in worker._pending

                with patch.object(worker, "_capture_device", return_value=(None, False)):
                    worker.update_devices([dev2])
                    assert "DEV_PORT" not in worker._pending
            finally:
                worker.close()


# ===========================================================================
# 9. Тесты паузы при сбоях и независимости устройств
# ===========================================================================


class TestFailurePauseAndIndependence:
    """Проверка паузы при сбоях и непрерывности работы соседних устройств."""

    def test_failure_sets_none_and_pauses(self) -> None:
        """При сбое устанавливается None в pending и включается пауза FAILURE_PAUSE."""
        dev = make_test_device("DEV_FAIL")
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = b"error"

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev])
                res = wait_for_drain(worker)
                assert res.get("DEV_FAIL") is None

                # Во время паузы FAILURE_PAUSE новых вызовов быть не должно
                initial_count = mock_run.call_count
                time.sleep(0.1)
                assert mock_run.call_count == initial_count
            finally:
                worker.close()

    def test_failure_on_one_device_does_not_block_second_device(self) -> None:
        """Сбой одного телефона не мешает успешному обновлению второго телефона."""
        dev1 = make_test_device("DEV_FAILS", usb_info="1-1")
        dev2 = make_test_device("DEV_WORKS", usb_info="1-2")

        valid_png = make_png_bytes(50, 50)

        def mock_subprocess_run(cmd: list[str], **kwargs: object) -> MagicMock:
            proc = MagicMock()
            if "DEV_FAILS" in cmd:
                proc.returncode = 1
                proc.stdout = b""
            else:
                proc.returncode = 0
                proc.stdout = valid_png
            return proc

        with patch("subprocess.run", side_effect=mock_subprocess_run):
            worker = USBPreviewWorker(adb_path="adb")
            try:
                worker.update_devices([dev1, dev2])

                deadline = time.monotonic() + 1.0
                all_res: dict[str, bytes | None] = {}
                while time.monotonic() < deadline and len(all_res) < 2:
                    d = wait_for_drain(worker, timeout=0.2)
                    all_res.update(d)

                assert all_res.get("DEV_FAILS") is None
                assert all_res.get("DEV_WORKS") is not None
            finally:
                worker.close()
