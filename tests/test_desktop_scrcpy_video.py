"""Модульные тесты для desktop/scrcpy_video.py (транспорт H.264 по USB scrcpy v5.0.1)."""

from __future__ import annotations

import io
from pathlib import Path
import socket
import struct
import subprocess
import threading
import time
from unittest.mock import MagicMock, call, patch

import pytest

from desktop.adb_client import DeviceInfo
from desktop.scrcpy_video import (
    ANNEX_B_START_CODE_4,
    MAX_PACKET_SIZE,
    PACKET_FLAG_CONFIG,
    PACKET_FLAG_KEY_FRAME,
    PACKET_FLAG_SESSION,
    SCRCPY_VERSION_PIN,
    ScrcpyVideoError,
    _get_valid_positive_transport_id,
    _peek_exact,
    _recv_exact,
    find_preview_server,
    iter_h264_packets,
    validate_usb_device,
)


# ---------------------------------------------------------------------------
# Тестовые фикстуры и помощники
# ---------------------------------------------------------------------------


def make_device(
    serial: str = "USB_SERIAL_123",
    state: str = "device",
    usb_info: str | None = "1-1.2",
    is_network: bool = False,
    is_ready: bool = True,
    transport_id: str | None = "42",
) -> DeviceInfo:
    """Создаёт экземпляр DeviceInfo для тестирования."""
    return DeviceInfo(
        serial=serial,
        state=state,
        state_display="Готов к работе" if is_ready else "Не готов",
        is_network=is_network,
        connection_type="USB" if not is_network else "Сетевое (ADB TCP/IP)",
        is_ready=is_ready,
        model="Pixel 8a",
        transport_id=transport_id,
        usb_info=usb_info,
    )


def pack_frame(flags_and_pts: int, payload: bytes) -> bytes:
    """Упаковывает 12-байтный заголовок кадра scrcpy с полезной нагрузкой."""
    header = struct.pack(">QI", flags_and_pts, len(payload))
    return header + payload


class MockStreamSocket:
    """Имитирует TCP-сокет, передающий синтетические данные сервера scrcpy."""

    def __init__(self, data: bytes = b"", timeout_on_empty: bool = False) -> None:
        self._buffer = bytearray(data)
        self._closed = False
        self._timeout_on_empty = timeout_on_empty
        self.connected_address = None

    def connect(self, addr: tuple[str, int]) -> None:
        self.connected_address = addr

    def settimeout(self, timeout: float) -> None:
        pass

    def recv(self, bufsize: int, flags: int = 0) -> bytes:
        if self._closed:
            return b""
        if not self._buffer:
            if self._timeout_on_empty:
                raise socket.timeout("timed out")
            return b""
        if flags == socket.MSG_PEEK:
            return bytes(self._buffer[:bufsize])
        chunk = bytes(self._buffer[:bufsize])
        del self._buffer[:bufsize]
        return chunk

    def close(self) -> None:
        self._closed = True


# ---------------------------------------------------------------------------
# Тесты: Валидация USB-устройства и адресация
# ---------------------------------------------------------------------------


def test_validate_usb_device_success():
    dev = make_device()
    validate_usb_device(dev)


def test_validate_usb_device_none():
    with pytest.raises(ScrcpyVideoError, match="Устройство не может быть None"):
        validate_usb_device(None)  # type: ignore


def test_validate_usb_device_empty_serial():
    dev = make_device(serial="")
    with pytest.raises(ScrcpyVideoError, match="Серийный номер устройства отсутствует"):
        validate_usb_device(dev)


def test_validate_usb_device_reject_network():
    dev = make_device(is_network=True)
    with pytest.raises(ScrcpyVideoError, match="Сетевое устройство.*запрещено"):
        validate_usb_device(dev)


def test_validate_usb_device_reject_network_serial():
    dev = make_device(serial="192.168.1.100:5555")
    with pytest.raises(ScrcpyVideoError, match="Сетевое устройство.*запрещено"):
        validate_usb_device(dev)


def test_validate_usb_device_reject_emulator():
    dev = make_device(serial="emulator-5554")
    with pytest.raises(ScrcpyVideoError, match="Эмулятор.*запрещён"):
        validate_usb_device(dev)


def test_validate_usb_device_reject_missing_usb_info():
    dev = make_device(usb_info=None)
    with pytest.raises(ScrcpyVideoError, match="не имеет usb_info"):
        validate_usb_device(dev)

    dev_empty = make_device(usb_info="   ")
    with pytest.raises(ScrcpyVideoError, match="не имеет usb_info"):
        validate_usb_device(dev_empty)


def test_validate_usb_device_reject_bad_state():
    dev_offline = make_device(state="offline", is_ready=False)
    with pytest.raises(ScrcpyVideoError, match="не в состоянии 'device'"):
        validate_usb_device(dev_offline)

    dev_not_ready = make_device(state="device", is_ready=False)
    with pytest.raises(ScrcpyVideoError, match="не готово к работе"):
        validate_usb_device(dev_not_ready)


def test_get_valid_positive_transport_id():
    dev = make_device(transport_id="123")
    assert _get_valid_positive_transport_id(dev) == "123"

    dev_zero = make_device(transport_id="0")
    assert _get_valid_positive_transport_id(dev_zero) is None

    dev_negative = make_device(transport_id="-5")
    assert _get_valid_positive_transport_id(dev_negative) is None

    dev_bool = make_device(transport_id=True)  # type: ignore
    assert _get_valid_positive_transport_id(dev_bool) is None

    dev_none = make_device(transport_id=None)
    assert _get_valid_positive_transport_id(dev_none) is None

    dev_str = make_device(transport_id="abc")
    assert _get_valid_positive_transport_id(dev_str) is None


# ---------------------------------------------------------------------------
# Тесты: find_preview_server
# ---------------------------------------------------------------------------


def test_find_preview_server_explicit_version_mismatch():
    assert find_preview_server("/usr/bin/scrcpy", "5.0.0") is None
    assert find_preview_server("/usr/bin/scrcpy", "4.0") is None
    assert find_preview_server("/usr/bin/scrcpy", "5.0.10") is None
    assert find_preview_server("/usr/bin/scrcpy", "5.0.1.2") is None
    assert find_preview_server("/usr/bin/scrcpy", "2.5.0.1") is None
    assert find_preview_server("/usr/bin/scrcpy", "15.0.1") is None
    assert find_preview_server("/usr/bin/scrcpy", "5.0.1a") is None


def test_find_preview_server_sibling_scrcpy_server(tmp_path: Path):
    scrcpy_bin = tmp_path / "scrcpy"
    scrcpy_bin.write_text("#!/bin/sh\n")
    server_bin = tmp_path / "scrcpy-server"
    server_bin.write_bytes(b"PK\x03\x04")

    found = find_preview_server(str(scrcpy_bin), "5.0.1")
    assert found == server_bin


def test_find_preview_server_sibling_scrcpy_server_jar(tmp_path: Path):
    scrcpy_bin = tmp_path / "scrcpy"
    scrcpy_bin.write_text("#!/bin/sh\n")
    server_jar = tmp_path / "scrcpy-server.jar"
    server_jar.write_bytes(b"PK\x03\x04")

    found = find_preview_server(str(scrcpy_bin), "5.0.1")
    assert found == server_jar


def test_find_preview_server_direct_server_file(tmp_path: Path):
    server_bin = tmp_path / "scrcpy-server"
    server_bin.write_bytes(b"PK\x03\x04")

    found = find_preview_server(str(server_bin), "5.0.1")
    assert found == server_bin


def test_find_preview_server_probed_version(tmp_path: Path):
    scrcpy_bin = tmp_path / "scrcpy"
    scrcpy_bin.write_text("#!/bin/sh\n")
    server_bin = tmp_path / "scrcpy-server"
    server_bin.write_bytes(b"PK\x03\x04")

    with patch("desktop.scrcpy_video.probe_version", return_value="scrcpy 5.0.1 <https://github.com/Genymobile/scrcpy>"):
        found = find_preview_server(str(scrcpy_bin), None)
        assert found == server_bin

    with patch("desktop.scrcpy_video.probe_version", return_value="scrcpy 2.4"):
        assert find_preview_server(str(scrcpy_bin), None) is None

    for bad_ver in ["scrcpy 5.0.1.2", "scrcpy 2.5.0.1", "scrcpy 5.0.10", "scrcpy 5.0.0"]:
        with patch("desktop.scrcpy_video.probe_version", return_value=bad_ver):
            assert find_preview_server(str(scrcpy_bin), None) is None


def test_find_preview_server_not_found(tmp_path: Path):
    scrcpy_bin = tmp_path / "scrcpy"
    scrcpy_bin.write_text("#!/bin/sh\n")
    # В каталоге нет соседнего файла сервера
    assert find_preview_server(str(scrcpy_bin), "5.0.1") is None


def test_find_preview_server_default_candidates(tmp_path: Path):
    cand_dir = tmp_path / "opt"
    cand_dir.mkdir()
    scrcpy_bin = cand_dir / "scrcpy"
    scrcpy_bin.write_text("#!/bin/sh\n")
    server_bin = cand_dir / "scrcpy-server"
    server_bin.write_bytes(b"PK\x03\x04")

    with patch("desktop.scrcpy_video.get_scrcpy_candidates", return_value=[str(scrcpy_bin)]):
        found = find_preview_server(None, "5.0.1")
        assert found == server_bin


# ---------------------------------------------------------------------------
# Тесты: iter_h264_packets Транспорт и протокол
# ---------------------------------------------------------------------------


def test_iter_h264_packets_full_protocol_stream(tmp_path: Path):
    """Проверяет полный пайплайн передачи видео:
    - Положительный transport_id (-t 42) используется для всех вызовов subprocess
    - Уникальный собственный JAR загружается и удаляется
    - tcp:0 перенаправляется на уникальный scrcpy_<scid>
    - Подключение строго к 127.0.0.1
    - Корректные параметры сервера v5.0.1 (max_size=288, max_fps=12, video_bit_rate=500000)
    - stdout=subprocess.DEVNULL для предотвращения дедлока пайпа
    - Проверочный dummy byte обрабатывается
    - Пакеты конфигурации добавляются перед следующим медиа-пакетом
    - Выдаются полные единицы доступа Annex B
    """
    server_file = tmp_path / "scrcpy-server"
    server_file.write_bytes(b"DEX_OR_JAR_DATA")

    device = make_device(serial="DEVICE1", transport_id="42")
    stop = threading.Event()

    dummy = b"\x00"
    config_payload = b"\x00\x00\x00\x01\x67\x42\x00\x1f\xe9\x01"
    config_packet = pack_frame(PACKET_FLAG_CONFIG, config_payload)

    idr_payload = b"\x00\x00\x00\x01\x65\x88\x84\x00\x10\x20\x30\x40\x50\x60\x70\x80\x90\xa0\xb0\xc0"
    idr_packet = pack_frame(PACKET_FLAG_KEY_FRAME | 1000, idr_payload)

    p_payload = b"\x00\x00\x00\x01\x41\x9a\x01\x02\x03\x04\x05\x06\x07\x08\x09\x10\x11\x12\x13"
    p_packet = pack_frame(2000, p_payload)

    stream_data = dummy + config_packet + idr_packet + p_packet
    mock_sock = MockStreamSocket(stream_data)

    calls_run = []

    def fake_subprocess_run(cmd, *args, **kwargs):
        calls_run.append(list(cmd))
        mock_res = MagicMock()
        mock_res.returncode = 0
        if "forward" in cmd and "tcp:0" in cmd:
            mock_res.stdout = "48123\n"
        else:
            mock_res.stdout = ""
        mock_res.stderr = ""
        return mock_res

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_proc.returncode = 0
    mock_proc.stdout = None
    mock_proc.stderr = None

    with (
        patch("subprocess.run", side_effect=fake_subprocess_run),
        patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
        patch("socket.socket", return_value=mock_sock),
    ):
        packets = list(iter_h264_packets("adb", device, server_file, stop))

    # Проверка выданных пакетов
    assert len(packets) == 2
    # Пакет 1 содержит SPS/PPS перед IDR
    assert packets[0] == config_payload + idr_payload
    assert packets[0].startswith(ANNEX_B_START_CODE_4)
    # Пакет 2 - P-кадр
    assert packets[1] == p_payload
    assert packets[1].startswith(ANNEX_B_START_CODE_4)

    # Проверка адреса подключения: строго 127.0.0.1:48123
    assert mock_sock.connected_address == ("127.0.0.1", 48123)

    # Проверка вызовов команд subprocess
    push_call = calls_run[0]
    assert push_call[:4] == ["adb", "-t", "42", "push"]
    assert push_call[4] == str(server_file)
    remote_jar = push_call[5]
    assert remote_jar.startswith("/data/local/tmp/scrcpy_")
    assert remote_jar.endswith(".jar")

    fwd_call = calls_run[1]
    assert fwd_call[:5] == ["adb", "-t", "42", "forward", "tcp:0"]
    assert fwd_call[5].startswith("localabstract:scrcpy_")

    # Проверка команды запуска сервера scrcpy v5.0.1 и stdout=DEVNULL
    assert mock_popen.call_args[1].get("stdout") == subprocess.DEVNULL
    assert mock_popen.call_args[1].get("stderr") == subprocess.PIPE

    server_cmd = mock_popen.call_args[0][0]
    assert server_cmd[:4] == ["adb", "-t", "42", "shell"]
    assert f"CLASSPATH={remote_jar}" in server_cmd[4]
    assert server_cmd[5:9] == ["app_process", "/", "com.genymobile.scrcpy.Server", "5.0.1"]
    assert any("scid=" in arg for arg in server_cmd)
    assert "tunnel_forward=true" in server_cmd
    assert "audio=false" in server_cmd
    assert "control=false" in server_cmd
    assert "cleanup=false" in server_cmd
    assert "max_size=288" in server_cmd
    assert "max_fps=12" in server_cmd
    assert "video_bit_rate=500000" in server_cmd
    assert "send_device_meta=false" in server_cmd
    assert "send_frame_meta=true" in server_cmd
    assert "send_dummy_byte=true" in server_cmd
    assert "send_stream_meta=false" in server_cmd

    # Очистка в finally: удаление forward, завершение процесса, удаление JAR
    fwd_rm_call = [c for c in calls_run if "forward" in c and "--remove" in c][0]
    assert fwd_rm_call == ["adb", "-t", "42", "forward", "--remove", "tcp:48123"]

    rm_jar_call = [c for c in calls_run if "shell" in c and "rm" in c][0]
    assert rm_jar_call == ["adb", "-t", "42", "shell", "rm", "-f", remote_jar]

    mock_proc.terminate.assert_called_once()


def test_iter_h264_packets_serial_fallback_when_no_transport_id(tmp_path: Path):
    """При отсутствии или некорректном transport_id используется -s <serial>."""
    server_file = tmp_path / "scrcpy-server"
    server_file.write_bytes(b"DEX")

    device = make_device(serial="SERIAL_XYZ", transport_id=None)
    stop = threading.Event()

    dummy = b"\x00"
    media_packet = pack_frame(100, b"\x00\x00\x00\x01\x65RAW")
    mock_sock = MockStreamSocket(dummy + media_packet)

    calls = []

    def fake_subprocess_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "50001\n"
        return mock_res

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    with (
        patch("subprocess.run", side_effect=fake_subprocess_run),
        patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
        patch("socket.socket", return_value=mock_sock),
    ):
        packets = list(iter_h264_packets("adb", device, server_file, stop))

    assert len(packets) == 1
    assert calls[0][:4] == ["adb", "-s", "SERIAL_XYZ", "push"]
    assert calls[1][:5] == ["adb", "-s", "SERIAL_XYZ", "forward", "tcp:0"]
    server_cmd = mock_popen.call_args[0][0]
    assert server_cmd[:4] == ["adb", "-s", "SERIAL_XYZ", "shell"]


def test_iter_h264_packets_parses_session_packet_and_stream_meta(tmp_path: Path):
    """Проверяет:
    - Метаданные потока (4 байта "h264" + 12 байт начальный session packet = 16 байт) корректно поглощаются.
    - Промежуточный 12-байтный session packet (без payload) пропускается без смещения последующего кадра H.264.
    """
    server_file = tmp_path / "scrcpy-server"
    server_file.write_bytes(b"DEX")

    device = make_device()
    stop = threading.Event()

    dummy = b"\x00"
    # Метаданные потока scrcpy v5.0.1: 4 байта кодек + 12 байт начальный пакет сессии (всего 16 байт)
    stream_meta = b"h264" + struct.pack(">III", 0x80000000, 288, 512)
    assert len(stream_meta) == 16

    # Промежуточный пакет сессии при повороте экрана (ровно 12 байт без полезной нагрузки)
    session_packet = struct.pack(">III", 0x80000000, 512, 288)
    assert len(session_packet) == 12

    media_payload = b"\x00\x00\x00\x01\x65FRAME"
    media_packet = pack_frame(100, media_payload)

    stream_data = dummy + stream_meta + session_packet + media_packet
    mock_sock = MockStreamSocket(stream_data)

    def fake_subprocess_run(cmd, *args, **kwargs):
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "50002\n"
        return mock_res

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    with (
        patch("subprocess.run", side_effect=fake_subprocess_run),
        patch("subprocess.Popen", return_value=mock_proc),
        patch("socket.socket", return_value=mock_sock),
    ):
        packets = list(iter_h264_packets("adb", device, server_file, stop))

    assert len(packets) == 1
    assert packets[0] == media_payload


def test_iter_h264_packets_orientation_change_mid_stream(tmp_path: Path):
    """Проверяет, что при смене ориентации (поступлении 12-байтного session packet между кадрами)
    следующий H.264-кадр читается точно без смещения."""
    server_file = tmp_path / "scrcpy-server"
    server_file.write_bytes(b"DEX")

    device = make_device()
    stop = threading.Event()

    dummy = b"\x00"
    frame1 = b"\x00\x00\x00\x01\x65FRAME_ONE"
    frame2 = b"\x00\x00\x00\x01\x65FRAME_TWO"

    packet1 = pack_frame(100, frame1)
    # Пакет сессии при повороте устройства: ровно 12 байт, без полезной нагрузки
    rotation_session = struct.pack(">III", 0x80000000, 512, 288)
    packet2 = pack_frame(200, frame2)

    stream_data = dummy + packet1 + rotation_session + packet2
    mock_sock = MockStreamSocket(stream_data)

    def fake_subprocess_run(cmd, *args, **kwargs):
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "50008\n"
        return mock_res

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    with (
        patch("subprocess.run", side_effect=fake_subprocess_run),
        patch("subprocess.Popen", return_value=mock_proc),
        patch("socket.socket", return_value=mock_sock),
    ):
        packets = list(iter_h264_packets("adb", device, server_file, stop))

    assert len(packets) == 2
    assert packets[0] == frame1
    assert packets[1] == frame2


def test_iter_h264_packets_ensures_annex_b_start_code(tmp_path: Path):
    """Если полезная нагрузка не начинается со стартового кода Annex B, он автоматически добавляется."""
    server_file = tmp_path / "scrcpy-server"
    server_file.write_bytes(b"DEX")

    device = make_device()
    stop = threading.Event()

    dummy = b"\x00"
    raw_slice_without_start_code = b"\x65\x88\x99"
    media_packet = pack_frame(100, raw_slice_without_start_code)

    mock_sock = MockStreamSocket(dummy + media_packet)

    def fake_subprocess_run(cmd, *args, **kwargs):
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "50003\n"
        return mock_res

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    with (
        patch("subprocess.run", side_effect=fake_subprocess_run),
        patch("subprocess.Popen", return_value=mock_proc),
        patch("socket.socket", return_value=mock_sock),
    ):
        packets = list(iter_h264_packets("adb", device, server_file, stop))

    assert len(packets) == 1
    assert packets[0] == ANNEX_B_START_CODE_4 + raw_slice_without_start_code


def test_iter_h264_packets_stop_cancellation_on_static_scene(tmp_path: Path):
    """Генератор немедленно завершает работу при активации stop.set() даже на статичной сцене."""
    server_file = tmp_path / "scrcpy-server"
    server_file.write_bytes(b"DEX")

    device = make_device()
    stop = threading.Event()

    dummy = b"\x00"
    mock_sock = MockStreamSocket(dummy, timeout_on_empty=True)

    def fake_subprocess_run(cmd, *args, **kwargs):
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "50004\n"
        return mock_res

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    def stopper():
        time.sleep(0.05)
        stop.set()

    t = threading.Thread(target=stopper)
    t.start()

    with (
        patch("subprocess.run", side_effect=fake_subprocess_run),
        patch("subprocess.Popen", return_value=mock_proc),
        patch("socket.socket", return_value=mock_sock),
    ):
        packets = list(iter_h264_packets("adb", device, server_file, stop))

    t.join()
    assert packets == []


def test_iter_h264_packets_oversize_packet_raises(tmp_path: Path):
    """Пакеты с размером, превышающим MAX_PACKET_SIZE, отклоняются с исключением."""
    server_file = tmp_path / "scrcpy-server"
    server_file.write_bytes(b"DEX")

    device = make_device()
    stop = threading.Event()

    dummy = b"\x00"
    huge_header = struct.pack(">QI", 100, MAX_PACKET_SIZE + 1)
    mock_sock = MockStreamSocket(dummy + huge_header)

    def fake_subprocess_run(cmd, *args, **kwargs):
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "50005\n"
        return mock_res

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    with (
        patch("subprocess.run", side_effect=fake_subprocess_run),
        patch("subprocess.Popen", return_value=mock_proc),
        patch("socket.socket", return_value=mock_sock),
    ):
        with pytest.raises(ScrcpyVideoError, match="превышает максимально допустимый"):
            list(iter_h264_packets("adb", device, server_file, stop))


def test_iter_h264_packets_safe_cleanup_on_push_failure(tmp_path: Path):
    """При ошибке adb push исключение выбрасывается, а удалённый файл подчищается."""
    server_file = tmp_path / "scrcpy-server"
    server_file.write_bytes(b"DEX")

    device = make_device()
    stop = threading.Event()

    calls = []

    def fake_subprocess_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        mock_res = MagicMock()
        if "push" in cmd:
            mock_res.returncode = 1
            mock_res.stderr = b"Permission denied"
        else:
            mock_res.returncode = 0
        return mock_res

    with patch("subprocess.run", side_effect=fake_subprocess_run):
        with pytest.raises(ScrcpyVideoError, match="Не удалось загрузить серверный JAR scrcpy"):
            list(iter_h264_packets("adb", device, server_file, stop))

    # Блок finally обязательно выполняет rm удалённого jar
    assert any("rm" in c for c in calls)


def test_iter_h264_packets_dummy_byte_missing_fails(tmp_path: Path):
    """Если соединение закрылось до получения проверочного байта готовности, выбрасывается ошибка."""
    server_file = tmp_path / "scrcpy-server"
    server_file.write_bytes(b"DEX")

    device = make_device()
    stop = threading.Event()

    mock_sock = MockStreamSocket(b"")  # Пустой поток

    def fake_subprocess_run(cmd, *args, **kwargs):
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "50006\n"
        return mock_res

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    with (
        patch("subprocess.run", side_effect=fake_subprocess_run),
        patch("subprocess.Popen", return_value=mock_proc),
        patch("socket.socket", return_value=mock_sock),
    ):
        with pytest.raises(ScrcpyVideoError, match="Не удалось получить проверочный байт готовности"):
            list(iter_h264_packets("adb", device, server_file, stop))


def test_iter_h264_packets_already_stopped(tmp_path: Path):
    """Если событие stop выставлено до запуска, генератор мгновенно завершается."""
    server_file = tmp_path / "scrcpy-server"
    server_file.write_bytes(b"DEX")

    device = make_device()
    stop = threading.Event()
    stop.set()

    with patch("subprocess.run") as mock_run:
        packets = list(iter_h264_packets("adb", device, server_file, stop))
        assert packets == []
        mock_run.assert_not_called()


def test_iter_h264_packets_invalid_args(tmp_path: Path):
    """Проверка валидации входных аргументов пути adb и файла сервера."""
    device = make_device()
    stop = threading.Event()

    with pytest.raises(ScrcpyVideoError, match="Некорректный путь к adb"):
        list(iter_h264_packets("", device, tmp_path / "server", stop))

    with pytest.raises(ScrcpyVideoError, match="Файл сервера scrcpy не найден"):
        list(iter_h264_packets("adb", device, tmp_path / "non_existent_server", stop))


def test_iter_h264_packets_bad_forward_port(tmp_path: Path):
    """Если вывод adb forward содержит некорректный номер порта, выбрасывается ошибка."""
    server_file = tmp_path / "scrcpy-server"
    server_file.write_bytes(b"DEX")

    device = make_device()
    stop = threading.Event()

    def fake_subprocess_run(cmd, *args, **kwargs):
        mock_res = MagicMock()
        mock_res.returncode = 0
        if "forward" in cmd and "tcp:0" in cmd:
            mock_res.stdout = "not_a_port\n"
        return mock_res

    with patch("subprocess.run", side_effect=fake_subprocess_run):
        with pytest.raises(ScrcpyVideoError, match="Не удалось разобрать выделенный порт"):
            list(iter_h264_packets("adb", device, server_file, stop))


def test_iter_h264_packets_server_crashes_on_startup(tmp_path: Path):
    """Если процесс сервера упал до успешного подключения к сокету, выбрасывается понятная ошибка."""
    server_file = tmp_path / "scrcpy-server"
    server_file.write_bytes(b"DEX")

    device = make_device()
    stop = threading.Event()

    def fake_subprocess_run(cmd, *args, **kwargs):
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "50007\n"
        return mock_res

    mock_proc = MagicMock()
    mock_proc.poll.return_value = 1
    mock_proc.returncode = 1
    mock_proc.stdout = None
    mock_proc.stderr = io.BytesIO(b"Incompatible version 5.0.1")

    with (
        patch("subprocess.run", side_effect=fake_subprocess_run),
        patch("subprocess.Popen", return_value=mock_proc),
    ):
        with pytest.raises(ScrcpyVideoError, match="Процесс сервера scrcpy неожиданно завершился"):
            list(iter_h264_packets("adb", device, server_file, stop))


def test_recv_exact_edge_cases():
    """Проверка граничных условий вспомогательной функции _recv_exact."""
    stop = threading.Event()
    sock = MockStreamSocket(b"12345")

    assert _recv_exact(sock, 0, stop) == b""

    with pytest.raises(ScrcpyVideoError, match="Запрошено отрицательное количество байт"):
        _recv_exact(sock, -1, stop)

    # Частичный EOF
    sock2 = MockStreamSocket(b"12")
    assert _recv_exact(sock2, 5, stop) is None


def test_peek_exact_edge_cases():
    """Проверка граничных условий вспомогательной функции _peek_exact."""
    stop = threading.Event()
    sock = MockStreamSocket(b"12345")

    assert _peek_exact(sock, 0, stop) == b""
    assert _peek_exact(sock, -1, stop) == b""

    # Просмотр без извлечения
    peeked = _peek_exact(sock, 4, stop)
    assert peeked == b"1234"
    # Данные не должны быть удалены из буфера сокета
    assert _recv_exact(sock, 5, stop) == b"12345"

    # Сокет без данных
    empty_sock = MockStreamSocket(b"")
    assert _peek_exact(empty_sock, 4, stop, timeout=0.05) is None
