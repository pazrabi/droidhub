"""Транспорт H.264 видео по USB только для чтения для DroidHub Desktop с использованием официального сервера scrcpy v5.0.1."""

from __future__ import annotations

from collections.abc import Iterator
import logging
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import socket
import struct
import subprocess
import threading
import time

from desktop.adb_client import DeviceInfo, is_network_serial
from desktop.binaries import get_scrcpy_candidates, probe_version

logger = logging.getLogger(__name__)

# Явная фиксация версии сервера scrcpy
SCRCPY_VERSION_PIN: str = "5.0.1"

# Флаги обрамления протокола (64-битный pts_and_flags в scrcpy v4+)
PACKET_FLAG_SESSION: int = 1 << 63
PACKET_FLAG_CONFIG: int = 1 << 62
PACKET_FLAG_KEY_FRAME: int = 1 << 61
PACKET_PTS_MASK: int = (1 << 61) - 1

# Стартовые коды Annex B
ANNEX_B_START_CODE_4: bytes = b"\x00\x00\x00\x01"
ANNEX_B_START_CODE_3: bytes = b"\x00\x00\x01"

# Лимиты и таймауты
MAX_PACKET_SIZE: int = 16 * 1024 * 1024  # Ограничение 16 МиБ на выделение одного пакета
SOCKET_RECV_CHUNK: int = 65536
SOCKET_TIMEOUT: float = 0.25
STARTUP_TIMEOUT: float = 5.0
SUBPROCESS_TIMEOUT: float = 5.0


class ScrcpyVideoError(Exception):
    """Базовое исключение для ошибок видеотранспорта scrcpy."""


def _get_valid_positive_transport_id(device: DeviceInfo) -> str | None:
    """Возвращает transport_id в виде строки, если он валиден и положителен (> 0), иначе None."""
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
    """Строго проверяет, что устройство является готовым физическим USB-устройством.

    Отклоняет сетевые устройства, эмуляторы, оффлайн-устройства или устройства без usb_info.
    """
    if device is None:
        raise ScrcpyVideoError("Устройство не может быть None")

    serial = getattr(device, "serial", None)
    if not serial or not isinstance(serial, str) or not serial.strip():
        raise ScrcpyVideoError("Серийный номер устройства отсутствует или некорректен")
    serial = serial.strip()

    if serial.startswith("emulator-"):
        raise ScrcpyVideoError(f"Эмулятор '{serial}' запрещён")

    if getattr(device, "is_network", False) or is_network_serial(serial):
        raise ScrcpyVideoError(
            f"Сетевое устройство '{serial}' запрещено; требуется физическое USB-подключение"
        )

    usb_info = getattr(device, "usb_info", None)
    if not usb_info or not isinstance(usb_info, str) or not usb_info.strip():
        raise ScrcpyVideoError(
            f"Устройство '{serial}' не имеет usb_info; требуется физическое USB-подключение"
        )

    state = (getattr(device, "state", "") or "").lower().strip()
    if state != "device":
        raise ScrcpyVideoError(
            f"Устройство '{serial}' не в состоянии 'device' (текущее: '{state}')"
        )

    if not getattr(device, "is_ready", False):
        raise ScrcpyVideoError(f"Устройство '{serial}' не готово к работе")


def _match_version_token(ver_str: str | None) -> bool:
    """Проверяет точное совпадение токена версии 5.0.1 без посторонних цифр или символов версии."""
    if not ver_str:
        return False
    return bool(re.search(r"(?<![0-9a-zA-Z.])v?5\.0\.1(?![0-9a-zA-Z.])", ver_str))


def find_preview_server(
    scrcpy_path: str | None = None,
    scrcpy_version: str | None = None,
) -> Path | None:
    """Находит локальный официальный файл scrcpy-server, строго привязанный к версии 5.0.1.

    Сопоставляет только токен версии 5.0.1, ищет соседний scrcpy-server или scrcpy-server.jar.
    Не выполняет удалённых загрузок.
    """
    # 1. Если передана явная версия scrcpy_version, проверяем строгое совпадение с токеном 5.0.1
    if scrcpy_version is not None:
        if not _match_version_token(scrcpy_version):
            return None

    # Помощник для проверки наличия файла сервера в каталоге
    def check_dir_for_server(directory: Path) -> Path | None:
        for name in ("scrcpy-server", "scrcpy-server.jar"):
            server_file = directory / name
            if server_file.is_file():
                return server_file
        return None

    # Помощник для проверки версии scrcpy из исполняемого файла
    def verify_executable_version(executable: Path) -> bool:
        if scrcpy_version is not None:
            return True
        ver_output = probe_version([str(executable), "--version"])
        if not ver_output or not _match_version_token(ver_output):
            return False
        return True

    # 2. Если передан конкретный путь
    if scrcpy_path:
        # Разрешаем имя команды через PATH при необходимости
        resolved_str = shutil.which(scrcpy_path) or scrcpy_path
        p = Path(resolved_str).expanduser()

        # Если путь указывает непосредственно на файл сервера
        if p.is_file() and p.name in ("scrcpy-server", "scrcpy-server.jar"):
            if scrcpy_version is not None:
                return p
            # Пробуем проверить соседний исполняемый файл scrcpy
            for cand_name in ("scrcpy", "scrcpy.exe"):
                sibling_bin = p.parent / cand_name
                if sibling_bin.is_file() and verify_executable_version(sibling_bin):
                    return p
            return None

        # Если путь указывает на исполняемый файл
        if p.is_file():
            if not verify_executable_version(p):
                return None
            return check_dir_for_server(p.parent)

        # Если путь указывает на каталог
        if p.is_dir():
            cand_bin = None
            for cand_name in ("scrcpy", "scrcpy.exe"):
                test_bin = p / cand_name
                if test_bin.is_file():
                    cand_bin = test_bin
                    break

            if cand_bin is not None:
                if not verify_executable_version(cand_bin):
                    return None
            elif scrcpy_version is None:
                return None
            return check_dir_for_server(p)

        return None

    # 3. Если путь не передан, проверяем стандартные кандидаты
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
) -> bytes | None:
    """Считывает ровно num_bytes из сокета, с ограничением chunk_size и проверкой флага остановки."""
    if num_bytes < 0:
        raise ScrcpyVideoError(f"Запрошено отрицательное количество байт: {num_bytes}")
    if num_bytes == 0:
        return b""

    buf = bytearray()
    while len(buf) < num_bytes:
        if stop.is_set():
            return None
        if proc is not None and proc.poll() is not None:
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
        buf.extend(chunk)

    return bytes(buf)


def _peek_exact(
    sock: socket.socket,
    num_bytes: int,
    stop: threading.Event,
    proc: subprocess.Popen | None = None,
    timeout: float = 1.0,
) -> bytes | None:
    """Просматривает ровно num_bytes из сокета без их извлечения (MSG_PEEK)."""
    if num_bytes <= 0:
        return b""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not stop.is_set():
        if proc is not None and proc.poll() is not None:
            return None
        try:
            peek_data = sock.recv(num_bytes, socket.MSG_PEEK)
        except (socket.timeout, TimeoutError):
            if stop.is_set():
                return None
            if proc is not None and proc.poll() is not None:
                return None
            continue
        except (ConnectionResetError, BrokenPipeError, OSError, AttributeError, TypeError):
            return None

        if not peek_data:
            return None
        if len(peek_data) >= num_bytes:
            return peek_data[:num_bytes]
        time.sleep(0.01)

    return None


def iter_h264_packets(
    adb_path: str,
    device: DeviceInfo,
    server_path: Path,
    stop: threading.Event,
) -> Iterator[bytes]:
    """Передаёт полные единицы доступа H.264 в формате Annex B через USB-транспорт только для чтения scrcpy v5.0.1.

    Особенности:
    - Проверяет только готовое физическое USB-устройство (отклоняет сеть и эмуляторы).
    - Использует положительный ADB transport ID (-t), если он доступен, иначе -s serial.
    - Загружает случайный уникальный собственный JAR-файл сервера без перезаписи системных файлов scrcpy.
    - Настраивает перенаправление adb forward tcp:0 на уникальный абстрактный сокет scrcpy_<scid>.
    - Подключается строго к 127.0.0.1.
    - Запускает сервер scrcpy v5.0.1 с cleanup=false, max_size=288, max_fps=12, video_bit_rate=500000.
    - Обрабатывает проверочный байт готовности, отключает метаданные устройства, сохраняет метаданные кадров.
    - Добавляет конфигурационные пакеты (SPS/PPS) перед следующим медиа-пакетом для формирования полных единиц доступа Annex B.
    - Корректно реагирует на событие остановки stop даже на статичных сценах благодаря ограниченным таймаутам.
    - Безопасная очистка в блоке finally: закрывает сокет, удаляет forward, завершает сервер, удаляет remote jar.
    """
    if not isinstance(adb_path, str) or not adb_path.strip():
        raise ScrcpyVideoError("Некорректный путь к adb")

    server_path = Path(server_path).resolve()
    if not server_path.is_file():
        raise ScrcpyVideoError(f"Файл сервера scrcpy не найден: {server_path}")

    # Проверка готовности физического USB-устройства
    validate_usb_device(device)

    if stop.is_set():
        return

    # Выбор способа адресации ADB: положительный transport_id или серийный номер
    tid = _get_valid_positive_transport_id(device)
    if tid is not None:
        target_args = ["-t", tid]
    else:
        target_args = ["-s", device.serial]

    # Генерация уникального имени удалённого JAR и уникального SCID
    session_id = secrets.token_hex(8)
    remote_jar = f"/data/local/tmp/scrcpy_{session_id}.jar"

    scid_int = secrets.randbelow(0x7FFFFFFF) + 1
    scid_hex = f"{scid_int:08x}"
    abstract_socket = f"scrcpy_{scid_hex}"

    local_port: int | None = None
    proc: subprocess.Popen | None = None
    sock: socket.socket | None = None

    try:
        # 1. Загрузка уникального JAR сервера на устройство
        push_cmd = [adb_path, *target_args, "push", str(server_path), remote_jar]
        push_res = subprocess.run(
            push_cmd,
            capture_output=True,
            timeout=SUBPROCESS_TIMEOUT,
            check=False,
        )
        if push_res.returncode != 0:
            err = (push_res.stderr or push_res.stdout or b"").decode(errors="replace").strip()
            raise ScrcpyVideoError(f"Не удалось загрузить серверный JAR scrcpy: {err}")

        if stop.is_set():
            return

        # 2. Настройка перенаправления tcp:0 -> localabstract:scrcpy_<scid>
        forward_cmd = [adb_path, *target_args, "forward", "tcp:0", f"localabstract:{abstract_socket}"]
        forward_res = subprocess.run(
            forward_cmd,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
            check=False,
        )
        if forward_res.returncode != 0:
            err = (forward_res.stderr or forward_res.stdout or "").strip()
            raise ScrcpyVideoError(f"Не удалось настроить перенаправление портов ADB: {err}")

        try:
            local_port = int(forward_res.stdout.strip())
        except (ValueError, TypeError):
            raise ScrcpyVideoError(
                f"Не удалось разобрать выделенный порт перенаправления ADB: {forward_res.stdout.strip()}"
            )

        if not (1 <= local_port <= 65535):
            raise ScrcpyVideoError(f"Получен некорректный перенаправленный порт: {local_port}")

        if stop.is_set():
            return

        # 3. Запуск процесса сервера scrcpy
        # Используем stdout=subprocess.DEVNULL для предотвращения зависания при заполнении системного буфера пайпа
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
            "cleanup=false",
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
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        # 4. Подключение строго к 127.0.0.1:<local_port>
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline and not stop.is_set():
            if proc.poll() is not None:
                err = b""
                if proc.stderr:
                    try:
                        err = proc.stderr.read()
                    except Exception:
                        pass
                raise ScrcpyVideoError(
                    f"Процесс сервера scrcpy неожиданно завершился с кодом {proc.returncode}: {err.decode(errors='replace').strip()}"
                )
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(SOCKET_TIMEOUT)
            try:
                s.connect(("127.0.0.1", local_port))
                sock = s
                break
            except (OSError, ConnectionRefusedError):
                s.close()
                time.sleep(0.05)

        if stop.is_set():
            return

        if sock is None:
            raise ScrcpyVideoError("Истекло время ожидания подключения к серверу scrcpy на 127.0.0.1")

        # 5. Обработка проверочного байта готовности (dummy byte)
        dummy_byte = _recv_exact(sock, 1, stop, proc=proc)
        if dummy_byte is None:
            if stop.is_set():
                return
            raise ScrcpyVideoError("Не удалось получить проверочный байт готовности от сервера scrcpy")

        # 6. Проверка метаданных потока (stream metadata)
        # В scrcpy v5.0.1 при send_stream_meta=true передаётся идентификатор кодека (4 байта "h264")
        # и начальный пакет сессии (12 байт), всего ровно 16 байт.
        try:
            peek_data = _peek_exact(sock, 4, stop, proc=proc)
            if peek_data == b"h264":
                _recv_exact(sock, 16, stop, proc=proc)
        except (AttributeError, OSError, socket.error):
            pass

        # 7. Чтение обрамлённых пакетов потока
        pending_config = bytearray()

        while not stop.is_set():
            header_bytes = _recv_exact(sock, 12, stop, proc=proc)
            if header_bytes is None:
                break

            pts_and_flags, packet_size = struct.unpack(">QI", header_bytes)

            is_session = bool(pts_and_flags & PACKET_FLAG_SESSION)
            if is_session:
                # Пакет сессии scrcpy v5.0.1 имеет фиксированный размер ровно 12 байт целиком
                # (байты 0–3: флаги сессии 0x80 и padding; байты 4–7: ширина u32; байты 8–11: высота u32)
                # и не содержит полезной нагрузки (payload). Не читаем полезную нагрузку из сокета.
                continue

            if packet_size > MAX_PACKET_SIZE:
                raise ScrcpyVideoError(
                    f"Размер пакета {packet_size} превышает максимально допустимый {MAX_PACKET_SIZE}"
                )

            payload = _recv_exact(sock, packet_size, stop, proc=proc) if packet_size > 0 else b""
            if payload is None and packet_size > 0:
                break

            is_config = bool(pts_and_flags & PACKET_FLAG_CONFIG)

            if is_config:
                # Накапливаем конфигурационные данные (SPS/PPS), чтобы прикрепить к следующему медиа-пакету
                pending_config.extend(payload)
                continue

            # Медиа-пакет (IDR или P-кадр)
            if pending_config:
                full_packet = bytes(pending_config) + payload
                pending_config.clear()
            else:
                full_packet = payload

            if not full_packet:
                continue

            # Проверяем наличие стартового кода Annex B (0x00000001 или 0x000001)
            if not (full_packet.startswith(ANNEX_B_START_CODE_4) or full_packet.startswith(ANNEX_B_START_CODE_3)):
                full_packet = ANNEX_B_START_CODE_4 + full_packet

            yield full_packet

    finally:
        # Безопасная очистка всех занятых ресурсов в обратном порядке
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
            finally:
                if proc.stdout is not None:
                    try:
                        proc.stdout.close()
                    except Exception:
                        pass
                if proc.stderr is not None:
                    try:
                        proc.stderr.close()
                    except Exception:
                        pass

        try:
            subprocess.run(
                [adb_path, *target_args, "shell", "rm", "-f", remote_jar],
                capture_output=True,
                timeout=SUBPROCESS_TIMEOUT,
                check=False,
            )
        except Exception:
            pass
