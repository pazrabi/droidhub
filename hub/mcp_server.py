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
            "Перед выполнением действий (tap, swipe, type_text) всегда вызывайте ui_dump или screenshot "
            "для получения точных координат и актуального состояния экрана."
        ),
    )
else:
    mcp_server = None  # type: ignore

mcp = mcp_server


# --- Регистрация инструментов ---

if mcp_server is not None:

    @mcp_server.tool()
    async def list_devices(device_id: str = "") -> list[dict[str, Any]]:
        """Получить список всех настроенных Android-устройств и их текущие статусы.

        Возвращает список устройств с параметрами: id, name, online, battery (заряд в %),
        temperature (°C), screen_on. Используйте этот инструмент первым, чтобы узнать доступные device_id.
        """
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
        """Получить детальный статус конкретного Android-устройства по device_id.

        Возвращает параметры: id, name, online (доступен ли по ADB), battery (уровень заряда в %),
        temperature (температура батареи в °C), screen_on (включен ли экран), last_seen, error.
        """
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
    async def screenshot(device_id: str, max_width: int = 720) -> Image:
        """Сделать скриншот экрана Android-устройства и вернуть изображение JPEG.

        ОБЯЗАТЕЛЬНО делайте screenshot или ui_dump перед выполнением любых действий (tap, swipe, type_text),
        чтобы увидеть текущее состояние экрана. Координаты элементов на экране измеряются в пикселях.
        """
        await _enforce_rate_limit(device_id)
        jpeg_bytes = await actions.screenshot(device_id, max_width=max_width, actor="mcp")
        if Image is not None:
            return Image(data=jpeg_bytes, format="jpeg")
        return jpeg_bytes  # type: ignore

    @mcp_server.tool()
    async def ui_dump(device_id: str) -> list[dict[str, Any]]:
        """Получить дамп дерева элементов интерфейса (UI hierarchy) текущего экрана.

        Возвращает список элементов: text, resource_id, class, content_desc, clickable,
        bounds ([x1, y1, x2, y2] в пикселях экрана) и center ([cx, cy] в пикселях экрана).
        Это ОСНОВНОЙ инструмент: всегда вызывайте ui_dump перед tap, tap_element или type_text,
        чтобы найти точные координаты или идентификаторы кнопок и полей ввода.
        """
        await _enforce_rate_limit(device_id)
        return await actions.ui_dump(device_id, actor="mcp")

    @mcp_server.tool()
    async def tap(device_id: str, x: int, y: int) -> dict[str, Any]:
        """Выполнить нажатие (тап) по координатам (x, y) на экране устройства.

        Координаты указываются в пикселях экрана. Перед нажатием ОБЯЗАТЕЛЬНО вызовите
        ui_dump или screenshot для определения точных пиксельных координат нужного элемента.
        """
        await _enforce_rate_limit(device_id)
        await actions.tap(device_id, x, y, actor="mcp")
        return {"ok": True, "x": x, "y": y}

    @mcp_server.tool()
    async def tap_element(
        device_id: str,
        text: str | None = None,
        resource_id: str | None = None,
    ) -> dict[str, Any]:
        """Найти элемент интерфейса по тексту (text) или идентификатору (resource_id) и нажать на его центр.

        Выполняет ui_dump и нажимает на центр найденного элемента. Координаты вычисляются в пикселях экрана.
        Рекомендуется сначала вызвать ui_dump, чтобы узнать точный текст или resource_id элемента.
        """
        await _enforce_rate_limit(device_id)
        return await actions.tap_element(device_id, text=text, resource_id=resource_id, actor="mcp")

    @mcp_server.tool()
    async def swipe(
        device_id: str,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        ms: int = 300,
    ) -> dict[str, Any]:
        """Выполнить свайп от точки (x1, y1) до точки (x2, y2) на экране за время ms (в миллисекундах).

        Все координаты указываются в пикселях экрана. Перед выполнением свайпа рекомендуется
        определить границы прокручиваемой области через ui_dump или screenshot.
        """
        await _enforce_rate_limit(device_id)
        await actions.swipe(device_id, x1, y1, x2, y2, ms=ms, actor="mcp")
        return {"ok": True}

    @mcp_server.tool()
    async def type_text(device_id: str, text: str) -> dict[str, Any]:
        """Ввести текст в активное поле ввода на устройстве.

        Поддерживает латиницу, кириллицу, спецсимволы и эмодзи. Перед вводом текста
        необходимо активировать нужное поле ввода тапом (tap или tap_element), предварительно
        определив его координаты через ui_dump или screenshot.
        """
        await _enforce_rate_limit(device_id)
        await actions.type_text(device_id, text, actor="mcp")
        return {"ok": True, "text": text}

    @mcp_server.tool()
    async def press_key(device_id: str, key: str) -> dict[str, Any]:
        """Нажать аппаратную или системную клавишу на устройстве.

        Допустимые клавиши: 'back' (назад), 'home' (домой), 'recents' (недавние приложения),
        'enter', 'power' (питание), 'volume_up', 'volume_down'. После нажатия клавиши
        рекомендуется вызвать wait, а затем ui_dump или screenshot.
        """
        await _enforce_rate_limit(device_id)
        await actions.key(device_id, key, actor="mcp")
        return {"ok": True, "key": key}

    @mcp_server.tool()
    async def open_app(device_id: str, package: str) -> dict[str, Any]:
        """Запустить приложение на устройстве по имени пакета (например, 'com.android.chrome').

        Список установленных пакетов можно получить через list_apps.
        После запуска приложения вызовите wait, затем ui_dump или screenshot.
        """
        await _enforce_rate_limit(device_id)
        await actions.open_app(device_id, package, actor="mcp")
        return {"ok": True, "package": package}

    @mcp_server.tool()
    async def list_apps(device_id: str) -> list[str]:
        """Получить список всех установленных пакетов приложений на устройстве.

        Используйте этот инструмент перед вызовом open_app для нахождения точного имени пакета.
        """
        await _enforce_rate_limit(device_id)
        return await actions.list_apps(device_id, actor="mcp")

    @mcp_server.tool()
    async def open_url(device_id: str, url: str) -> dict[str, Any]:
        """Открыть URL-ссылку в браузере по умолчанию на устройстве.

        После перехода по ссылке рекомендуется подождать загрузки через wait,
        а затем проверить результат через ui_dump или screenshot.
        """
        await _enforce_rate_limit(device_id)
        await actions.open_url(device_id, url, actor="mcp")
        return {"ok": True, "url": url}

    @mcp_server.tool()
    async def wait(device_id: str, seconds: float = 1.0) -> dict[str, Any]:
        """Приостановить выполнение на указанное количество секунд (seconds).

        Вызывайте после действий (open_app, open_url, tap, swipe), чтобы дать приложению
        время загрузиться перед следующим ui_dump или screenshot.
        """
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
