from __future__ import annotations

import asyncio
import re
from typing import Any

from hub.config import get_settings


class AdbError(Exception):
    """Базовая ошибка выполнения команды ADB."""
    pass


class DeviceOffline(AdbError):
    """Устройство оффлайн или не найдено."""
    pass


class AdbTimeout(AdbError):
    """Превышено время ожидания команды ADB."""
    pass


ADB_OFFLINE_PATTERN = re.compile(
    r"error:\s*(?:device\s+('.+?'\s+)?not found|device\s+offline|no devices(?:/emulators found)?)",
    re.IGNORECASE,
)


def _get_adb_path() -> str:
    """Возвращает путь к adb из настроек get_settings().adb_path."""
    try:
        settings = get_settings()
        return getattr(settings, "adb_path", "adb") or "adb"
    except Exception:
        return "adb"


async def run_adb(
    args: list[str],
    *,
    serial: str | None = None,
    timeout: float = 15.0,
    binary: bool = False,
) -> str | bytes:
    """
    Запускает команду adb [-s serial] args через asyncio subprocess.
    При ненулевом коде возврата выбрасывает AdbError или DeviceOffline.
    При превышении таймаута убивает процесс и выбрасывает AdbTimeout.
    """
    adb_path = _get_adb_path()
    cmd: list[str] = [adb_path]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(str(a) for a in args)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise AdbError(f"Исполняемый файл ADB не найден по пути '{cmd[0]}': {exc}") from exc
    except Exception as exc:
        raise AdbError(f"Не удалось запустить процесс ADB: {exc}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:
            pass
        raise AdbTimeout(f"Превышено время ожидания команды ADB ({timeout} с)")
    except asyncio.CancelledError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:
            pass
        raise

    target_str = f" '{serial}'" if serial else ""

    if proc.returncode != 0:
        err_text = stderr.decode("utf-8", errors="replace").strip()
        out_text = stdout.decode("utf-8", errors="replace").strip()
        msg = err_text or out_text or f"Код возврата {proc.returncode}"

        if ADB_OFFLINE_PATTERN.search(msg):
            raise DeviceOffline(f"Устройство{target_str} оффлайн или не найдено: {msg}")
        if out_text and ADB_OFFLINE_PATTERN.search(out_text):
            raise DeviceOffline(f"Устройство{target_str} оффлайн или не найдено: {out_text}")

        raise AdbError(f"Ошибка выполнения ADB (код {proc.returncode}): {msg}")

    if binary:
        err_text = stderr.decode("utf-8", errors="replace").strip()
        if err_text and ADB_OFFLINE_PATTERN.search(err_text):
            raise DeviceOffline(f"Устройство{target_str} оффлайн или не найдено: {err_text}")
        if stdout.lstrip().startswith((b"error:", b"adb: error:")):
            out_preview = stdout[:512].decode("utf-8", errors="replace").strip()
            if ADB_OFFLINE_PATTERN.search(out_preview):
                raise DeviceOffline(f"Устройство{target_str} оффлайн или не найдено: {out_preview}")
        return stdout

    text = stdout.decode("utf-8", errors="replace")
    err_text = stderr.decode("utf-8", errors="replace").strip()

    if err_text and ADB_OFFLINE_PATTERN.search(err_text):
        raise DeviceOffline(f"Устройство{target_str} оффлайн или не найдено: {err_text}")

    stripped_text = text.strip()
    if (stripped_text.startswith("error:") or stripped_text.startswith("adb: error:")) and ADB_OFFLINE_PATTERN.search(stripped_text):
        raise DeviceOffline(f"Устройство{target_str} оффлайн или не найдено: {stripped_text}")

    return text


async def adb_shell(serial: str | None, cmd: str, timeout: float = 15.0) -> str:
    """Выполняет команду shell на устройстве через adb."""
    res = await run_adb(["shell", cmd], serial=serial, timeout=timeout, binary=False)
    if isinstance(res, bytes):
        return res.decode("utf-8", errors="replace")
    return res


async def connect(host: str, port: int | str = 5555, timeout: float = 10.0) -> bool:
    """
    Подключается к устройству по TCP (adb connect host:port).
    Возвращает True при успешном подключении, False при ошибке или отказе.
    """
    target = f"{host}:{port}"
    try:
        out = await run_adb(["connect", target], timeout=timeout)
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        out_lower = out.lower()
        if "connected to" in out_lower:
            return not ("failed" in out_lower or "cannot" in out_lower or "unable" in out_lower)
        return False
    except (AdbError, AdbTimeout):
        return False


__all__ = [
    "AdbError",
    "DeviceOffline",
    "AdbTimeout",
    "run_adb",
    "adb_shell",
    "connect",
]
