from __future__ import annotations

import asyncio
import base64
import contextvars
import os
import re
import shlex
import tempfile
import time
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")

from hub.audit import get_audit
from hub.devices import adb  # noqa: F401  (tests patch actions.adb)
from hub.devices.parsing import (
    extract_hierarchy_xml as _extract_hierarchy_xml,
    find_element as _find_element,
    parse_screen_off as _parse_screen_off,
    parse_ui_elements as _parse_ui_elements,
    process_image_to_jpeg as _process_image_to_jpeg,
    split_text_for_adb_b64 as _split_text_for_adb_b64,
)
from hub.devices.adb import AdbError, AdbTimeout, DeviceOffline, adb_shell, connect, run_adb  # noqa: F401

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

ADB_KEYBOARD_PACKAGE = "com.android.adbkeyboard"
ADB_KEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"
_IME_SWITCH_DELAY: float = 0.5

# Блокировки и задачи для скриншотов по устройствам
_screenshot_locks: dict[str, asyncio.Lock] = {}
_screenshot_inflight: dict[str, asyncio.Task[bytes]] = {}

# Блокировки для ввода текста (переключение IME) по устройствам
_type_text_locks: dict[str, asyncio.Lock] = {}


def _get_type_text_lock(device_id: str) -> asyncio.Lock:
    """Возвращает персональный asyncio.Lock для синхронизации переключения клавиатуры и ввода текста."""
    if device_id not in _type_text_locks:
        _type_text_locks[device_id] = asyncio.Lock()
    return _type_text_locks[device_id]


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
            return bool(await connect(host, port))
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
    try:
        aud = audit if audit is not None else get_audit()
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


async def _raw_screenshot_pull(device_id: str) -> bytes:
    """Делает скриншот на телефоне через shell screencap и стягивает через adb pull."""
    serial = _get_serial(device_id)

    safe_serial = re.sub(r"[^a-zA-Z0-9_-]", "_", serial or device_id)
    remote_path = f"/data/local/tmp/hub_{safe_serial}.png"

    fd, local_tmp = tempfile.mkstemp(prefix="hub_screen_", suffix=".png")
    os.close(fd)

    ok = False
    try:
        # 1. screencap в один и тот же файл на телефоне (перезаписывается, без rm на каждый кадр)
        await run_adb(["shell", f"screencap -p {remote_path}"], serial=serial)

        # 2. забираем файл на сервер
        await run_adb(["pull", remote_path, local_tmp], serial=serial)

        if not os.path.exists(local_tmp) or os.path.getsize(local_tmp) == 0:
            raise RuntimeError("adb pull не создал локальный файл или файл пустой")

        with open(local_tmp, "rb") as f:
            raw_bytes = f.read()

        if not raw_bytes:
            raise RuntimeError("Пустые байты после adb pull")

        ok = True
        return raw_bytes
    finally:
        if local_tmp and os.path.exists(local_tmp):
            try:
                os.unlink(local_tmp)
            except OSError:
                pass
        if not ok:
            # при сбое не оставляем возможный обрезанный файл на телефоне
            try:
                await asyncio.shield(run_adb(["shell", f"rm -f {remote_path}"], serial=serial))
            except BaseException:
                pass


async def _raw_screenshot_exec_out(device_id: str) -> bytes:
    """Резервный скриншот через adb exec-out screencap -p."""
    serial = _get_serial(device_id)
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


async def _capture_raw_task(device_id: str, actor: str = "system") -> bytes:
    """Выполняет захват скриншота с автоподключением под блокировкой устройства."""
    lock = _get_screenshot_lock(device_id)
    async with lock:
        try:
            try:
                await ensure_awake(device_id, actor=actor)
            except Exception as exc:
                if _is_offline_error(exc):
                    await _reconnect_for_device(device_id)
                    await ensure_awake(device_id, actor=actor)
                else:
                    raise
            return await _take_screenshot_with_reconnect(device_id)
        finally:
            _screenshot_inflight.pop(device_id, None)


_SCREEN_CACHE_TTL: float = 3.0
_screen_cache: dict[str, tuple[bool, float]] = {}  # device_id -> (is_awake, timestamp)
_in_ensure_awake: set[str] = set()

_ensure_awake_locks: dict[str, asyncio.Lock] = {}
_ensure_awake_ctx: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "_ensure_awake_ctx", default=frozenset()
)


def _get_ensure_awake_lock(device_id: str) -> asyncio.Lock:
    """Возвращает персональный asyncio.Lock для синхронизации пробуждения устройства."""
    if device_id not in _ensure_awake_locks:
        _ensure_awake_locks[device_id] = asyncio.Lock()
    return _ensure_awake_locks[device_id]


def _is_screen_cached_awake(device_id: str) -> bool:
    """Возвращает True, если состояние экрана закешировано как включенное и кеш не старше 3 с."""
    if device_id in _screen_cache:
        is_awake, ts = _screen_cache[device_id]
        if 0 <= (time.monotonic() - ts) <= _SCREEN_CACHE_TTL:
            return is_awake
    return False


async def _check_swipe_lock(serial: str) -> bool:
    """Проверяет наличие экрана блокировки / keyguard при пробуждении."""
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
    свайпает вверх, ожидает до 3 с пока экран не включится.
    Возвращает True, если пришлось будить, иначе False.
    Кеш состояния экрана не дольше 3 с на устройство.
    """
    active_devices = _ensure_awake_ctx.get()
    if device_id in active_devices or device_id in _in_ensure_awake:
        return False

    if _is_screen_cached_awake(device_id):
        return False

    lock = _get_ensure_awake_lock(device_id)
    async with lock:
        if _is_screen_cached_awake(device_id):
            return False

        token = _ensure_awake_ctx.set(active_devices | {device_id})
        try:
            serial = _get_serial(device_id)

            cmd = 'dumpsys power | grep -E "mWakefulness|Display Power"'
            out = ""
            try:
                out = await adb_shell(serial, cmd)
            except Exception as exc:
                if _is_offline_error(exc):
                    await _reconnect_for_device(device_id)
                    try:
                        out = await adb_shell(serial, cmd)
                    except Exception:
                        _screen_cache.pop(device_id, None)
                        raise
                else:
                    _screen_cache.pop(device_id, None)
                    raise

            if out and not _parse_screen_off(out):
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
            is_awake = False
            while time.monotonic() < deadline:
                await asyncio.sleep(0.1)
                try:
                    check_out = await adb_shell(serial, cmd)
                except Exception:
                    continue
                if check_out and not _parse_screen_off(check_out):
                    is_awake = True
                    break

            if is_awake:
                _screen_cache[device_id] = (True, time.monotonic())
            else:
                _screen_cache.pop(device_id, None)

            return True
        finally:
            _ensure_awake_ctx.reset(token)


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
    params = {"max_width": max_width}

    # Не допускай гонок: на одно устройство одновременно один screenshot (asyncio.Lock на device_id),
    # параллельные запросы получают результат текущего.
    task = _screenshot_inflight.get(device_id)
    if task is None or task.done():
        task = asyncio.create_task(_capture_raw_task(device_id, actor=actor))
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
    params = {"x": x, "y": y}

    async def _action() -> None:
        await ensure_awake(device_id, actor=actor)
        serial = _get_serial(device_id)
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
    params = {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "ms": ms}

    async def _action() -> None:
        await ensure_awake(device_id, actor=actor)
        serial = _get_serial(device_id)
        await adb_shell(serial, f"input swipe {x1} {y1} {x2} {y2} {ms}")

    await _execute_action(device_id, "swipe", params, _action, actor=actor)


async def type_text(device_id: str, text: str, *, actor: str = "system") -> None:
    """Вводит текст: латиница через input text, кириллица/эмодзи через ADBKeyBoard broadcast ADB_INPUT_B64."""
    params = {"text": text}

    async def _action() -> None:
        await ensure_awake(device_id, actor=actor)
        serial = _get_serial(device_id)

        if not text:
            return

        # Чисто ASCII-текст по-прежнему отправляем через input text без переключения IME
        if text.isascii():
            escaped = text.replace(" ", "%s")
            await adb_shell(serial, f"input text {shlex.quote(escaped)}")
            return

        # Для текста с не-ASCII символами: блокировка устройства, проверка ADBKeyBoard и временное переключение IME
        lock = _get_type_text_lock(device_id)
        async with lock:
            pm_out = await adb_shell(serial, f"pm list packages {ADB_KEYBOARD_PACKAGE}")
            if ADB_KEYBOARD_PACKAGE not in (pm_out or ""):
                raise RuntimeError("На телефоне не установлена клавиатура ADBKeyBoard")

            prev_ime = (await adb_shell(serial, "settings get secure default_input_method")).strip()
            try:
                await adb_shell(serial, f"ime enable {ADB_KEYBOARD_IME}")
                await adb_shell(serial, f"ime set {ADB_KEYBOARD_IME}")
                if _IME_SWITCH_DELAY > 0:
                    await asyncio.sleep(_IME_SWITCH_DELAY)
                for chunk in _split_text_for_adb_b64(text):
                    b64 = base64.b64encode(chunk.encode("utf-8")).decode("ascii")
                    await adb_shell(serial, f"am broadcast -a ADB_INPUT_B64 --es msg '{b64}'")
            finally:
                if prev_ime and prev_ime != "null" and prev_ime != ADB_KEYBOARD_IME:
                    await adb_shell(serial, f"ime set {prev_ime}")

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

    params = {"name": name}

    async def _action() -> None:
        if key_lower not in ("power", "keycode_power"):
            await ensure_awake(device_id, actor=actor)
        serial = _get_serial(device_id)
        try:
            await adb_shell(serial, f"input keyevent {keycode}")
        finally:
            if key_lower in ("power", "keycode_power"):
                _screen_cache.pop(device_id, None)

    await _execute_action(device_id, "key", params, _action, actor=actor)


async def open_app(device_id: str, package: str, *, actor: str = "system") -> None:
    """Запускает приложение по имени пакета."""
    params = {"package": package}

    async def _action() -> None:
        await ensure_awake(device_id, actor=actor)
        serial = _get_serial(device_id)

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
    params = {"url": url}

    async def _action() -> None:
        await ensure_awake(device_id, actor=actor)
        serial = _get_serial(device_id)
        await adb_shell(serial, f"am start -a android.intent.action.VIEW -d {shlex.quote(url)}")

    await _execute_action(device_id, "open_url", params, _action, actor=actor)


async def ui_dump(device_id: str, *, actor: str = "system") -> list[dict]:
    """Делает дамп интерфейса через uiautomator dump со сжатием в список элементов."""
    params: dict[str, Any] = {}

    async def _action() -> list[dict]:
        await ensure_awake(device_id, actor=actor)
        serial = _get_serial(device_id)

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

        return _parse_ui_elements(_extract_hierarchy_xml(xml_text))

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

        matched = _find_element(elements, text, resource_id)
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


async def wait(device_id: str, seconds: float, *, actor: str = "system") -> None:
    """Ожидает указанное количество секунд."""
    params = {"seconds": seconds}
    try:
        await asyncio.sleep(seconds)
        await _record_audit(device_id, "wait", params, ok=True, actor=actor)
    except Exception as e:
        await _record_audit(device_id, "wait", params, ok=False, error=str(e), actor=actor)
        raise
