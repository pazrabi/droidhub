from __future__ import annotations

import asyncio
from collections import defaultdict
import dataclasses
import hmac
import time
from typing import Any, Callable

from fastapi import status
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

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


# --- Вспомогательный локальный ограничитель (fallback, если limiter недоступен) ---

class _LocalDeviceLimiter:
    def __init__(self, window_seconds: float = 60.0) -> None:
        self.window_seconds = window_seconds
        self._history: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def check(self, device_id: str, limit: int | None = None) -> bool:
        if limit is None:
            try:
                settings = get_settings()
                limit = getattr(settings, "rate_limit_per_min", 60)
            except Exception:
                limit = 60
        if limit <= 0:
            return True

        now = time.monotonic()
        cutoff = now - self.window_seconds

        async with self._lock:
            history = [t for t in self._history[device_id] if t > cutoff]
            if len(history) >= limit:
                self._history[device_id] = history
                return False
            history.append(now)
            self._history[device_id] = history
            return True


_fallback_limiter = _LocalDeviceLimiter()


async def _enforce_rate_limit(device_id: str) -> None:
    """Проверяет лимит действий в минуту на устройство."""
    if not device_id:
        return
    active_limiter = limiter if limiter is not None else _fallback_limiter
    allowed = await active_limiter.check(device_id)
    if not allowed:
        raise RuntimeError(f"Превышен лимит действий на устройство '{device_id}'")


async def _log_audit_safe(
    device_id: str | None,
    action: str,
    params: dict[str, Any],
    ok: bool,
    error: str | None = None,
) -> None:
    """Безопасная запись события аудита с actor='mcp'."""
    try:
        if audit_module is not None:
            aud = audit_module.get_audit()
            if aud is not None:
                await aud.log(
                    actor="mcp",
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
        "phone-hub",
        instructions=(
            "Сервер Phone Hub для удаленного управления Android-телефонами через ADB. "
            "Главный инструмент просмотра экрана — look: компактный и быстрый список элементов с индексами и координатами. "
            "Предпочитайте look скриншоту; скриншот использовать только когда в look нет нужных элементов (игры, картинки, WebView). "
            "Для нажатия элементов используйте tap_index по номерам из последнего вызова look."
        ),
    )
else:
    mcp_server = None  # type: ignore

mcp = mcp_server


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
        await _log_audit_safe(device_id or None, "list_devices", {"device_id": device_id}, ok=True)
        return results

    @mcp_server.tool()
    async def device_status(device_id: str) -> dict[str, Any]:
        """Получить детальный статус Android-устройства (батарея, температура, экран) по device_id.
Get detailed Android device status (battery, temperature, screen) by device_id."""
        await _enforce_rate_limit(device_id)
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
            await _log_audit_safe(device_id, "device_status", {"device_id": device_id}, ok=True)
            return res
        except Exception as e:
            await _log_audit_safe(device_id, "device_status", {"device_id": device_id}, ok=False, error=str(e))
            raise

    @mcp_server.tool()
    async def look(device_id: str) -> str:
        """Главный инструмент «посмотреть на экран»: компактный текстовый список элементов в пикселях; предпочитай look скриншоту, скриншот только когда в look нет нужных элементов (игры, картинки, WebView).
Main tool to view screen: compact text list of elements in pixels; prefer look over screenshot, use screenshot only when elements are missing in look (games, images, WebView)."""
        await _enforce_rate_limit(device_id)
        raw_dump = await actions.ui_dump(device_id, actor="mcp")

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
        await _log_audit_safe(device_id, "look", {"device_id": device_id, "count": len(displayed)}, ok=True)

        if not lines:
            return "(нет значимых элементов)"
        return "\n".join(lines)

    @mcp_server.tool()
    async def tap_index(device_id: str, index: int) -> str:
        """Нажать элемент по номеру [N] из последнего look устройства (координаты в пикселях); предпочитай look и tap_index скриншоту.
Tap element by index [N] from last look on device (coordinates in pixels); prefer look and tap_index over screenshot."""
        await _enforce_rate_limit(device_id)
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
        await actions.tap(device_id, cx, cy, actor="mcp")
        await _log_audit_safe(device_id, "tap_index", {"device_id": device_id, "index": idx, "x": cx, "y": cy}, ok=True)
        return f"Тап по элементу [{idx}] ({cx}, {cy}) выполнен"

    @mcp_server.tool()
    async def screenshot(device_id: str, max_width: int = 540) -> Image:
        """Сделать скриншот экрана в пикселях JPEG; предпочитай look скриншоту, скриншот использовать только когда в look нет нужных элементов (игры, картинки, WebView).
Take JPEG screenshot in pixels; prefer look over screenshot, use screenshot only when needed elements are missing in look (games, images, WebView)."""
        await _enforce_rate_limit(device_id)
        jpeg_bytes = await actions.screenshot(device_id, max_width=max_width, actor="mcp")
        if Image is not None:
            return Image(data=jpeg_bytes, format="jpeg")
        return jpeg_bytes  # type: ignore

    @mcp_server.tool()
    async def ui_dump(device_id: str) -> list[dict[str, Any]]:
        """Получить полное дерево элементов интерфейса с координатами в пикселях; для большинства задач предпочитай look.
Get full UI hierarchy tree with pixel coordinates; for most tasks prefer look over screenshot and ui_dump."""
        await _enforce_rate_limit(device_id)
        return await actions.ui_dump(device_id, actor="mcp")

    @mcp_server.tool()
    async def tap(device_id: str, x: int, y: int) -> str:
        """Нажать по координатам (x, y) в пикселях; предпочитай look и tap_index скриншоту, скриншот только когда в look нет нужных элементов (игры, картинки, WebView).
Tap screen at (x, y) in pixels; prefer look and tap_index over screenshot, use screenshot only when elements are missing in look (games, images, WebView)."""
        await _enforce_rate_limit(device_id)
        await actions.tap(device_id, x, y, actor="mcp")
        return f"Тап ({x}, {y}) выполнен"

    @mcp_server.tool()
    async def tap_element(
        device_id: str,
        text: str | None = None,
        resource_id: str | None = None,
    ) -> str:
        """Найти элемент по тексту или id и нажать на его центр в пикселях; предпочитай look и tap_index скриншоту, скриншот только для игр, картинок, WebView.
Find element by text or id and tap its pixel center; prefer look and tap_index over screenshot, screenshot only for games, images, WebView."""
        await _enforce_rate_limit(device_id)
        elem = await actions.tap_element(device_id, text=text, resource_id=resource_id, actor="mcp")
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
        await _enforce_rate_limit(device_id)
        await actions.swipe(device_id, x1, y1, x2, y2, ms=ms, actor="mcp")
        return f"Свайп ({x1}, {y1}) -> ({x2}, {y2}) выполнен"

    @mcp_server.tool()
    async def type_text(device_id: str, text: str) -> str:
        """Ввести текст в активное поле ввода; перед вводом найди поле через look и нажми на него, предпочитай look скриншоту.
Type text into active input field; before typing find field via look and tap it, prefer look over screenshot."""
        await _enforce_rate_limit(device_id)
        await actions.type_text(device_id, text, actor="mcp")
        return f"Введен текст: {text}"

    @mcp_server.tool()
    async def press_key(device_id: str, key: str) -> str:
        """Нажать системную клавишу (back, home, recents, enter, power, volume_up, volume_down); для проверки экрана предпочитай look скриншоту.
Press system key (back, home, recents, enter, power, volume_up, volume_down); to inspect screen prefer look over screenshot."""
        await _enforce_rate_limit(device_id)
        await actions.key(device_id, key, actor="mcp")
        return f"Нажата клавиша {key}"

    @mcp_server.tool()
    async def open_app(device_id: str, package: str) -> str:
        """Запустить приложение по имени пакета; после запуска проверяй экран через look, предпочитай look скриншоту.
Open app by package name; after launch inspect screen via look, prefer look over screenshot."""
        await _enforce_rate_limit(device_id)
        await actions.open_app(device_id, package, actor="mcp")
        return f"Запущено приложение {package}"

    @mcp_server.tool()
    async def list_apps(device_id: str) -> list[str]:
        """Получить список всех установленных пакетов приложений на устройстве перед вызовом open_app.
Get list of all installed application packages on device before calling open_app."""
        await _enforce_rate_limit(device_id)
        return await actions.list_apps(device_id, actor="mcp")

    @mcp_server.tool()
    async def open_url(device_id: str, url: str) -> str:
        """Открыть URL в браузере по умолчанию; после перехода проверяй экран через look, предпочитай look скриншоту.
Open URL in default browser; after navigation inspect screen via look, prefer look over screenshot."""
        await _enforce_rate_limit(device_id)
        await actions.open_url(device_id, url, actor="mcp")
        return f"Открыт URL {url}"

    @mcp_server.tool()
    async def wait(device_id: str, seconds: float = 1.0) -> dict[str, Any]:
        """Приостановить выполнение на указанное количество секунд; после ожидания проверяй экран через look, предпочитай look скриншоту.
Pause execution for specified number of seconds; after waiting inspect screen via look, prefer look over screenshot."""
        await _enforce_rate_limit(device_id)
        await actions.wait(device_id, seconds, actor="mcp")
        return {"ok": True, "waited": seconds}


# --- Авторизационный ASGI-middleware для MCP ---

class MCPAuthMiddleware:
    """ASGI-слой авторизации MCP: заголовок Authorization: Bearer <mcp_token> или секретный путь /mcp/<token>."""

    def __init__(self, app: Any, get_token: Callable[[], str]) -> None:
        self.app = app
        self.get_token = get_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            await self.app(scope, receive, send)
            return

        expected_token = (self.get_token() or "").strip()
        if not expected_token:
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

        authorized = False
        new_path = path

        # 1. Заголовок Authorization: Bearer <mcp_token>
        if auth_header.lower().startswith("bearer "):
            token_candidate = auth_header[7:].strip()
            if hmac.compare_digest(token_candidate, expected_token):
                authorized = True

        # 2. Секретный путь: /mcp/<mcp_token> или /<mcp_token>
        path_matched, candidate_path = self._check_and_rewrite_path(path, expected_token)
        if path_matched:
            authorized = True
            new_path = candidate_path

        if not authorized:
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

        if new_path != path:
            scope = dict(scope)
            scope["path"] = new_path
            scope["raw_path"] = new_path.encode("latin1")

        await self.app(scope, receive, send)

    @staticmethod
    def _check_and_rewrite_path(path: str, expected_token: str) -> tuple[bool, str]:
        if path == "/mcp" or path.startswith("/mcp/"):
            subpath = path[4:]
        else:
            subpath = path

        parts = [p for p in subpath.split("/") if p]
        if parts:
            candidate = parts[0]
            if hmac.compare_digest(candidate, expected_token):
                remaining = subpath[len("/" + candidate):]
                if not remaining:
                    remaining = "/"
                elif not remaining.startswith("/"):
                    remaining = "/" + remaining
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

    auth_app = MCPAuthMiddleware(starlette_app, get_token=_get_token)
    lifespan_ctx = lambda: starlette_app.router.lifespan_context(starlette_app)
    return auth_app, lifespan_ctx


get_mcp_app = create_mcp_app

__all__ = [
    "FastMCP",
    "Image",
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
