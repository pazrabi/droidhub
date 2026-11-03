from __future__ import annotations

import asyncio
from collections import defaultdict
import contextlib
import contextvars
import dataclasses
import hmac
import sys
import time
from typing import Any, Callable

from fastapi import status
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

try:
    from hub.auth import Client, resolve_token
except ImportError:
    @dataclasses.dataclass(frozen=True)
    class Client:  # type: ignore[no-redef]
        name: str
        rate_limit_per_min: int | None = None
        allow_power: bool = True

    def resolve_token(token: str, settings: Any = None) -> Client | None:  # type: ignore[no-redef]
        if not token:
            return None
        if settings is None:
            try:
                settings = get_settings()
            except Exception:
                settings = None
        raw = getattr(settings, "client_tokens", "") if settings else ""
        if raw:
            for item in raw.split(","):
                item = item.strip()
                if not item:
                    continue
                parts = item.split(":")
                if len(parts) >= 2:
                    c_name, c_tok = parts[0].strip(), parts[1].strip()
                    if not c_name or not c_tok:
                        continue
                    limit = None
                    allow_power = True
                    if len(parts) >= 3 and parts[2].strip():
                        try:
                            limit = int(parts[2].strip())
                        except ValueError:
                            pass
                    if len(parts) >= 4 and parts[3].strip():
                        if "nopower" in parts[3].strip().split("+"):
                            allow_power = False
                    if hmac.compare_digest(token, c_tok):
                        return Client(name=c_name, rate_limit_per_min=limit, allow_power=allow_power)
        mcp_tok = getattr(settings, "mcp_token", "") if settings else ""
        if mcp_tok and hmac.compare_digest(token, mcp_tok):
            return Client(name="mcp")
        return None

try:
    from mcp.server.fastmcp import FastMCP, Image
except (ImportError, ModuleNotFoundError):
    try:
        from mcp.server.mcpserver import MCPServer as FastMCP, Image
    except (ImportError, ModuleNotFoundError):
        FastMCP = None  # type: ignore
        Image = None  # type: ignore

try:
    from mcp.server.transport_security import TransportSecuritySettings
except (ImportError, ModuleNotFoundError):
    TransportSecuritySettings = None  # type: ignore

try:
    from hub.api.routes import limiter
except (ImportError, ModuleNotFoundError):
    limiter = None  # type: ignore

try:
    import hub.audit as audit_module
except (ImportError, ModuleNotFoundError):
    audit_module = None  # type: ignore

from hub.config import Settings, get_settings
from hub.devices import actions


# --- Вспомогательные структуры данных и маппинги ---

CLASS_MAP: dict[str, str] = {
    "Button": "Кнопка",
    "ImageButton": "Кнопка",
    "TextView": "Текст",
    "EditText": "Поле ввода",
    "ImageView": "Картинка",
    "CheckBox": "Флажок",
    "RadioButton": "Радиокнопка",
    "Switch": "Переключатель",
    "ProgressBar": "Прогресс",
    "SeekBar": "Ползунок",
    "Spinner": "Список",
    "ListView": "Список",
    "RecyclerView": "Список",
    "ScrollView": "Прокрутка",
    "WebView": "WebView",
    "View": "Элемент",
    "ViewGroup": "Группа",
    "FrameLayout": "Контейнер",
    "LinearLayout": "Контейнер",
    "RelativeLayout": "Контейнер",
}


def _is_truthy(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1")
    return bool(val)


@dataclasses.dataclass
class _LookCacheEntry:
    timestamp: float
    indices: dict[int, tuple[int, int]]


_look_cache: dict[str, _LookCacheEntry] = {}


# --- Отслеживание контекста авторизованного клиента ---

_current_client: contextvars.ContextVar[Client | None] = contextvars.ContextVar(
    "_current_client", default=None
)
_session_clients: dict[str, Client] = {}


def get_current_client() -> Client:
    """Возвращает текущего авторизованного клиента или дефолтного клиента 'mcp'."""
    c = _current_client.get()
    if c is not None:
        return c
    return Client(name="mcp")


def set_current_client(client: Client | None) -> contextvars.Token:
    """Устанавливает текущего клиента в ContextVar."""
    return _current_client.set(client)


@contextlib.contextmanager
def client_context(client: Client | None):
    """Контекстный менеджер для установки текущего клиента."""
    tok = _current_client.set(client)
    try:
        yield
    finally:
        _current_client.reset(tok)


def _call_resolve_token(token: str, settings: Any = None) -> Client | None:
    if not token or not isinstance(token, str):
        return None
    token = token.strip()
    if not token:
        return None
    auth_mod = sys.modules.get("hub.auth")
    auth_fn = getattr(auth_mod, "resolve_token", None) if auth_mod else None
    current_fn = getattr(sys.modules.get(__name__), "resolve_token", resolve_token)
    fn = auth_fn or current_fn or resolve_token
    if fn is not None:
        return fn(token, settings=settings)
    return None


# --- Ограничитель действий на пару (клиент, устройство) ---

class ClientDeviceRateLimiter:
    """Ограничитель действий на пару (клиент, устройство) со скользящим окном в памяти."""

    def __init__(self, window_seconds: float = 60.0) -> None:
        self.window_seconds = window_seconds
        self._history: dict[tuple[str, str], list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def check(
        self,
        device_id: str,
        client: Client | str | None = None,
        limit: int | None = None,
    ) -> bool:
        if not device_id:
            return True

        if client is None:
            c = get_current_client()
            client_name = c.name if hasattr(c, "name") else str(c or "mcp")
            client_limit = getattr(c, "rate_limit_per_min", None)
        elif isinstance(client, str):
            client_name = client
            client_limit = None
        else:
            client_name = getattr(client, "name", "mcp")
            client_limit = getattr(client, "rate_limit_per_min", None)

        if limit is None:
            if client_limit is not None:
                limit = client_limit
            else:
                try:
                    settings = get_settings()
                    limit = getattr(settings, "rate_limit_per_min", 60)
                except Exception:
                    limit = 60

        if limit <= 0:
            return True

        now = time.monotonic()
        cutoff = now - self.window_seconds
        key = (client_name, device_id)

        async with self._lock:
            history = [t for t in self._history[key] if t > cutoff]
            if len(history) >= limit:
                self._history[key] = history
                return False
            history.append(now)
            self._history[key] = history
            return True

    def reset(
        self,
        device_id: str | None = None,
        client: Client | str | None = None,
    ) -> None:
        client_name = None
        if client is not None:
            client_name = client if isinstance(client, str) else getattr(client, "name", None)

        if device_id is not None and client_name is not None:
            self._history.pop((client_name, device_id), None)
        elif device_id is not None:
            keys_to_del = [k for k in self._history if (isinstance(k, tuple) and k[1] == device_id) or k == device_id]
            for k in keys_to_del:
                self._history.pop(k, None)
        elif client_name is not None:
            keys_to_del = [k for k in self._history if isinstance(k, tuple) and k[0] == client_name]
            for k in keys_to_del:
                self._history.pop(k, None)
        else:
            self._history.clear()


limiter = ClientDeviceRateLimiter()


async def _enforce_rate_limit(device_id: str, client: Client | None = None) -> None:
    """Проверяет лимит действий в минуту на пару (клиент, устройство)."""
    if not device_id:
        return
    c = client or get_current_client()
    allowed = await limiter.check(device_id, client=c)
    if not allowed:
        client_name = getattr(c, "name", "mcp")
        raise RuntimeError(f"Превышен лимит действий на устройство '{device_id}' для клиента '{client_name}'")


async def _log_audit_safe(
    device_id: str | None,
    action: str,
    params: dict[str, Any],
    ok: bool,
    error: str | None = None,
    actor: str | None = None,
) -> None:
    """Безопасная запись события аудита с actor=client.name."""
    try:
        if audit_module is not None:
            aud = audit_module.get_audit()
            if aud is not None:
                if actor is None:
                    actor = get_current_client().name
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


# --- Инициализация FastMCP сервера ---

if FastMCP is not None:
    mcp_server = FastMCP(
        "droidhub",
        instructions=(
            "Сервер DroidHub для удаленного управления Android-телефонами через ADB. "
            "Главный инструмент просмотра экрана — look: компактный и быстрый список элементов с индексами и координатами. "
            "Предпочитайте look скриншоту; скриншот использовать только когда в look нет нужных элементов (игры, картинки, WebView). "
            "Для нажатия элементов используйте tap_index по номерам из последнего вызова look."
        ),
    )
else:
    mcp_server = None  # type: ignore

mcp = mcp_server

if mcp_server is not None:
    _orig_call_tool = mcp_server.call_tool

    async def _wrapped_call_tool(
        name: str,
        arguments: dict[str, Any],
        context: Any = None,
    ) -> Any:
        client: Client | None = None
        if context is not None:
            req_ctx = getattr(context, "request_context", None)
            if req_ctx is not None:
                req = getattr(req_ctx, "request", None)
                if req is not None and hasattr(req, "scope"):
                    client = req.scope.get("auth_client")
                if client is None:
                    session = getattr(req_ctx, "session", None)
                    sess_id = (
                        getattr(session, "session_id", None)
                        or getattr(session, "mcp_session_id", None)
                    )
                    if sess_id and sess_id in _session_clients:
                        client = _session_clients[sess_id]
        if client is None:
            client = _current_client.get() or Client(name="mcp")

        tok = _current_client.set(client)
        try:
            return await _orig_call_tool(name, arguments, context)
        finally:
            _current_client.reset(tok)

    mcp_server.call_tool = _wrapped_call_tool  # type: ignore[assignment]


# --- Регистрация инструментов ---

if mcp_server is not None:

    @mcp_server.tool()
    async def list_devices(device_id: str = "") -> list[dict[str, Any]]:
        """Получить список настроенных Android-устройств и их статусы; используй первым для получения device_id.
Get list of configured Android devices and their statuses; use first to obtain device_id."""
        mgr = actions.get_manager()
        statuses = mgr.statuses()
        results: list[dict[str, Any]] = []
        for s in statuses:
            if dataclasses.is_dataclass(s):
                results.append(dataclasses.asdict(s))
            elif isinstance(s, dict):
                results.append(s)
            else:
                results.append({
                    "id": getattr(s, "id", ""),
                    "name": getattr(s, "name", ""),
                    "online": getattr(s, "online", False),
                    "battery": getattr(s, "battery", None),
                    "temperature": getattr(s, "temperature", None),
                    "screen_on": getattr(s, "screen_on", None),
                    "last_seen": getattr(s, "last_seen", None),
                    "error": getattr(s, "error", None),
                })
        client = get_current_client()
        await _log_audit_safe(device_id or None, "list_devices", {"device_id": device_id}, ok=True, actor=client.name)
        return results

    @mcp_server.tool()
    async def device_status(device_id: str) -> dict[str, Any]:
        """Получить детальный статус Android-устройства (батарея, температура, экран) по device_id.
Get detailed Android device status (battery, temperature, screen) by device_id."""
        client = get_current_client()
        await _enforce_rate_limit(device_id, client=client)
        mgr = actions.get_manager()
        try:
            status = await mgr.refresh(device_id)
            if dataclasses.is_dataclass(status):
                res = dataclasses.asdict(status)
            elif isinstance(status, dict):
                res = status
            else:
                res = {
                    "id": getattr(status, "id", device_id),
                    "name": getattr(status, "name", ""),
                    "online": getattr(status, "online", False),
                    "battery": getattr(status, "battery", None),
                    "temperature": getattr(status, "temperature", None),
                    "screen_on": getattr(status, "screen_on", None),
                    "last_seen": getattr(status, "last_seen", None),
                    "error": getattr(status, "error", None),
                }
            await _log_audit_safe(device_id, "device_status", {"device_id": device_id}, ok=True, actor=client.name)
            return res
        except Exception as e:
            await _log_audit_safe(device_id, "device_status", {"device_id": device_id}, ok=False, error=str(e), actor=client.name)
            raise

    @mcp_server.tool()
    async def look(device_id: str) -> str:
        """Главный инструмент «посмотреть на экран»: компактный текстовый список элементов в пикселях; предпочитай look скриншоту, скриншот только когда в look нет нужных элементов (игры, картинки, WebView).
Main tool to view screen: compact text list of elements in pixels; prefer look over screenshot, use screenshot only when elements are missing in look (games, images, WebView)."""
        client = get_current_client()
        await _enforce_rate_limit(device_id, client=client)
        raw_dump = await actions.ui_dump(device_id, actor=client.name)

        pkg: str | None = None
        act: str | None = None
        if isinstance(raw_dump, dict):
            raw_elements = raw_dump.get("elements", [])
            pkg = raw_dump.get("package")
            act = raw_dump.get("activity")
        elif isinstance(raw_dump, list):
            raw_elements = raw_dump
        else:
            raw_elements = []

        # 1. Поиск пакета / активити если доступно
        for el in raw_elements:
            if isinstance(el, dict):
                if not pkg and el.get("package"):
                    pkg = str(el["package"]).strip() or None
                if not act and el.get("activity"):
                    act = str(el["activity"]).strip() or None
                if not pkg and el.get("resource_id") and ":id/" in str(el["resource_id"]):
                    prefix = str(el["resource_id"]).split(":id/")[0]
                    if "." in prefix and not prefix.startswith("android"):
                        pkg = prefix

        header_line: str | None = None
        if pkg and act:
            header_line = f"Экран: {pkg} / {act}"
        elif pkg:
            header_line = f"Экран: {pkg}"
        elif act:
            header_line = f"Экран: {act}"

        # 2. Фильтрация только значимых элементов
        candidates: list[tuple[int, int, str, str, str, dict[str, Any]]] = []
        for el in raw_elements:
            if not isinstance(el, dict):
                continue
            text_raw = (el.get("text") or "").strip()
            desc_raw = (el.get("content_desc") or "").strip()
            clickable = _is_truthy(el.get("clickable"))
            scrollable = _is_truthy(el.get("scrollable"))
            checkable = _is_truthy(el.get("checkable"))
            editable = _is_truthy(el.get("editable"))
            cls_raw = el.get("class") or ""
            if "EditText" in cls_raw:
                editable = True

            if not (text_raw or desc_raw or clickable or scrollable or checkable or editable):
                continue

            center = el.get("center")
            if isinstance(center, (list, tuple)) and len(center) >= 2:
                cx, cy = int(center[0]), int(center[1])
            elif el.get("bounds") and len(el["bounds"]) >= 4:
                b = el["bounds"]
                cx, cy = int((b[0] + b[2]) // 2), int((b[1] + b[3]) // 2)
            else:
                cx, cy = 0, 0

            if el.get("type"):
                elem_type = str(el["type"])
            else:
                short_cls = cls_raw.split(".")[-1] if cls_raw else ""
                elem_type = CLASS_MAP.get(short_cls, short_cls or "Элемент")

            if text_raw and desc_raw and text_raw != desc_raw:
                content_text = f"{text_raw} ({desc_raw})"
            elif text_raw:
                content_text = text_raw
            elif desc_raw:
                content_text = desc_raw
            else:
                content_text = ""

            content_text = " ".join(content_text.split())
            if len(content_text) > 80:
                content_text = content_text[:80]

            flags: list[str] = []
            if clickable:
                flags.append("кликабельно")
            if scrollable:
                flags.append("прокрутка")
            if checkable:
                flags.append("выбор")
            if editable:
                flags.append("ввод")
            flags_str = f" ({', '.join(flags)})" if flags else ""

            candidates.append((cy, cx, elem_type, content_text, flags_str, el))

        # 3. Сортировка сверху вниз (по cy, затем cx, приоритет элементам с текстом)
        candidates.sort(key=lambda item: (item[0], item[1], 0 if item[3] else 1))

        # 4. Дедупликация
        seen_keys: set[tuple[str, str, str, int, int]] = set()
        seen_centers_with_text: set[tuple[int, int]] = set()
        seen_centers_empty: set[tuple[int, int]] = set()
        unique_elements: list[tuple[str, str, str, int, int]] = []

        for cy, cx, elem_type, content_text, flags_str, _ in candidates:
            key = (elem_type, content_text, flags_str, cx, cy)
            if key in seen_keys:
                continue

            if content_text:
                seen_centers_with_text.add((cx, cy))
            else:
                if (cx, cy) in seen_centers_with_text or (cx, cy) in seen_centers_empty:
                    continue
                seen_centers_empty.add((cx, cy))

            seen_keys.add(key)
            unique_elements.append((elem_type, content_text, flags_str, cx, cy))

        # 5. Лимит не больше 120 элементов с пометкой
        total_count = len(unique_elements)
        max_limit = 120
        if total_count > max_limit:
            displayed = unique_elements[:max_limit]
            limit_note = f"(показано {len(displayed)} из {total_count})"
        else:
            displayed = unique_elements
            limit_note = None

        # 6. Сохранение в кеш соответствия индекс -> центр
        cache_indices: dict[int, tuple[int, int]] = {}
        lines: list[str] = []
        if header_line:
            lines.append(header_line)

        for idx, (elem_type, content_text, flags_str, cx, cy) in enumerate(displayed, start=1):
            cache_indices[idx] = (cx, cy)
            text_part = f" «{content_text}»" if content_text else ""
            lines.append(f"[{idx}] {elem_type}{text_part}{flags_str} центр=({cx},{cy})")

        if limit_note:
            lines.append(limit_note)

        _look_cache[device_id] = _LookCacheEntry(timestamp=time.monotonic(), indices=cache_indices)
        await _log_audit_safe(device_id, "look", {"device_id": device_id, "count": len(displayed)}, ok=True, actor=client.name)

        if not lines:
            return "(нет значимых элементов)"
        return "\n".join(lines)

    @mcp_server.tool()
    async def tap_index(device_id: str, index: int) -> str:
        """Нажать элемент по номеру [N] из последнего look устройства (координаты в пикселях); предпочитай look и tap_index скриншоту.
Tap element by index [N] from last look on device (coordinates in pixels); prefer look and tap_index over screenshot."""
        client = get_current_client()
        await _enforce_rate_limit(device_id, client=client)
        try:
            idx = int(index)
        except (ValueError, TypeError):
            raise ValueError(f"Некорректный номер элемента {index!r}. Сначала вызови look.")

        entry = _look_cache.get(device_id)
        if entry is None:
            raise ValueError(f"Для устройства '{device_id}' нет данных последнего look: сначала вызови look.")

        now = time.monotonic()
        if now - entry.timestamp > 120.0:
            raise ValueError(f"Данные look для устройства '{device_id}' устарели (>120 с): сначала вызови look.")

        center = entry.indices.get(idx)
        if center is None:
            raise ValueError(f"Элемент [{idx}] отсутствует в последнем look устройства '{device_id}': сначала вызови look.")

        cx, cy = center
        await actions.tap(device_id, cx, cy, actor=client.name)
        await _log_audit_safe(device_id, "tap_index", {"device_id": device_id, "index": idx, "x": cx, "y": cy}, ok=True, actor=client.name)
        return f"Тап по элементу [{idx}] ({cx}, {cy}) выполнен"

    @mcp_server.tool()
    async def screenshot(device_id: str, max_width: int = 540) -> Image:
        """Сделать скриншот экрана в пикселях JPEG; предпочитай look скриншоту, скриншот использовать только когда в look нет нужных элементов (игры, картинки, WebView).
Take JPEG screenshot in pixels; prefer look over screenshot, use screenshot only when needed elements are missing in look (games, images, WebView)."""
        client = get_current_client()
        await _enforce_rate_limit(device_id, client=client)
        jpeg_bytes = await actions.screenshot(device_id, max_width=max_width, actor=client.name)
        if Image is not None:
            return Image(data=jpeg_bytes, format="jpeg")
        return jpeg_bytes  # type: ignore

    @mcp_server.tool()
    async def ui_dump(device_id: str) -> list[dict[str, Any]]:
        """Получить полное дерево элементов интерфейса с координатами в пикселях; для большинства задач предпочитай look.
Get full UI hierarchy tree with pixel coordinates; for most tasks prefer look over screenshot and ui_dump."""
        client = get_current_client()
        await _enforce_rate_limit(device_id, client=client)
        return await actions.ui_dump(device_id, actor=client.name)

    @mcp_server.tool()
    async def tap(device_id: str, x: int, y: int) -> str:
        """Нажать по координатам (x, y) в пикселях; предпочитай look и tap_index скриншоту, скриншот только когда в look нет нужных элементов (игры, картинки, WebView).
Tap screen at (x, y) in pixels; prefer look and tap_index over screenshot, use screenshot only when elements are missing in look (games, images, WebView)."""
        client = get_current_client()
        await _enforce_rate_limit(device_id, client=client)
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
        client = get_current_client()
        await _enforce_rate_limit(device_id, client=client)
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
        client = get_current_client()
        await _enforce_rate_limit(device_id, client=client)
        await actions.swipe(device_id, x1, y1, x2, y2, ms=ms, actor=client.name)
        return f"Свайп ({x1}, {y1}) -> ({x2}, {y2}) выполнен"

    @mcp_server.tool()
    async def type_text(device_id: str, text: str) -> str:
        """Ввести текст в активное поле ввода; перед вводом найди поле через look и нажми на него, предпочитай look скриншоту.
Type text into active input field; before typing find field via look and tap it, prefer look over screenshot."""
        client = get_current_client()
        await _enforce_rate_limit(device_id, client=client)
        await actions.type_text(device_id, text, actor=client.name)
        return f"Введен текст: {text}"

    @mcp_server.tool()
    async def press_key(device_id: str, key: str) -> str:
        """Нажать системную клавишу (back, home, recents, enter, power, volume_up, volume_down); для проверки экрана предпочитай look скриншоту.
Press system key (back, home, recents, enter, power, volume_up, volume_down); to inspect screen prefer look over screenshot."""
        client = get_current_client()
        await _enforce_rate_limit(device_id, client=client)
        await actions.key(device_id, key, actor=client.name)
        return f"Нажата клавиша {key}"

    @mcp_server.tool()
    async def open_app(device_id: str, package: str) -> str:
        """Запустить приложение по имени пакета; после запуска проверяй экран через look, предпочитай look скриншоту.
Open app by package name; after launch inspect screen via look, prefer look over screenshot."""
        client = get_current_client()
        await _enforce_rate_limit(device_id, client=client)
        await actions.open_app(device_id, package, actor=client.name)
        return f"Запущено приложение {package}"

    @mcp_server.tool()
    async def list_apps(device_id: str) -> list[str]:
        """Получить список всех установленных пакетов приложений на устройстве перед вызовом open_app.
Get list of all installed application packages on device before calling open_app."""
        client = get_current_client()
        await _enforce_rate_limit(device_id, client=client)
        return await actions.list_apps(device_id, actor=client.name)

    @mcp_server.tool()
    async def open_url(device_id: str, url: str) -> str:
        """Открыть URL в браузере по умолчанию; после перехода проверяй экран через look, предпочитай look скриншоту.
Open URL in default browser; after navigation inspect screen via look, prefer look over screenshot."""
        client = get_current_client()
        await _enforce_rate_limit(device_id, client=client)
        await actions.open_url(device_id, url, actor=client.name)
        return f"Открыт URL {url}"

    @mcp_server.tool()
    async def wait(device_id: str, seconds: float = 1.0) -> dict[str, Any]:
        """Приостановить выполнение на указанное количество секунд; после ожидания проверяй экран через look, предпочитай look скриншоту.
Pause execution for specified number of seconds; after waiting inspect screen via look, prefer look over screenshot."""
        client = get_current_client()
        await _enforce_rate_limit(device_id, client=client)
        await actions.wait(device_id, seconds, actor=client.name)
        return {"ok": True, "waited": seconds}


# --- Авторизационный ASGI-middleware для MCP ---

class MCPAuthMiddleware:
    """ASGI-слой авторизации MCP: заголовок Authorization: Bearer <token> или секретный путь /mcp/<token>."""

    def __init__(
        self,
        app: Any,
        get_token: Callable[[], str] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.app = app
        self.get_token = get_token
        self.settings = settings

    def _get_settings(self) -> Settings | None:
        if self.settings is not None:
            return self.settings
        try:
            return get_settings()
        except Exception:
            return None

    def _is_server_enabled(self) -> bool:
        if self.get_token is not None:
            legacy_token = (self.get_token() or "").strip()
            if legacy_token:
                return True
        st = self._get_settings()
        if st is not None:
            if getattr(st, "mcp_token", "").strip():
                return True
            if getattr(st, "client_tokens", "").strip():
                return True
        return False

    def _resolve_client(self, token: str) -> Client | None:
        if not token:
            return None
        st = self._get_settings()
        client = _call_resolve_token(token, settings=st)
        if client is not None:
            return client
        if self.get_token is not None:
            expected = (self.get_token() or "").strip()
            if expected and hmac.compare_digest(token, expected):
                return Client(name="mcp")
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            await self.app(scope, receive, send)
            return

        if not self._is_server_enabled():
            if scope_type == "http":
                resp = JSONResponse(
                    {"detail": "MCP сервер отключен (mcp_token не настроен)"},
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
                await resp(scope, receive, send)
            elif scope_type == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return

        path = scope.get("path", "")
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("latin1")

        client: Client | None = None
        new_path = path

        # 1. Заголовок Authorization: Bearer <token>
        if auth_header.lower().startswith("bearer "):
            token_candidate = auth_header[7:].strip()
            if token_candidate:
                client = self._resolve_client(token_candidate)

        # 2. Секретный путь: /mcp/<token> или /<token>
        if client is None:
            candidate_token, candidate_path = self._extract_path_token(path)
            if candidate_token:
                candidate_client = self._resolve_client(candidate_token)
                if candidate_client is not None:
                    client = candidate_client
                    new_path = candidate_path

        if client is None:
            if scope_type == "http":
                resp = JSONResponse(
                    {"detail": "Не авторизован"},
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await resp(scope, receive, send)
            elif scope_type == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return

        # Если обращение шло напрямую к FastMCP приложению по пути /mcp или /mcp/ (например, с Bearer auth)
        if new_path == "/mcp":
            new_path = "/"
        elif new_path.startswith("/mcp/"):
            new_path = new_path[4:]
            if not new_path:
                new_path = "/"

        scope = dict(scope)
        scope["auth_client"] = client
        if new_path != path:
            scope["path"] = new_path
            scope["raw_path"] = new_path.encode("latin1")

        session_id_hdr = headers.get(b"mcp-session-id", b"").decode("latin1").strip()
        if session_id_hdr:
            _session_clients[session_id_hdr] = client

        async def wrapped_send(message: Any) -> None:
            if message.get("type") == "http.response.start":
                for k, v in message.get("headers", []):
                    if k.lower() == b"mcp-session-id":
                        sess_id = v.decode("latin1").strip()
                        if sess_id:
                            _session_clients[sess_id] = client
                        break
            await send(message)

        tok = _current_client.set(client)
        try:
            await self.app(scope, receive, wrapped_send)
        finally:
            _current_client.reset(tok)

    @staticmethod
    def _extract_path_token(path: str) -> tuple[str, str]:
        if path == "/mcp" or path.startswith("/mcp/"):
            subpath = path[4:]
        else:
            subpath = path

        parts = [p.strip() for p in subpath.split("/") if p.strip()]
        if parts:
            candidate = parts[0]
            idx = subpath.find(candidate)
            after = subpath[idx + len(candidate):]
            if not after or after == "/":
                remaining = "/"
            else:
                remaining = after if after.startswith("/") else "/" + after
            return candidate, remaining
        return "", path

    @classmethod
    def _check_and_rewrite_path(cls, path: str, expected_token: str) -> tuple[bool, str]:
        candidate, remaining = cls._extract_path_token(path)
        if candidate and expected_token and hmac.compare_digest(candidate, expected_token):
            return True, remaining
        return False, path


# --- Фабрика создания приложения MCP ---

def create_mcp_app(settings: Settings | None = None) -> tuple[Any, Callable[[], Any]]:
    """Создает приложение ASGI для MCP-сервера с авторизацией и возвращает (asgi_app, lifespan_context_func)."""
    if FastMCP is None or mcp_server is None:
        raise RuntimeError("Пакет mcp не установлен или недоступен")

    kwargs: dict[str, Any] = {"streamable_http_path": "/"}
    if TransportSecuritySettings is not None:
        kwargs["transport_security"] = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )

    starlette_app = mcp_server.streamable_http_app(**kwargs)

    def _get_token() -> str:
        if settings is not None:
            return getattr(settings, "mcp_token", "")
        return get_settings().mcp_token

    auth_app = MCPAuthMiddleware(starlette_app, get_token=_get_token, settings=settings)
    lifespan_ctx = lambda: starlette_app.router.lifespan_context(starlette_app)
    return auth_app, lifespan_ctx


get_mcp_app = create_mcp_app

__all__ = [
    "FastMCP",
    "Image",
    "Client",
    "ClientDeviceRateLimiter",
    "limiter",
    "get_current_client",
    "set_current_client",
    "client_context",
    "resolve_token",
    "mcp_server",
    "mcp",
    "MCPAuthMiddleware",
    "create_mcp_app",
    "get_mcp_app",
    "list_devices",
    "device_status",
    "look",
    "tap_index",
    "screenshot",
    "ui_dump",
    "tap",
    "tap_element",
    "swipe",
    "type_text",
    "press_key",
    "open_app",
    "list_apps",
    "open_url",
    "wait",
]
