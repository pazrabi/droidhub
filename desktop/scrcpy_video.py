"""Read-only USB H264 video transport for DroidHub Desktop using official scrcpy server v5.0.1."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import platform
import secrets
import shutil
import socket
import struct
import subprocess
import threading
import time

from desktop.adb_client import DeviceInfo, is_network_serial
from desktop.binaries import get_scrcpy_candidates, probe_version
from desktop.process_flags import NO_CONSOLE

# Explicit version pin for scrcpy server
SCRCPY_VERSION_PIN: str = "5.0.1"

# Stream metadata is disabled. Only 12-byte media headers are expected.
PACKET_FLAG_CONFIG: int = 1 << 62

# Annex B start codes
ANNEX_B_START_CODE_4: bytes = b"\x00\x00\x00\x01"
ANNEX_B_START_CODE_3: bytes = b"\x00\x00\x01"

# Limits and bounds
MAX_PACKET_SIZE: int = 1024 * 1024
SOCKET_RECV_CHUNK: int = 65536
SOCKET_TIMEOUT: float = 0.25
STARTUP_TIMEOUT: float = 8.0
PACKET_TIMEOUT: float = 3.0
SUBPROCESS_TIMEOUT: float = 5.0


class ScrcpyVideoError(Exception):
    """Base exception for scrcpy video transport errors."""


def _get_valid_positive_transport_id(device: DeviceInfo) -> str | None:
    """Returns transport_id as a string if valid and positive (> 0), else None."""
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


def validate_usb_device(device: DeviceInfo) -> None:
    """Strictly validates that the device is a ready physical USB device.

    Rejects network devices, emulators, offline devices, or devices without usb_info.
    """
    if device is None:
        raise ScrcpyVideoError("Device cannot be None")

    serial = getattr(device, "serial", None)
    if not serial or not isinstance(serial, str) or not serial.strip():
        raise ScrcpyVideoError("Device serial is missing or invalid")
    serial = serial.strip()

    if serial.startswith("emulator-"):
        raise ScrcpyVideoError(f"Emulator '{serial}' is not permitted")

    if getattr(device, "is_network", False) or is_network_serial(serial):
        raise ScrcpyVideoError(f"Network device '{serial}' is not permitted; physical USB required")

    usb_info = getattr(device, "usb_info", None)
    if not usb_info or not isinstance(usb_info, str) or not usb_info.strip():
        raise ScrcpyVideoError(f"Device '{serial}' has no usb_info; physical USB required")

    state = (getattr(device, "state", "") or "").lower().strip()
    if state != "device":
        raise ScrcpyVideoError(f"Device '{serial}' is not in 'device' state (current: '{state}')")

    if not getattr(device, "is_ready", False):
        raise ScrcpyVideoError(f"Device '{serial}' is not ready")


def find_preview_server(
    scrcpy_path: str | None = None,
    scrcpy_version: str | None = None,
) -> Path | None:
    """Finds local official scrcpy server file pinned strictly to version 5.0.1.

    Matches version token 5.0.1 only, looking for sibling scrcpy-server or scrcpy-server.jar.
    Does not perform any remote downloads.
    """
    # 1. If explicit scrcpy_version is passed, ensure it contains the exact token 5.0.1
    if scrcpy_version is not None:
        if SCRCPY_VERSION_PIN not in scrcpy_version.split():
            return None

    # Helper to check a directory for sibling server file
    def check_dir_for_server(directory: Path) -> Path | None:
        for name in ("scrcpy-server", "scrcpy-server.jar"):
            server_file = directory / name
            if server_file.is_file():
                return server_file
        return None

    # Helper to verify scrcpy version from an executable file
    def verify_executable_version(executable: Path) -> bool:
        if scrcpy_version is not None:
            return True
        ver_output = probe_version([str(executable), "--version"])
        if not ver_output or SCRCPY_VERSION_PIN not in ver_output.split():
            return False
        return True

    # 2. If a specific path was provided
    if scrcpy_path:
        # Resolve command name if needed
        resolved_str = shutil.which(scrcpy_path) or scrcpy_path
        p = Path(resolved_str).expanduser()

        # If pointing directly to server file
        if p.is_file() and p.name in ("scrcpy-server", "scrcpy-server.jar"):
            if scrcpy_version is not None:
                return p
            # Attempt to check sibling scrcpy binary for version verification
            scrcpy_bin_name = "scrcpy.exe" if platform.system() == "Windows" else "scrcpy"
            sibling_bin = p.parent / scrcpy_bin_name
            if sibling_bin.is_file() and verify_executable_version(sibling_bin):
                return p
            return None

        # If pointing to an executable
        if p.is_file():
            if not verify_executable_version(p):
                return None
            return check_dir_for_server(p.parent)

        # If pointing to a directory
        if p.is_dir():
            scrcpy_bin_name = "scrcpy.exe" if platform.system() == "Windows" else "scrcpy"
            cand_bin = p / scrcpy_bin_name
            if cand_bin.is_file():
                if not verify_executable_version(cand_bin):
                    return None
            elif scrcpy_version is None:
                return None
            return check_dir_for_server(p)

        return None

    # 3. If no path was provided, probe default candidates
    for cand in get_scrcpy_candidates():
        cand_path = Path(cand).expanduser()
        if not cand_path.is_file():
            continue
        if not verify_executable_version(cand_path):
            continue
        found = check_dir_for_server(cand_path.parent)
        if found:
            return found

    return None


def _recv_exact(
    sock: socket.socket,
    num_bytes: int,
    stop: threading.Event,
    proc: subprocess.Popen | None = None,
    chunk_size: int = SOCKET_RECV_CHUNK,
    *,
    deadline: float | None = None,
) -> bytes | None:
    """Reads exactly num_bytes from socket, bounded by chunk_size and responsive to stop."""
    if num_bytes < 0:
        raise ScrcpyVideoError(f"Negative byte count requested: {num_bytes}")
    if num_bytes == 0:
        return b""

    buf = bytearray()
    while len(buf) < num_bytes:
        if stop.is_set():
            return None
        if proc is not None and proc.poll() is not None:
            return None
        if deadline is not None and time.monotonic() >= deadline:
            return None

        to_read = min(num_bytes - len(buf), chunk_size)
        try:
            chunk = sock.recv(to_read)
        except (socket.timeout, TimeoutError):
            if stop.is_set():
                return None
            if proc is not None and proc.poll() is not None:
                return None
            continue
        except (ConnectionResetError, BrokenPipeError, OSError):
            return None

        if not chunk:
            # EOF
            return None
        if not buf and deadline is None:
            deadline = time.monotonic() + PACKET_TIMEOUT
        buf.extend(chunk)

    return bytes(buf)


def iter_h264_packets(
    adb_path: str,
    device: DeviceInfo,
    server_path: Path,
    stop: threading.Event,
) -> Iterator[bytes]:
    """Streams complete H264 access units in Annex B over read-only USB transport via scrcpy v5.0.1.

    Features:
    - Validates ready physical USB device only (rejects network / emulators).
    - Uses -t positive ADB transport ID when available, else -s serial.
    - Pushes a random unique owned remote JAR without overwriting native scrcpy files.
    - Sets up adb forward tcp:0 to a unique scrcpy_<scid> abstract socket.
    - Connects strictly to 127.0.0.1.
    - Runs scrcpy server v5.0.1 with max_size=288, max_fps=12, video_bit_rate=500000.
    - Checks dummy readiness byte, disables device meta, keeps frame meta.
    - Prepends config packets (SPS/PPS) to next media packet to yield complete Annex B access units.
    - Responsive to stop event even on static scenes via bounded timeouts.
    - Safe cleanup in finally: closes socket, removes adb forward, terminates server, removes remote jar.
    """
    if not isinstance(adb_path, str) or not adb_path.strip():
        raise ScrcpyVideoError("Invalid adb_path")

    server_path = Path(server_path).resolve()
    if not server_path.is_file():
        raise ScrcpyVideoError(f"Scrcpy server file does not exist: {server_path}")

    # Validate physical USB eligibility
    validate_usb_device(device)

    if stop.is_set():
        return

    # Choose addressing
    tid = _get_valid_positive_transport_id(device)
    if tid is not None:
        target_args = ["-t", tid]
    else:
        target_args = ["-s", device.serial]

    # Generate unique remote JAR name and unique SCID
    session_id = secrets.token_hex(8)
    remote_jar = f"/data/local/tmp/scrcpy_{session_id}.jar"

    scid_int = secrets.randbelow(0x7FFFFFFF) + 1
    scid_hex = f"{scid_int:08x}"
    abstract_socket = f"scrcpy_{scid_hex}"

    local_port: int | None = None
    proc: subprocess.Popen | None = None
    sock: socket.socket | None = None

    try:
        # 1. Push owned unique server JAR to device
        push_cmd = [adb_path, *target_args, "push", str(server_path), remote_jar]
        push_res = subprocess.run(
            push_cmd,
            capture_output=True,
            timeout=SUBPROCESS_TIMEOUT,
            check=False,
            **NO_CONSOLE,
        )
        if push_res.returncode != 0:
            err = (push_res.stderr or push_res.stdout or b"").decode(errors="replace").strip()
            raise ScrcpyVideoError(f"Failed to push scrcpy server JAR: {err}")

        if stop.is_set():
            return

        # 2. Setup forward tcp:0 -> localabstract:scrcpy_<scid>
        forward_cmd = [adb_path, *target_args, "forward", "tcp:0", f"localabstract:{abstract_socket}"]
        forward_res = subprocess.run(
            forward_cmd,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
            check=False,
            **NO_CONSOLE,
        )
        if forward_res.returncode != 0:
            err = (forward_res.stderr or forward_res.stdout or "").strip()
            raise ScrcpyVideoError(f"Failed to set up ADB port forwarding: {err}")

        try:
            local_port = int(forward_res.stdout.strip())
        except (ValueError, TypeError):
            raise ScrcpyVideoError(f"Could not parse allocated ADB forward port: {forward_res.stdout.strip()}")

        if not (1 <= local_port <= 65535):
            bad_port, local_port = local_port, None
            raise ScrcpyVideoError(f"Invalid forwarded port received: {bad_port}")

        if stop.is_set():
            return

        # 3. Launch scrcpy server process
        server_cmd = [
            adb_path,
            *target_args,
            "shell",
            f"CLASSPATH={remote_jar}",
            "app_process",
            "/",
            "com.genymobile.scrcpy.Server",
            SCRCPY_VERSION_PIN,
            f"scid={scid_hex}",
            "tunnel_forward=true",
            "audio=false",
            "control=false",
            "cleanup=true",
            "power_on=false",
            "max_size=288",
            "max_fps=12",
            "video_bit_rate=500000",
            "send_device_meta=false",
            "send_frame_meta=true",
            "send_dummy_byte=true",
            "send_stream_meta=false",
        ]

        proc = subprocess.Popen(
            server_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **NO_CONSOLE,
        )

        # 4. Connect strictly to 127.0.0.1:<local_port>
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline and not stop.is_set():
            if proc.poll() is not None:
                raise ScrcpyVideoError(
                    f"Scrcpy server process exited unexpectedly with code {proc.returncode}"
                )
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(SOCKET_TIMEOUT)
            try:
                s.connect(("127.0.0.1", local_port))
                # The host port can accept before its device socket is listening.
                dummy = _recv_exact(
                    s, 1, stop, proc=proc,
                    deadline=min(deadline, time.monotonic() + 1.0),
                )
                if dummy == b"\x00":
                    sock = s
                    break
            except OSError:
                pass
            finally:
                if sock is not s:
                    s.close()
            stop.wait(0.05)

        if stop.is_set():
            return

        if sock is None:
            raise ScrcpyVideoError("Failed to receive dummy readiness byte from scrcpy server")

        pending_config = bytearray()
        first_frame_deadline = time.monotonic() + STARTUP_TIMEOUT

        while not stop.is_set():
            header_bytes = _recv_exact(sock, 12, stop, proc=proc, deadline=first_frame_deadline)
            if header_bytes is None:
                break

            pts_and_flags, packet_size = struct.unpack(">QI", header_bytes)

            if packet_size == 0:
                raise ScrcpyVideoError("Empty scrcpy video packet")
            if packet_size > MAX_PACKET_SIZE:
                raise ScrcpyVideoError(f"Packet size {packet_size} exceeds maximum allowed {MAX_PACKET_SIZE}")
            if pts_and_flags >> 63:
                raise ScrcpyVideoError("Unexpected scrcpy session metadata")

            payload = _recv_exact(
                sock, packet_size, stop, proc=proc, deadline=time.monotonic() + PACKET_TIMEOUT
            )
            if payload is None:
                break

            if pts_and_flags & PACKET_FLAG_CONFIG:
                if len(pending_config) + len(payload) > MAX_PACKET_SIZE:
                    raise ScrcpyVideoError("Oversized scrcpy codec configuration")
                # Buffer config packet (SPS/PPS) to prepend to next media packet
                pending_config.extend(payload)
                continue

            # Media packet
            if pending_config:
                full_packet = bytes(pending_config) + payload
                pending_config.clear()
            else:
                full_packet = payload

            if not full_packet:
                continue

            # Ensure Annex B start code
            if not (full_packet.startswith(ANNEX_B_START_CODE_4) or full_packet.startswith(ANNEX_B_START_CODE_3)):
                full_packet = ANNEX_B_START_CODE_4 + full_packet

            first_frame_deadline = None
            yield full_packet

    finally:
        # Safe cleanup of all owned resources in reverse order
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

        if local_port is not None:
            try:
                subprocess.run(
                    [adb_path, *target_args, "forward", "--remove", f"tcp:{local_port}"],
                    capture_output=True,
                    timeout=SUBPROCESS_TIMEOUT,
                    check=False,
                    **NO_CONSOLE,
                )
            except Exception:
                pass

        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=1.0)
            except Exception:
                pass

        try:
            subprocess.run(
                [adb_path, *target_args, "shell", "rm", "-f", remote_jar],
                capture_output=True,
                timeout=SUBPROCESS_TIMEOUT,
                check=False,
                **NO_CONSOLE,
            )
        except Exception:
            pass
