"""Unit tests for desktop/scrcpy_video.py (read-only USB H264 transport for scrcpy v5.0.1)."""

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
    _recv_exact,
    find_preview_server,
    iter_h264_packets,
    validate_usb_device,
)


# ---------------------------------------------------------------------------
# Test Fixtures & Helpers
# ---------------------------------------------------------------------------


def make_device(
    serial: str = "USB_SERIAL_123",
    state: str = "device",
    usb_info: str | None = "1-1.2",
    is_network: bool = False,
    is_ready: bool = True,
    transport_id: str | None = "42",
) -> DeviceInfo:
    """Creates a DeviceInfo instance for testing."""
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
    """Packs a 12-byte scrcpy frame header followed by the payload."""
    header = struct.pack(">QI", flags_and_pts, len(payload))
    return header + payload


class MockStreamSocket:
    """Simulates a TCP socket streaming synthetic scrcpy server data."""

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
# Tests: Device Validation & Addressing
# ---------------------------------------------------------------------------


def test_validate_usb_device_success():
    dev = make_device()
    validate_usb_device(dev)


def test_validate_usb_device_none():
    with pytest.raises(ScrcpyVideoError, match="Device cannot be None"):
        validate_usb_device(None)  # type: ignore


def test_validate_usb_device_empty_serial():
    dev = make_device(serial="")
    with pytest.raises(ScrcpyVideoError, match="Device serial is missing"):
        validate_usb_device(dev)


def test_validate_usb_device_reject_network():
    dev = make_device(is_network=True)
    with pytest.raises(ScrcpyVideoError, match="Network device.*not permitted"):
        validate_usb_device(dev)


def test_validate_usb_device_reject_network_serial():
    dev = make_device(serial="192.168.1.100:5555")
    with pytest.raises(ScrcpyVideoError, match="Network device.*not permitted"):
        validate_usb_device(dev)


def test_validate_usb_device_reject_emulator():
    dev = make_device(serial="emulator-5554")
    with pytest.raises(ScrcpyVideoError, match="Emulator.*is not permitted"):
        validate_usb_device(dev)


def test_validate_usb_device_reject_missing_usb_info():
    dev = make_device(usb_info=None)
    with pytest.raises(ScrcpyVideoError, match="has no usb_info"):
        validate_usb_device(dev)

    dev_empty = make_device(usb_info="   ")
    with pytest.raises(ScrcpyVideoError, match="has no usb_info"):
        validate_usb_device(dev_empty)


def test_validate_usb_device_reject_bad_state():
    dev_offline = make_device(state="offline", is_ready=False)
    with pytest.raises(ScrcpyVideoError, match="not in 'device' state"):
        validate_usb_device(dev_offline)

    dev_not_ready = make_device(state="device", is_ready=False)
    with pytest.raises(ScrcpyVideoError, match="is not ready"):
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
# Tests: find_preview_server
# ---------------------------------------------------------------------------


def test_find_preview_server_explicit_version_mismatch():
    assert find_preview_server("/usr/bin/scrcpy", "5.0.0") is None
    assert find_preview_server("/usr/bin/scrcpy", "4.0") is None
    assert find_preview_server("/usr/bin/scrcpy", "5.0.10") is None


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


def test_find_preview_server_not_found(tmp_path: Path):
    scrcpy_bin = tmp_path / "scrcpy"
    scrcpy_bin.write_text("#!/bin/sh\n")
    # No sibling server file
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
# Tests: iter_h264_packets Transport & Protocol
# ---------------------------------------------------------------------------


def test_iter_h264_packets_full_protocol_stream(tmp_path: Path):
    """Verifies complete streaming pipeline:

    - Positive transport_id (-t 42) used for all subprocess calls
    - Unique owned remote jar pushed and cleaned up
    - tcp:0 forwarded to unique scrcpy_<scid>
    - Connects to 127.0.0.1
    - Correct v5.0.1 server arguments (max_size=288, max_fps=12, video_bit_rate=500000)
    - Readiness dummy byte handled
    - Config packets prepended to next media packet
    - Complete Annex B access units yielded
    """
    server_file = tmp_path / "scrcpy-server"
    server_file.write_bytes(b"DEX_OR_JAR_DATA")

    device = make_device(serial="DEVICE1", transport_id="42")
    stop = threading.Event()

    # Synthetic stream:
    # 1. Dummy byte: 0x00
    # 2. Config packet (SPS/PPS): PACKET_FLAG_CONFIG, 10 bytes
    # 3. Media packet (IDR): keyframe flag + pts 1000, 20 bytes
    # 4. Media packet (P-frame): pts 2000, 15 bytes
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

    with (
        patch("subprocess.run", side_effect=fake_subprocess_run),
        patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
        patch("socket.socket", return_value=mock_sock),
    ):
        packets = list(iter_h264_packets("adb", device, server_file, stop))

    # Assertions on yielded packets
    assert len(packets) == 2
    # Packet 1 must have config prepended to IDR:
    assert packets[0] == config_payload + idr_payload
    assert packets[0].startswith(ANNEX_B_START_CODE_4)
    # Packet 2 must be the P-frame:
    assert packets[1] == p_payload
    assert packets[1].startswith(ANNEX_B_START_CODE_4)

    # Verify connection was made strictly to 127.0.0.1:48123
    assert mock_sock.connected_address == ("127.0.0.1", 48123)

    # Verify subprocess commands
    # 1. Push command used -t 42
    push_call = calls_run[0]
    assert push_call[:4] == ["adb", "-t", "42", "push"]
    assert push_call[4] == str(server_file)
    remote_jar = push_call[5]
    assert remote_jar.startswith("/data/local/tmp/scrcpy_")
    assert remote_jar.endswith(".jar")

    # 2. Forward command used -t 42 and tcp:0
    fwd_call = calls_run[1]
    assert fwd_call[:5] == ["adb", "-t", "42", "forward", "tcp:0"]
    assert fwd_call[5].startswith("localabstract:scrcpy_")

    # 3. Popen command started scrcpy server v5.0.1
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

    # 4. Finally cleanup: forward removed, child reaped, remote jar removed
    fwd_rm_call = [c for c in calls_run if "forward" in c and "--remove" in c][0]
    assert fwd_rm_call == ["adb", "-t", "42", "forward", "--remove", "tcp:48123"]

    rm_jar_call = [c for c in calls_run if "shell" in c and "rm" in c][0]
    assert rm_jar_call == ["adb", "-t", "42", "shell", "rm", "-f", remote_jar]

    mock_proc.terminate.assert_called_once()


def test_iter_h264_packets_serial_fallback_when_no_transport_id(tmp_path: Path):
    """When transport_id is missing/invalid, falls back to -s <serial>."""
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
    # Check that -s SERIAL_XYZ was used
    assert calls[0][:4] == ["adb", "-s", "SERIAL_XYZ", "push"]
    assert calls[1][:5] == ["adb", "-s", "SERIAL_XYZ", "forward", "tcp:0"]
    server_cmd = mock_popen.call_args[0][0]
    assert server_cmd[:4] == ["adb", "-s", "SERIAL_XYZ", "shell"]


def test_iter_h264_packets_parses_session_packet_and_stream_meta(tmp_path: Path):
    """Verifies that:

    - Stream metadata (b"h264" + width + height) is safely consumed if present
    - Session packets (PACKET_FLAG_SESSION) are discarded and not yielded as media
    """
    server_file = tmp_path / "scrcpy-server"
    server_file.write_bytes(b"DEX")

    device = make_device()
    stop = threading.Event()

    dummy = b"\x00"
    stream_meta = b"h264" + struct.pack(">II", 288, 512)
    session_payload = b"\x00" * 16
    session_packet = pack_frame(PACKET_FLAG_SESSION, session_payload)
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


def test_iter_h264_packets_ensures_annex_b_start_code(tmp_path: Path):
    """If media packet does not start with 00 00 00 01 or 00 00 01, start code is prepended."""
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
    """Verifies that generator exits promptly when stop.set() is called even during static scenes."""
    server_file = tmp_path / "scrcpy-server"
    server_file.write_bytes(b"DEX")

    device = make_device()
    stop = threading.Event()

    dummy = b"\x00"
    # timeout_on_empty will simulate static scene where no new frames arrive
    mock_sock = MockStreamSocket(dummy, timeout_on_empty=True)

    def fake_subprocess_run(cmd, *args, **kwargs):
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "50004\n"
        return mock_res

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    # Trigger stop event after short delay
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
    """Rejects packets exceeding MAX_PACKET_SIZE to bound memory allocation."""
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
        with pytest.raises(ScrcpyVideoError, match="exceeds maximum"):
            list(iter_h264_packets("adb", device, server_file, stop))


def test_iter_h264_packets_safe_cleanup_on_push_failure(tmp_path: Path):
    """If push fails, raises error and cleans up without leaking forward or child."""
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
        with pytest.raises(ScrcpyVideoError, match="Failed to push scrcpy server JAR"):
            list(iter_h264_packets("adb", device, server_file, stop))

    # Finally block must attempt to rm remote jar
    assert any("rm" in c for c in calls)


def test_iter_h264_packets_dummy_byte_missing_fails(tmp_path: Path):
    """If server closes connection without sending dummy readiness byte, raises error."""
    server_file = tmp_path / "scrcpy-server"
    server_file.write_bytes(b"DEX")

    device = make_device()
    stop = threading.Event()

    mock_sock = MockStreamSocket(b"")  # Empty stream

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
        with pytest.raises(ScrcpyVideoError, match="Failed to receive dummy readiness byte"):
            list(iter_h264_packets("adb", device, server_file, stop))


def test_iter_h264_packets_already_stopped(tmp_path: Path):
    """If stop is set before starting, generator exits immediately without running commands."""
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
    """Checks argument validation for adb_path and server_path."""
    device = make_device()
    stop = threading.Event()

    with pytest.raises(ScrcpyVideoError, match="Invalid adb_path"):
        list(iter_h264_packets("", device, tmp_path / "server", stop))

    with pytest.raises(ScrcpyVideoError, match="Scrcpy server file does not exist"):
        list(iter_h264_packets("adb", device, tmp_path / "non_existent_server", stop))


def test_iter_h264_packets_bad_forward_port(tmp_path: Path):
    """If adb forward returns non-integer or out-of-range port, raises error."""
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
        with pytest.raises(ScrcpyVideoError, match="Could not parse allocated ADB forward port"):
            list(iter_h264_packets("adb", device, server_file, stop))


def test_iter_h264_packets_server_crashes_on_startup(tmp_path: Path):
    """If server process exits prematurely before socket connection, raises error."""
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
    mock_proc.stderr = io.BytesIO(b"Incompatible version 5.0.1")

    with (
        patch("subprocess.run", side_effect=fake_subprocess_run),
        patch("subprocess.Popen", return_value=mock_proc),
    ):
        with pytest.raises(ScrcpyVideoError, match="Scrcpy server process exited unexpectedly"):
            list(iter_h264_packets("adb", device, server_file, stop))


def test_recv_exact_edge_cases():
    """Checks bounds and edge cases of _recv_exact."""
    stop = threading.Event()
    sock = MockStreamSocket(b"12345")

    assert _recv_exact(sock, 0, stop) == b""

    with pytest.raises(ScrcpyVideoError, match="Negative byte count"):
        _recv_exact(sock, -1, stop)

    # Partial EOF
    sock2 = MockStreamSocket(b"12")
    assert _recv_exact(sock2, 5, stop) is None

