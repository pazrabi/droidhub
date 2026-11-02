from __future__ import annotations

import asyncio
import base64
from io import BytesIO
import os
import re
import shlex
import tempfile
import time
from typing import Any, Awaitable, Callable, TypeVar
import xml.etree.ElementTree as ET

from PIL import Image

T = TypeVar("T")

try:
    from hub.audit import get_audit
except (ImportError, ModuleNotFoundError):
    get_audit = None  # type: ignore

try:
    from hub.config import get_settings
except (ImportError, ModuleNotFoundError):
    get_settings = None  # type: ignore

try:
    from hub.devices import adb
    from hub.devices.adb import (
        AdbError,
        AdbTimeout,
        DeviceOffline,
        adb_shell,
        connect,
        run_adb,
    )
except (ImportError, ModuleNotFoundError):
    adb = None  # type: ignore
    adb_shell = None  # type: ignore
    run_adb = None  # type: ignore
    connect = None  # type: ignore
    class AdbError(Exception): pass  # type: ignore
    class DeviceOffline(AdbError): pass  # type: ignore
    class AdbTimeout(AdbError): pass  # type: ignore

_manager: Any = None
audit: Any = None  # Хук для тестов или прямого переопределения аудита

KEY_CODES: dict[str, str] = {
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "recents": "KEYCODE_APP_SWITCH",
    "app_switch": "KEYCODE_APP_SWITCH",
    "enter": "KEYCODE_ENTER",
    "power": "KEYCODE_POWER",
    "volume_up": "KEYCODE_VOLUME_UP",
    "volume_down": "KEYCODE_VOLUME_DOWN",
    "volume": "KEYCODE_VOLUME_UP",
}

_SHELL_UNSAFE_CHARS = set(';&|`$"\'()<>\\!*?#~=\n\r\t')

# Блокировки и задачи для скриншотов по устройствам
_screenshot_locks: dict[str, asyncio.Lock] = {}
_screenshot_inflight: dict[str, asyncio.Task[bytes]] = {}


def _get_screenshot_lock(device_id: str) -> asyncio.Lock:
    """Возвращает персональный asyncio.Lock для устройства."""
    if device_id not in _screenshot_locks:
        _screenshot_locks[device_id] = asyncio.Lock()
    return _screenshot_locks[device_id]


def set_manager(manager: Any) -> None:
    """Устанавливает глобальный менеджер устройств."""
    global _manager
    _manager = manager


def get_manager() -> Any:
    """Возвращает текущий менеджер устройств."""
    global _manager
    if _manager is None:
        raise RuntimeError("Менеджер устройств не установлен. Вызовите set_manager().")
    return _manager


def _get_serial(device_id: str) -> str:
    """Получает serial (host:port) для устройства через DeviceManager."""
    mgr = get_manager()
    if hasattr(mgr, "serial"):
        return mgr.serial(device_id)
    if hasattr(mgr, "get"):
        dev = mgr.get(device_id)
        return f"{dev.host}:{dev.port}"
    return str(device_id)


def _is_offline_error(exc: BaseException) -> bool:
    """Проверяет, указывает ли исключение на оффлайн/потерю соединения с устройством."""
    if isinstance(exc, DeviceOffline):
        return True
    if isinstance(exc, (KeyError, ValueError, PermissionError)):
        return False
    msg = str(exc).lower()
    if "device" in msg and "not found" in msg:
        return True
    offline_keywords = (
        "offline",
        "оффлайн",
        "нет соединения",
        "no devices",
        "connection refused",
        "connection reset",
        "not connected",
    )
    return any(k in msg for k in offline_keywords)


async def _reconnect_for_device(device_id: str) -> bool:
    """Вызывает adb.connect(host, port) для устройства через менеджер."""
    try:
        mgr = get_manager()
        dev = None
        if hasattr(mgr, "get"):
            try:
                dev = mgr.get(device_id)
            except Exception:
                pass

        host: str | None = getattr(dev, "host", None) if dev else None
        port: int = getattr(dev, "port", 5555) if dev else 5555

        if not host and hasattr(mgr, "serial"):
            try:
                s = mgr.serial(device_id)
                if ":" in s:
                    h, p = s.split(":", 1)
                    host = h
                    port = int(p)
                else:
                    host = s
            except Exception:
                pass

        if host:
            conn_fn = globals().get("connect")
            if conn_fn is None and adb is not None:
                conn_fn = getattr(adb, "connect", None)
            if conn_fn is not None:
                res = await conn_fn(host, port)
                return bool(res)
    except Exception:
        pass
    return False


async def _execute_action(
    device_id: str,
    action_name: str,
    params: dict[str, Any],
    coro_fn: Callable[[], Awaitable[T]],
    actor: str = "system",
) -> T:
    """
    Выполняет действие с автоподключением при DeviceOffline/обрыве связи
    и логированием результата в журнал аудита.
    """
    try:
        try:
            res = await coro_fn()
        except Exception as exc:
            if _is_offline_error(exc):
                await _reconnect_for_device(device_id)
                res = await coro_fn()
            else:
                raise
        await _record_audit(device_id, action_name, params, ok=True, actor=actor)
        return res
    except Exception as e:
        await _record_audit(device_id, action_name, params, ok=False, error=str(e), actor=actor)
        raise


async def _record_audit(
    device_id: str | None,
    action: str,
    params: dict[str, Any],
    ok: bool,
    error: str | None = None,
    actor: str = "system",
) -> None:
    """Безопасная запись действия в журнал аудита."""
    global audit
    try:
        aud = audit if audit is not None else (get_audit() if get_audit is not None else None)
        if aud is not None:
            await aud.log(
                actor=actor,
                device_id=device_id,
                action=action,
                params=params,
                ok=ok,
                error=error,
            )
    except Exception:
        pass


def _process_image_to_jpeg(raw_bytes: bytes, max_width: int) -> bytes:
    """Уменьшает изображение Pillow до max_width и сохраняет как JPEG."""
    img = Image.open(BytesIO(raw_bytes))
    if max_width > 0 and img.width > max_width:
        ratio = max_width / float(img.width)
        new_height = int(round(img.height * ratio))
        img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)

    if img.mode != "RGB":
        img = img.convert("RGB")

    out_buf = BytesIO()
    img.save(out_buf, format="JPEG", quality=80)
    return out_buf.getvalue()


async def _raw_screenshot_pull(device_id: str) -> bytes:
    """Делает скриншот на телефоне через shell screencap и стягивает через adb pull."""
    serial = _get_serial(device_id)
    if run_adb is None:
        raise RuntimeError("Функция run_adb не доступна")

    safe_serial = re.sub(r"[^a-zA-Z0-9_-]", "_", serial or device_id)
    remote_path = f"/data/local/tmp/hub_{safe_serial}.png"

    fd, local_tmp = tempfile.mkstemp(prefix="hub_screen_", suffix=".png")
    os.close(fd)

    try:
        # 1. adb shell screencap -p /data/local/tmp/hub_<device>.png
        await run_adb(["shell", f"screencap -p {remote_path}"], serial=serial)

        # 2. run_adb(["pull", remote, local_tmp])
        await run_adb(["pull", remote_path, local_tmp], serial=serial)

        if not os.path.exists(local_tmp) or os.path.getsize(local_tmp) == 0:
            raise RuntimeError("adb pull не создал локальный файл или файл пустой")

        with open(local_tmp, "rb") as f:
            raw_bytes = f.read()

        if not raw_bytes:
            raise RuntimeError("Пустые байты после adb pull")

        return raw_bytes
    finally:
        if local_tmp and os.path.exists(local_tmp):
            try:
                os.unlink(local_tmp)
            except OSError:
                pass
        try:
            if run_adb is not None:
                await asyncio.shield(run_adb(["shell", f"rm -f {remote_path}"], serial=serial))
        except BaseException:
            pass


async def _raw_screenshot_exec_out(device_id: str) -> bytes:
    """Резервный скриншот через adb exec-out screencap -p."""
    serial = _get_serial(device_id)
    if run_adb is None:
        raise RuntimeError("Функция run_adb не доступна")
    raw_bytes = await run_adb(["exec-out", "screencap", "-p"], serial=serial, binary=True)
    if not isinstance(raw_bytes, bytes):
        raw_bytes = str(raw_bytes).encode("latin1")
    return raw_bytes


async def _take_screenshot_raw(device_id: str) -> bytes:
    """Пытается получить байты скриншота через pull, при ошибке откатывается на exec-out."""
    try:
        return await _raw_screenshot_pull(device_id)
    except Exception as exc:
        if _is_offline_error(exc):
            raise
        return await _raw_screenshot_exec_out(device_id)


async def _take_screenshot_with_reconnect(device_id: str) -> bytes:
    """Делает скриншот (pull с откатом на exec-out) с автопереподключением при DeviceOffline."""
    try:
        return await _take_screenshot_raw(device_id)
    except Exception as exc:
        if _is_offline_error(exc):
            await _reconnect_for_device(device_id)
            return await _take_screenshot_raw(device_id)
        raise


async def _capture_raw_task(device_id: str) -> bytes:
    """Выполняет захват скриншота с автоподключением под блокировкой устройства."""
    lock = _get_screenshot_lock(device_id)
    async with lock:
        try:
            return await _take_screenshot_with_reconnect(device_id)
        finally:
            _screenshot_inflight.pop(device_id, None)


_SCREEN_CACHE_TTL: float = 3.0
_screen_cache: dict[str, tuple[bool, float]] = {}  # device_id -> (is_awake, timestamp)
_in_ensure_awake: set[str] = set()


def _is_screen_cached_awake(device_id: str) -> bool:
    """Возвращает True, если состояние экрана закешировано как включенное и кеш не старше 3 с."""
    if device_id in _screen_cache:
        is_awake, ts = _screen_cache[device_id]
        if 0 <= (time.monotonic() - ts) <= _SCREEN_CACHE_TTL:
            return is_awake
    return False


def _parse_screen_off(dumpsys_output: str) -> bool:
    """
    Определяет, выключен ли экран, по выводу
    dumpsys power | grep -E "mWakefulness|Display Power"
    с учетом формата Android 11 и 13.
    """
    if not dumpsys_output:
        return False

    # 1. Проверяем Display Power: state=OFF / ON / DOZE / etc.
    disp_match = re.search(
        r"Display Power:\s*state[=\s]+([A-Za-z0-9_]+)", dumpsys_output, re.IGNORECASE
    )
    if disp_match:
        disp_state = disp_match.group(1).upper()
        if disp_state in ("OFF", "DOZE", "DOZE_SUSPEND"):
            return True

    # 2. Проверяем mWakefulness: Awake / Asleep / Dozing / Dreaming / Sleeping
    wake_match = re.search(r"\bmWakefulness[=\s]+([A-Za-z0-9_]+)", dumpsys_output, re.IGNORECASE)
    if wake_match:
        wake_state = wake_match.group(1).upper()
        if wake_state != "AWAKE":
            return True

    # Явные подтверждения включенного экрана
    if disp_match and disp_match.group(1).upper() == "ON":
        return False
    if wake_match and wake_match.group(1).upper() == "AWAKE":
        return False

    return False


async def _check_swipe_lock(serial: str) -> bool:
    """Проверяет наличие экрана блокировки / keyguard при пробуждении."""
    if adb_shell is None:
        return False
    try:
        out = await adb_shell(
            serial,
            'dumpsys window | grep -E "mDreamingLockscreen=true|mShowing=true|isStatusBarKeyguard=true|mKeyguardShowing=true"',
        )
        if any(
            token in out
            for token in (
                "mDreamingLockscreen=true",
                "mShowing=true",
                "isStatusBarKeyguard=true",
                "mKeyguardShowing=true",
            )
        ):
            return True
    except Exception:
        pass

    try:
        trust_out = await adb_shell(serial, 'dumpsys trust | grep -i "locked"')
        if "device is locked=true" in trust_out.lower() or "device is locked: true" in trust_out.lower():
            return True
    except Exception:
        pass

    return False


async def ensure_awake(device_id: str, *, actor: str = "system") -> bool:
    """
    Проверяет, включен ли экран. Если выключен (по dumpsys power / Display Power state OFF /
    mWakefulness != Awake) — посылает KEYCODE_WAKEUP, при наличии свайпа-блокировки
    свайпает вверх, ожидает до 3 с включения экрана.
    Возвращает True, если пришлось будить, иначе False.
    Кеш состояния экрана не дольше 3 с на устройство.
    """
    if device_id in _in_ensure_awake:
        return False

    if _is_screen_cached_awake(device_id):
        return False

    _in_ensure_awake.add(device_id)
    try:
        serial = _get_serial(device_id)
        if adb_shell is None:
            raise RuntimeError("Функция adb_shell не доступна")

        cmd = 'dumpsys power | grep -E "mWakefulness|Display Power"'
        try:
            out = await adb_shell(serial, cmd)
        except Exception as exc:
            if _is_offline_error(exc):
                await _reconnect_for_device(device_id)
                out = await adb_shell(serial, cmd)
            else:
                out = ""

        if not _parse_screen_off(out):
            _screen_cache[device_id] = (True, time.monotonic())
            return False

        # Экран выключен — будим
        _screen_cache.pop(device_id, None)
        await adb_shell(serial, "input keyevent KEYCODE_WAKEUP")

        if await _check_swipe_lock(serial):
            await adb_shell(serial, "input swipe 500 1200 500 400 300")
            try:
                await adb_shell(serial, "wm dismiss-keyguard")
            except Exception:
                pass

        # Ожидаем до 3 с пока экран не включится
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            try:
                check_out = await adb_shell(serial, cmd)
            except Exception:
                check_out = ""
            if not _parse_screen_off(check_out):
                break

        _screen_cache[device_id] = (True, time.monotonic())
        return True
    finally:
        _in_ensure_awake.discard(device_id)


async def power(device_id: str, action: str, *, actor: str = "system") -> None:
    """
    Управление питанием устройства: wake, sleep, reboot, shutdown.
    Пишет аудит с action=power:<action>.
    Не вызывает ensure_awake рекурсивно.
    """
    act_lower = action.strip().lower()
    if act_lower not in ("wake", "sleep", "reboot", "shutdown"):
        raise ValueError(f"Неизвестное действие питания: {action}")

    audit_action = f"power:{act_lower}"
    params = {"action": act_lower}

    async def _action() -> None:
        serial = _get_serial(device_id)
        if adb_shell is None:
            raise RuntimeError("Функция adb_shell не доступна")

        if act_lower == "wake":
            await adb_shell(serial, "input keyevent KEYCODE_WAKEUP")
            _screen_cache[device_id] = (True, time.monotonic())
        elif act_lower == "sleep":
            await adb_shell(serial, "input keyevent KEYCODE_SLEEP")
            _screen_cache[device_id] = (False, time.monotonic())
        elif act_lower == "reboot":
            _screen_cache.pop(device_id, None)
            await adb_shell(serial, "reboot")
        elif act_lower == "shutdown":
            _screen_cache.pop(device_id, None)
            out = ""
            has_error = False
            try:
                out = await adb_shell(serial, "reboot -p")
            except Exception:
                has_error = True

            if (
                has_error
                or not out
                or not out.strip()
                or any(
                    err in out.lower()
                    for err in ("error", "denied", "permitted", "not allowed", "unknown")
                )
            ):
                await adb_shell(serial, "svc power shutdown")

    await _execute_action(device_id, audit_action, params, _action, actor=actor)


async def screenshot(device_id: str, max_width: int = 720, *, actor: str = "system") -> bytes:
    """Делает скриншот через pull (с откатом на exec-out) и уменьшает до JPEG с помощью Pillow."""
    await ensure_awake(device_id, actor=actor)
    params = {"max_width": max_width}

    # Не допускай гонок: на одно устройство одновременно один screenshot (asyncio.Lock на device_id),
    # параллельные запросы получают результат текущего.
    task = _screenshot_inflight.get(device_id)
    if task is None or task.done():
        task = asyncio.create_task(_capture_raw_task(device_id))
        _screenshot_inflight[device_id] = task
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    try:
        raw_bytes = await asyncio.shield(task)
        res = _process_image_to_jpeg(raw_bytes, max_width)
        await _record_audit(device_id, "screenshot", params, ok=True, actor=actor)
        return res
    except Exception as e:
        await _record_audit(device_id, "screenshot", params, ok=False, error=str(e), actor=actor)
        raise


async def tap(device_id: str, x: int, y: int, *, actor: str = "system") -> None:
    """Выполняет нажатие (тап) по координатам (x, y)."""
    await ensure_awake(device_id, actor=actor)
    params = {"x": x, "y": y}

    async def _action() -> None:
        serial = _get_serial(device_id)
        if adb_shell is None:
            raise RuntimeError("Функция adb_shell не доступна")
        await adb_shell(serial, f"input tap {x} {y}")

    await _execute_action(device_id, "tap", params, _action, actor=actor)


async def swipe(
    device_id: str,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    ms: int = 300,
    *,
    actor: str = "system",
) -> None:
    """Выполняет свайп от (x1, y1) до (x2, y2) за ms миллисекунд."""
    await ensure_awake(device_id, actor=actor)
    params = {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "ms": ms}

    async def _action() -> None:
        serial = _get_serial(device_id)
        if adb_shell is None:
            raise RuntimeError("Функция adb_shell не доступна")
        await adb_shell(serial, f"input swipe {x1} {y1} {x2} {y2} {ms}")

    await _execute_action(device_id, "swipe", params, _action, actor=actor)


async def long_press(
    device_id: str,
    x: int,
    y: int,
    ms: int = 800,
    *,
    actor: str = "system",
) -> None:
    """Выполняет долгое нажатие по координатам (x, y) длительностью ms миллисекунд."""
    await ensure_awake(device_id, actor=actor)
    params = {"x": x, "y": y, "ms": ms}

    async def _action() -> None:
        serial = _get_serial(device_id)
        if adb_shell is None:
            raise RuntimeError("Функция adb_shell не доступна")
        await adb_shell(serial, f"input swipe {x} {y} {x} {y} {ms}")

    await _execute_action(device_id, "long_press", params, _action, actor=actor)


async def type_text(device_id: str, text: str, *, actor: str = "system") -> None:
    """Вводит текст: латиница через input text, кириллица/эмодзи/спецсимволы через ADBKeyBoard broadcast ADB_INPUT_B64."""
    await ensure_awake(device_id, actor=actor)
    params = {"text": text}

    async def _action() -> None:
        serial = _get_serial(device_id)
        if adb_shell is None:
            raise RuntimeError("Функция adb_shell не доступна")

        if text:
            # Если текст содержит не-ASCII символы (кириллица, эмодзи) или спецсимволы shell (&, $, кавычки и т.д.),
            # направляем ввод через ADBKeyBoard broadcast ADB_INPUT_B64 (base64 полностью безопасен к инъекциям).
            is_safe_ascii = text.isascii() and not any(c in _SHELL_UNSAFE_CHARS for c in text)
            if is_safe_ascii:
                escaped = text.replace(" ", "%s")
                await adb_shell(serial, f"input text {shlex.quote(escaped)}")
            else:
                b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
                await adb_shell(serial, f"am broadcast -a ADB_INPUT_B64 --es msg '{b64}'")

    await _execute_action(device_id, "type_text", params, _action, actor=actor)


async def key(device_id: str, name: str, *, actor: str = "system") -> None:
    """Нажимает аппаратную клавишу (back, home, recents, enter, power, volume_up, volume_down)."""
    clean_name = name.strip()
    key_lower = clean_name.lower()
    if key_lower in KEY_CODES:
        keycode = KEY_CODES[key_lower]
    elif re.fullmatch(r"KEYCODE_[A-Z0-9_]+", clean_name.upper()):
        keycode = clean_name.upper()
    else:
        raise ValueError(f"Неизвестная клавиша: {name}")

    if key_lower not in ("power", "keycode_power"):
        await ensure_awake(device_id, actor=actor)

    params = {"name": name}

    async def _action() -> None:
        serial = _get_serial(device_id)
        if adb_shell is None:
            raise RuntimeError("Функция adb_shell не доступна")
        await adb_shell(serial, f"input keyevent {keycode}")

    await _execute_action(device_id, "key", params, _action, actor=actor)


async def open_app(device_id: str, package: str, *, actor: str = "system") -> None:
    """Запускает приложение по имени пакета."""
    await ensure_awake(device_id, actor=actor)
    params = {"package": package}

    async def _action() -> None:
        serial = _get_serial(device_id)
        if adb_shell is None:
            raise RuntimeError("Функция adb_shell не доступна")

        quoted_pkg = shlex.quote(package)
        if "/" in package:
            cmd = f"am start -n {quoted_pkg}"
        else:
            cmd = f"monkey -p {quoted_pkg} -c android.intent.category.LAUNCHER 1"

        await adb_shell(serial, cmd)

    await _execute_action(device_id, "open_app", params, _action, actor=actor)


async def list_apps(device_id: str, *, actor: str = "system") -> list[str]:
    """Возвращает список установленных пакетов приложений на устройстве."""
    params: dict[str, Any] = {}

    async def _action() -> list[str]:
        serial = _get_serial(device_id)
        if adb_shell is None:
            raise RuntimeError("Функция adb_shell не доступна")

        out = await adb_shell(serial, "pm list packages")
        apps: list[str] = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("package:"):
                apps.append(line[len("package:"):].strip())
            else:
                apps.append(line)

        apps.sort()
        return apps

    return await _execute_action(device_id, "list_apps", params, _action, actor=actor)


async def open_url(device_id: str, url: str, *, actor: str = "system") -> None:
    """Открывает URL в браузере устройства."""
    await ensure_awake(device_id, actor=actor)
    params = {"url": url}

    async def _action() -> None:
        serial = _get_serial(device_id)
        if adb_shell is None:
            raise RuntimeError("Функция adb_shell не доступна")
        await adb_shell(serial, f"am start -a android.intent.action.VIEW -d {shlex.quote(url)}")

    await _execute_action(device_id, "open_url", params, _action, actor=actor)


async def ui_dump(device_id: str, *, actor: str = "system") -> list[dict]:
    """Делает дамп интерфейса через uiautomator dump со сжатием в список элементов."""
    await ensure_awake(device_id, actor=actor)
    params: dict[str, Any] = {}

    async def _action() -> list[dict]:
        serial = _get_serial(device_id)
        if adb_shell is None:
            raise RuntimeError("Функция adb_shell не доступна")

        # 1. Удаляем старый файл перед дампом, чтобы не вернуть устаревший интерфейс при сбое
        await adb_shell(serial, "rm -f /sdcard/window_dump.xml")

        # 2. Выполняем uiautomator dump
        dump_out = await adb_shell(serial, "uiautomator dump /sdcard/window_dump.xml")
        dump_clean = dump_out.strip()
        if "ERROR:" in dump_out or "Error:" in dump_out or "could not get idle state" in dump_out.lower():
            raise RuntimeError(f"Не удалось получить дамп UI: {dump_clean}")

        if "<hierarchy" in dump_out or "<node" in dump_out:
            xml_text = dump_out
        else:
            xml_text = await adb_shell(serial, "cat /sdcard/window_dump.xml")

        # Проверяем наличие корневого тега <hierarchy до вызова парсера
        if "<hierarchy" not in xml_text:
            err_msg = xml_text.strip() or dump_clean or "не удалось прочитать файл дампа"
            raise RuntimeError(f"Не удалось получить дамп UI: {err_msg}")

        start_idx = xml_text.find("<hierarchy")
        if start_idx != -1:
            end_idx = xml_text.rfind("</hierarchy>")
            if end_idx != -1:
                xml_text = xml_text[start_idx : end_idx + len("</hierarchy>")]
            else:
                xml_text = xml_text[start_idx:]

        # Очищаем невалидные управляющие XML-символы (\x00-\x08, \x0b, \x0c, \x0e-\x1f)
        xml_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml_text)

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as pe:
            raise RuntimeError(f"Не удалось получить дамп UI: ошибка разбора XML ({pe})") from pe

        elements: list[dict] = []
        for node in root.iter("node"):
            text = node.attrib.get("text", "")
            resource_id = node.attrib.get("resource-id", "") or node.attrib.get("resource_id", "")
            cls_name = node.attrib.get("class", "")
            content_desc = node.attrib.get("content-desc", "") or node.attrib.get("content_desc", "")
            clickable = node.attrib.get("clickable", "false").lower() == "true"
            bounds_raw = node.attrib.get("bounds", "")

            # Сжатие: пропускаем пустые неинтерактивные контейнеры без текста, id, описания
            if not (text or resource_id or content_desc or clickable):
                continue

            coords = [int(v) for v in re.findall(r"-?\d+", bounds_raw)]
            if len(coords) >= 4:
                x1, y1, x2, y2 = coords[0], coords[1], coords[2], coords[3]
                bounds = [x1, y1, x2, y2]
                center = [(x1 + x2) // 2, (y1 + y2) // 2]
            else:
                bounds = [0, 0, 0, 0]
                center = [0, 0]

            elements.append({
                "text": text,
                "resource_id": resource_id,
                "class": cls_name,
                "content_desc": content_desc,
                "clickable": clickable,
                "bounds": bounds,
                "center": center,
            })

        return elements

    return await _execute_action(device_id, "ui_dump", params, _action, actor=actor)


async def tap_element(
    device_id: str,
    text: str | None = None,
    resource_id: str | None = None,
    *,
    actor: str = "system",
) -> dict:
    """Находит элемент по text или resource_id через ui_dump, нажимает на его центр и возвращает элемент."""
    # Приводим пустые строки к None
    text = text or None
    resource_id = resource_id or None
    if not text and not resource_id:
        raise ValueError("Необходимо указать text или resource_id для поиска элемента")

    params = {"text": text, "resource_id": resource_id}

    async def _action() -> dict:
        elements = await ui_dump(device_id, actor=actor)

        # 1. Точный поиск
        exact_matches: list[dict] = []
        for elem in elements:
            text_match = (text is None) or (elem["text"] == text or elem["content_desc"] == text)
            id_match = (resource_id is None) or (
                elem["resource_id"] == resource_id
                or elem["resource_id"].endswith(f":id/{resource_id}")
            )
            if text_match and id_match:
                exact_matches.append(elem)

        matched: dict | None = None
        # Отдаем приоритет видимым элементам (center != [0, 0])
        visible_exact = [e for e in exact_matches if e.get("center") != [0, 0]]
        if visible_exact:
            matched = visible_exact[0]
        elif exact_matches:
            matched = exact_matches[0]

        # 2. Нестрогий поиск (подстрока, без учета регистра)
        if matched is None:
            t_low = text.lower() if text else None
            r_low = resource_id.lower() if resource_id else None
            fuzzy_matches: list[dict] = []
            for elem in elements:
                text_match = (t_low is None) or (
                    t_low in elem["text"].lower() or t_low in elem["content_desc"].lower()
                )
                id_match = (r_low is None) or (r_low in elem["resource_id"].lower())
                if text_match and id_match:
                    fuzzy_matches.append(elem)

            visible_fuzzy = [e for e in fuzzy_matches if e.get("center") != [0, 0]]
            if visible_fuzzy:
                matched = visible_fuzzy[0]
            elif fuzzy_matches:
                matched = fuzzy_matches[0]

        if matched is None:
            raise ValueError(f"Элемент не найден: text={text!r}, resource_id={resource_id!r}")

        cx, cy = matched["center"]
        await tap(device_id, cx, cy, actor=actor)
        return matched

    try:
        res = await _action()
        await _record_audit(
            device_id,
            "tap_element",
            {"text": text, "resource_id": resource_id, "found": res},
            ok=True,
            actor=actor,
        )
        return res
    except Exception as e:
        await _record_audit(device_id, "tap_element", params, ok=False, error=str(e), actor=actor)
        raise


async def wake_and_unlock(device_id: str, *, actor: str = "system") -> None:
    """Пробуждает устройство и разблокирует экран."""
    params: dict[str, Any] = {}

    async def _action() -> None:
        serial = _get_serial(device_id)
        if adb_shell is None:
            raise RuntimeError("Функция adb_shell не доступна")

        await adb_shell(serial, "input keyevent KEYCODE_WAKEUP")
        await adb_shell(serial, "wm dismiss-keyguard")
        await adb_shell(serial, "input keyevent 82")
        _screen_cache[device_id] = (True, time.monotonic())

    await _execute_action(device_id, "wake_and_unlock", params, _action, actor=actor)


async def shell(device_id: str, cmd: str, *, actor: str = "system") -> str:
    """Выполняет произвольную команду shell на устройстве (только если allow_shell=True)."""
    if get_settings is None:
        raise RuntimeError("Модуль конфигурации недоступен")

    settings = get_settings()
    if not settings.allow_shell:
        err = PermissionError("Выполнение shell-команд запрещено настройками (allow_shell=False)")
        await _record_audit(device_id, "shell", {"cmd": cmd}, ok=False, error=str(err), actor=actor)
        raise err

    params = {"cmd": cmd}

    async def _action() -> str:
        serial = _get_serial(device_id)
        if adb_shell is None:
            raise RuntimeError("Функция adb_shell не доступна")
        return await adb_shell(serial, cmd)

    return await _execute_action(device_id, "shell", params, _action, actor=actor)


async def wait(device_id: str, seconds: float, *, actor: str = "system") -> None:
    """Ожидает указанное количество секунд."""
    params = {"seconds": seconds}
    try:
        await asyncio.sleep(seconds)
        await _record_audit(device_id, "wait", params, ok=True, actor=actor)
    except Exception as e:
        await _record_audit(device_id, "wait", params, ok=False, error=str(e), actor=actor)
        raise
