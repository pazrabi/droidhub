"""MCP-сервер DroidHub: инструменты управления телефонами (без shell и питания)."""
from __future__ import annotations

import dataclasses
import time
from typing import Any

from mcp.server.mcpserver import Image, MCPServer

import hub.audit as audit_module
from hub.auth import Client
from hub.devices import actions, browser
from hub.mcp_server.context import _current_client, get_current_client
from hub.mcp_server.look import CACHE_TTL_SECONDS, LookCacheEntry, build_look, look_cache
from hub.mcp_server.ratelimit import enforce_rate_limit

_STATUS_FIELDS = ("id", "name", "online", "battery", "temperature", "screen_on", "last_seen", "error")

mcp_server = MCPServer(
    "droidhub",
    instructions=(
        "Сервер DroidHub для удаленного управления Android-телефонами через ADB. "
        "Главный инструмент просмотра экрана — look: компактный и быстрый список элементов с индексами и координатами. "
        "Предпочитайте look скриншоту; скриншот использовать только когда в look нет нужных элементов (игры, картинки, WebView). "
        "Для нажатия элементов используйте tap_index по номерам из последнего вызова look."
    ),
)

# Клиент берётся из scope запроса (его кладёт MCPAuthMiddleware), иначе из ContextVar.
_orig_call_tool = mcp_server.call_tool


async def _call_tool_with_client(name: str, arguments: dict[str, Any], context: Any = None) -> Any:
    request = getattr(getattr(context, "request_context", None), "request", None)
    client = request.scope.get("auth_client") if request is not None and hasattr(request, "scope") else None
    token = _current_client.set(client or get_current_client())
    try:
        return await _orig_call_tool(name, arguments, context)
    finally:
        _current_client.reset(token)


mcp_server.call_tool = _call_tool_with_client  # type: ignore[assignment]


async def log_audit_safe(
    device_id: str | None,
    action: str,
    params: dict[str, Any],
    ok: bool,
    error: str | None = None,
    actor: str | None = None,
) -> None:
    """Запись в аудит; ошибки записи не должны ломать вызов инструмента."""
    try:
        audit = audit_module.get_audit()
        if audit is not None:
            await audit.log(
                actor=actor or get_current_client().name,
                device_id=device_id,
                action=action,
                params=params,
                ok=ok,
                error=error,
            )
    except Exception:
        pass


async def _begin(device_id: str) -> Client:
    """Текущий клиент после проверки лимита действий на устройство."""
    client = get_current_client()
    await enforce_rate_limit(device_id, client=client)
    return client


def _status_dict(status: Any, default_id: str = "") -> dict[str, Any]:
    if dataclasses.is_dataclass(status):
        return dataclasses.asdict(status)
    if isinstance(status, dict):
        return status
    result = {field: getattr(status, field, None) for field in _STATUS_FIELDS}
    result["id"] = result["id"] or default_id
    result["name"] = result["name"] or ""
    result["online"] = bool(result["online"])
    return result


@mcp_server.tool()
async def list_devices(device_id: str = "") -> list[dict[str, Any]]:
    """Получить список настроенных Android-устройств и их статусы; используй первым для получения device_id.
Get list of configured Android devices and their statuses; use first to obtain device_id."""
    results = [_status_dict(s) for s in actions.get_manager().statuses()]
    client = get_current_client()
    await log_audit_safe(device_id or None, "list_devices", {"device_id": device_id}, ok=True, actor=client.name)
    return results


@mcp_server.tool()
async def device_status(device_id: str) -> dict[str, Any]:
    """Получить детальный статус Android-устройства (батарея, температура, экран) по device_id.
Get detailed Android device status (battery, temperature, screen) by device_id."""
    client = await _begin(device_id)
    params = {"device_id": device_id}
    try:
        result = _status_dict(await actions.get_manager().refresh(device_id), default_id=device_id)
    except Exception as e:
        await log_audit_safe(device_id, "device_status", params, ok=False, error=str(e), actor=client.name)
        raise
    await log_audit_safe(device_id, "device_status", params, ok=True, actor=client.name)
    return result


@mcp_server.tool()
async def look(device_id: str) -> str:
    """Главный инструмент «посмотреть на экран»: компактный текстовый список элементов в пикселях; предпочитай look скриншоту, скриншот только когда в look нет нужных элементов (игры, картинки, WebView).
Main tool to view screen: compact text list of elements in pixels; prefer look over screenshot, use screenshot only when elements are missing in look (games, images, WebView)."""
    client = await _begin(device_id)
    raw_dump = await actions.ui_dump(device_id, actor=client.name)
    text, indices = build_look(raw_dump)
    look_cache[device_id] = LookCacheEntry(timestamp=time.monotonic(), indices=indices)
    await log_audit_safe(device_id, "look", {"device_id": device_id, "count": len(indices)}, ok=True, actor=client.name)
    return text


@mcp_server.tool()
async def tap_index(device_id: str, index: int) -> str:
    """Нажать элемент по номеру [N] из последнего look устройства (координаты в пикселях); предпочитай look и tap_index скриншоту.
Tap element by index [N] from last look on device (coordinates in pixels); prefer look and tap_index over screenshot."""
    client = await _begin(device_id)
    try:
        idx = int(index)
    except (ValueError, TypeError):
        raise ValueError(f"Некорректный номер элемента {index!r}. Сначала вызови look.")

    entry = look_cache.get(device_id)
    if entry is None:
        raise ValueError(f"Для устройства '{device_id}' нет данных последнего look: сначала вызови look.")
    if time.monotonic() - entry.timestamp > CACHE_TTL_SECONDS:
        raise ValueError(f"Данные look для устройства '{device_id}' устарели (>120 с): сначала вызови look.")
    center = entry.indices.get(idx)
    if center is None:
        raise ValueError(f"Элемент [{idx}] отсутствует в последнем look устройства '{device_id}': сначала вызови look.")

    cx, cy = center
    await actions.tap(device_id, cx, cy, actor=client.name)
    await log_audit_safe(device_id, "tap_index", {"device_id": device_id, "index": idx, "x": cx, "y": cy}, ok=True, actor=client.name)
    return f"Тап по элементу [{idx}] ({cx}, {cy}) выполнен"


@mcp_server.tool()
async def screenshot(device_id: str, max_width: int = 540) -> Image:
    """Сделать скриншот экрана в пикселях JPEG; предпочитай look скриншоту, скриншот использовать только когда в look нет нужных элементов (игры, картинки, WebView).
Take JPEG screenshot in pixels; prefer look over screenshot, use screenshot only when needed elements are missing in look (games, images, WebView)."""
    client = await _begin(device_id)
    jpeg_bytes = await actions.screenshot(device_id, max_width=max_width, actor=client.name)
    return Image(data=jpeg_bytes, format="jpeg")


@mcp_server.tool()
async def ui_dump(device_id: str) -> list[dict[str, Any]]:
    """Получить полное дерево элементов интерфейса с координатами в пикселях; для большинства задач предпочитай look.
Get full UI hierarchy tree with pixel coordinates; for most tasks prefer look over screenshot and ui_dump."""
    client = await _begin(device_id)
    return await actions.ui_dump(device_id, actor=client.name)


@mcp_server.tool()
async def tap(device_id: str, x: int, y: int) -> str:
    """Нажать по координатам (x, y) в пикселях; предпочитай look и tap_index скриншоту, скриншот только когда в look нет нужных элементов (игры, картинки, WebView).
Tap screen at (x, y) in pixels; prefer look and tap_index over screenshot, use screenshot only when elements are missing in look (games, images, WebView)."""
    client = await _begin(device_id)
    await actions.tap(device_id, x, y, actor=client.name)
    return f"Тап ({x}, {y}) выполнен"


@mcp_server.tool()
async def tap_element(
    device_id: str,
    text: str | None = None,
    resource_id: str | None = None,
) -> str:
    """Найти элемент по тексту или id и нажать на его центр в пикселях; предпочитай look и tap_index скриншоту, скриншот только для игр, картинок, WebView.
Find element by text or id and tap its pixel center; prefer look and tap_index over screenshot, screenshot only for games, images, WebView."""
    client = await _begin(device_id)
    elem = await actions.tap_element(device_id, text=text, resource_id=resource_id, actor=client.name)
    target = text or resource_id or (elem.get("text") if isinstance(elem, dict) else "") or ""
    return f"Тап по элементу «{target}» выполнен" if target else "Тап по элементу выполнен"


@mcp_server.tool()
async def swipe(
    device_id: str,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    ms: int = 300,
) -> str:
    """Выполнить свайп от (x1, y1) до (x2, y2) в пикселях за время ms; предпочитай look скриншоту, скриншот только для игр, картинок, WebView.
Swipe from (x1, y1) to (x2, y2) in pixels taking ms milliseconds; prefer look over screenshot, screenshot only for games, images, WebView."""
    client = await _begin(device_id)
    await actions.swipe(device_id, x1, y1, x2, y2, ms=ms, actor=client.name)
    return f"Свайп ({x1}, {y1}) -> ({x2}, {y2}) выполнен"


@mcp_server.tool()
async def type_text(device_id: str, text: str) -> str:
    """Ввести текст в активное поле ввода; перед вводом найди поле через look и нажми на него, предпочитай look скриншоту.
Type text into active input field; before typing find field via look and tap it, prefer look over screenshot."""
    client = await _begin(device_id)
    await actions.type_text(device_id, text, actor=client.name)
    return f"Введен текст: {text}"


@mcp_server.tool()
async def press_key(device_id: str, key: str) -> str:
    """Нажать системную клавишу (back, home, recents, enter, power, volume_up, volume_down); для проверки экрана предпочитай look скриншоту.
Press system key (back, home, recents, enter, power, volume_up, volume_down); to inspect screen prefer look over screenshot."""
    client = await _begin(device_id)
    await actions.key(device_id, key, actor=client.name)
    return f"Нажата клавиша {key}"


@mcp_server.tool()
async def open_app(device_id: str, package: str) -> str:
    """Запустить приложение по имени пакета; после запуска проверяй экран через look, предпочитай look скриншоту.
Open app by package name; after launch inspect screen via look, prefer look over screenshot."""
    client = await _begin(device_id)
    await actions.open_app(device_id, package, actor=client.name)
    return f"Запущено приложение {package}"


@mcp_server.tool()
async def list_apps(device_id: str) -> list[str]:
    """Получить список всех установленных пакетов приложений на устройстве перед вызовом open_app.
Get list of all installed application packages on device before calling open_app."""
    client = await _begin(device_id)
    return await actions.list_apps(device_id, actor=client.name)


@mcp_server.tool()
async def open_url(device_id: str, url: str) -> str:
    """Открыть URL в браузере по умолчанию; после перехода проверяй экран через look, предпочитай look скриншоту.
Open URL in default browser; after navigation inspect screen via look, prefer look over screenshot."""
    client = await _begin(device_id)
    await actions.open_url(device_id, url, actor=client.name)
    return f"Открыт URL {url}"


@mcp_server.tool()
async def wait(device_id: str, seconds: float = 1.0) -> dict[str, Any]:
    """Приостановить выполнение на указанное количество секунд; после ожидания проверяй экран через look, предпочитай look скриншоту.
Pause execution for specified number of seconds; after waiting inspect screen via look, prefer look over screenshot."""
    client = await _begin(device_id)
    await actions.wait(device_id, seconds, actor=client.name)
    return {"ok": True, "waited": seconds}


@mcp_server.tool()
async def fetch_page(
    device_id: str,
    url: str,
    wait_seconds: float = 15.0,
    capture_json: list[str] | None = None,
    max_chars: int = 200000,
) -> dict[str, Any]:
    """Открыть адрес в Chrome телефона и вернуть текст всей страницы (не пиксели) и JSON-ответы сайта; capture_json: подстроки адресов нужных ответов, wait_seconds: сколько ждать догрузки; в ответе captcha=true, если показана капча.
Open URL in phone's Chrome and return full page text (no pixels) plus the site's JSON responses; capture_json: URL substrings of wanted responses, wait_seconds: max wait for content; captcha=true when a captcha is shown."""
    client = await _begin(device_id)
    return await browser.fetch_page(
        device_id, url, wait_seconds, capture_json, max_chars, actor=client.name
    )


@mcp_server.tool()
async def browser_tabs(device_id: str) -> list[dict[str, Any]]:
    """Список открытых вкладок Chrome на телефоне (index, title, url); индекс нужен для page_text.
List open Chrome tabs on the phone (index, title, url); the index is used by page_text."""
    client = await _begin(device_id)
    return await browser.browser_tabs(device_id, actor=client.name)


@mcp_server.tool()
async def page_text(device_id: str, tab_index: int = 0, max_chars: int = 200000) -> dict[str, Any]:
    """Текст всей страницы уже открытой вкладки Chrome по индексу из browser_tabs, а не только видимой части экрана.
Full text of an already open Chrome tab by index from browser_tabs, not only the visible part of the screen."""
    client = await _begin(device_id)
    return await browser.page_text(device_id, tab_index, max_chars, actor=client.name)
