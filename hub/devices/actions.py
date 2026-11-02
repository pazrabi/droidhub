from __future__ import annotations

import asyncio
import base64
from io import BytesIO
import re
import shlex
from typing import Any
import xml.etree.ElementTree as ET

from PIL import Image

try:
    from hub.audit import get_audit
except (ImportError, ModuleNotFoundError):
    get_audit = None  # type: ignore

try:
    from hub.config import get_settings
except (ImportError, ModuleNotFoundError):
    get_settings = None  # type: ignore

try:
    from hub.devices.adb import adb_shell, run_adb, AdbError, DeviceOffline, AdbTimeout
except (ImportError, ModuleNotFoundError):
    adb_shell = None  # type: ignore
    run_adb = None  # type: ignore
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


async def screenshot(device_id: str, max_width: int = 720, *, actor: str = "system") -> bytes:
    """Делает скриншот через screencap и уменьшает до JPEG с помощью Pillow."""
    params = {"max_width": max_width}
    try:
        serial = _get_serial(device_id)
        if run_adb is None:
            raise RuntimeError("Функция run_adb не доступна")
        raw_bytes = await run_adb(["exec-out", "screencap", "-p"], serial=serial, binary=True)
        if not isinstance(raw_bytes, bytes):
            raw_bytes = str(raw_bytes).encode("latin1")

        img = Image.open(BytesIO(raw_bytes))
        if max_width > 0 and img.width > max_width:
            ratio = max_width / float(img.width)
            new_height = int(round(img.height * ratio))
            img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)

        if img.mode != "RGB":
            img = img.convert("RGB")

        out_buf = BytesIO()
        img.save(out_buf, format="JPEG", quality=80)
        jpeg_bytes = out_buf.getvalue()

        await _record_audit(device_id, "screenshot", params, ok=True, actor=actor)
        return jpeg_bytes
    except Exception as e:
        await _record_audit(device_id, "screenshot", params, ok=False, error=str(e), actor=actor)
        raise


async def tap(device_id: str, x: int, y: int, *, actor: str = "system") -> None:
    """Выполняет нажатие (тап) по координатам (x, y)."""
    params = {"x": x, "y": y}
    try:
        serial = _get_serial(device_id)
        if adb_shell is None:
            raise RuntimeError("Функция adb_shell не доступна")
        await adb_shell(serial, f"input tap {x} {y}")
        await _record_audit(device_id, "tap", params, ok=True, actor=actor)
    except Exception as e:
        await _record_audit(device_id, "tap", params, ok=False, error=str(e), actor=actor)
        raise


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
    try:
        serial = _get_serial(device_id)
        if adb_shell is None:
            raise RuntimeError("Функция adb_shell не доступна")
        await adb_shell(serial, f"input swipe {x1} {y1} {x2} {y2} {ms}")
        await _record_audit(device_id, "swipe", params, ok=True, actor=actor)
    except Exception as e:
        await _record_audit(device_id, "swipe", params, ok=False, error=str(e), actor=actor)
        raise


async def long_press(
    device_id: str,
    x: int,
    y: int,
    ms: int = 800,
    *,
    actor: str = "system",
) -> None:
    """Выполняет долгое нажатие по координатам (x, y) длительностью ms миллисекунд."""
    params = {"x": x, "y": y, "ms": ms}
    try:
        serial = _get_serial(device_id)
        if adb_shell is None:
            raise RuntimeError("Функция adb_shell не доступна")
        await adb_shell(serial, f"input swipe {x} {y} {x} {y} {ms}")
        await _record_audit(device_id, "long_press", params, ok=True, actor=actor)
    except Exception as e:
        await _record_audit(device_id, "long_press", params, ok=False, error=str(e), actor=actor)
        raise


async def type_text(device_id: str, text: str, *, actor: str = "system") -> None:
    """Вводит текст: латиница через input text, кириллица/эмодзи/спецсимволы через ADBKeyBoard broadcast ADB_INPUT_B64."""
    params = {"text": text}
    try:
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

        await _record_audit(device_id, "type_text", params, ok=True, actor=actor)
    except Exception as e:
        await _record_audit(device_id, "type_text", params, ok=False, error=str(e), actor=actor)
        raise


async def key(device_id: str, name: str, *, actor: str = "system") -> None:
    """Нажимает аппаратную клавишу (back, home, recents, enter, power, volume_up, volume_down)."""
    params = {"name": name}
    try:
        clean_name = name.strip()
        key_lower = clean_name.lower()
        if key_lower in KEY_CODES:
            keycode = KEY_CODES[key_lower]
        elif re.fullmatch(r"KEYCODE_[A-Z0-9_]+", clean_name.upper()):
            keycode = clean_name.upper()
        else:
            raise ValueError(f"Неизвестная клавиша: {name}")

        serial = _get_serial(device_id)
        if adb_shell is None:
            raise RuntimeError("Функция adb_shell не доступна")
        await adb_shell(serial, f"input keyevent {keycode}")
        await _record_audit(device_id, "key", params, ok=True, actor=actor)
    except Exception as e:
        await _record_audit(device_id, "key", params, ok=False, error=str(e), actor=actor)
        raise


async def open_app(device_id: str, package: str, *, actor: str = "system") -> None:
    """Запускает приложение по имени пакета."""
    params = {"package": package}
    try:
        serial = _get_serial(device_id)
        if adb_shell is None:
            raise RuntimeError("Функция adb_shell не доступна")

        quoted_pkg = shlex.quote(package)
        if "/" in package:
            cmd = f"am start -n {quoted_pkg}"
        else:
            cmd = f"monkey -p {quoted_pkg} -c android.intent.category.LAUNCHER 1"

        await adb_shell(serial, cmd)
        await _record_audit(device_id, "open_app", params, ok=True, actor=actor)
    except Exception as e:
        await _record_audit(device_id, "open_app", params, ok=False, error=str(e), actor=actor)
        raise


async def list_apps(device_id: str, *, actor: str = "system") -> list[str]:
    """Возвращает список установленных пакетов приложений на устройстве."""
    params: dict[str, Any] = {}
    try:
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
        await _record_audit(device_id, "list_apps", params, ok=True, actor=actor)
        return apps
    except Exception as e:
        await _record_audit(device_id, "list_apps", params, ok=False, error=str(e), actor=actor)
        raise


async def open_url(device_id: str, url: str, *, actor: str = "system") -> None:
    """Открывает URL в браузере устройства."""
    params = {"url": url}
    try:
        serial = _get_serial(device_id)
        if adb_shell is None:
            raise RuntimeError("Функция adb_shell не доступна")
        await adb_shell(serial, f"am start -a android.intent.action.VIEW -d {shlex.quote(url)}")
        await _record_audit(device_id, "open_url", params, ok=True, actor=actor)
    except Exception as e:
        await _record_audit(device_id, "open_url", params, ok=False, error=str(e), actor=actor)
        raise


async def ui_dump(device_id: str, *, actor: str = "system") -> list[dict]:
    """Делает дамп интерфейса через uiautomator dump со сжатием в список элементов."""
    params: dict[str, Any] = {}
    try:
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

        await _record_audit(device_id, "ui_dump", params, ok=True, actor=actor)
        return elements
    except Exception as e:
        await _record_audit(device_id, "ui_dump", params, ok=False, error=str(e), actor=actor)
        raise


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
    try:
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
        await _record_audit(
            device_id,
            "tap_element",
            {"text": text, "resource_id": resource_id, "found": matched},
            ok=True,
            actor=actor,
        )
        return matched
    except Exception as e:
        await _record_audit(device_id, "tap_element", params, ok=False, error=str(e), actor=actor)
        raise


async def wake_and_unlock(device_id: str, *, actor: str = "system") -> None:
    """Пробуждает устройство и разблокирует экран."""
    params: dict[str, Any] = {}
    try:
        serial = _get_serial(device_id)
        if adb_shell is None:
            raise RuntimeError("Функция adb_shell не доступна")

        await adb_shell(serial, "input keyevent KEYCODE_WAKEUP")
        await adb_shell(serial, "wm dismiss-keyguard")
        await adb_shell(serial, "input keyevent 82")
        await _record_audit(device_id, "wake_and_unlock", params, ok=True, actor=actor)
    except Exception as e:
        await _record_audit(device_id, "wake_and_unlock", params, ok=False, error=str(e), actor=actor)
        raise


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
    try:
        serial = _get_serial(device_id)
        if adb_shell is None:
            raise RuntimeError("Функция adb_shell не доступна")

        out = await adb_shell(serial, cmd)
        await _record_audit(device_id, "shell", params, ok=True, actor=actor)
        return out
    except Exception as e:
        await _record_audit(device_id, "shell", params, ok=False, error=str(e), actor=actor)
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
