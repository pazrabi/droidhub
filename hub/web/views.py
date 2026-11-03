from __future__ import annotations

from dataclasses import dataclass
import inspect
from pathlib import Path
from typing import Any, Optional
import urllib.parse

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

# Импорт зависимости авторизации по контракту CLAUDE.md / PLAN.md
try:
    from hub.auth import get_sso_user, require_user
except (ImportError, ModuleNotFoundError):
    def require_user(request: Request) -> str:
        """Fallback зависимости require_user, если hub.auth еще не создан."""
        user = None
        if hasattr(request, "session"):
            try:
                user = request.session.get("user")
            except AssertionError:
                user = None
        if user and isinstance(user, str) and user.strip():
            return user.strip()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется авторизация",
        )

    def get_sso_user(request: Request, settings: Any = None) -> Optional[str]:
        return None


def get_settings_from_request(request: Request | None = None) -> Any:
    """Возвращает настройки приложения из request.app.state.settings или get_settings()."""
    if request is not None:
        scope = getattr(request, "scope", None)
        app = scope.get("app") if isinstance(scope, dict) else None
        if app is not None:
            settings = getattr(getattr(app, "state", None), "settings", None)
            if settings is not None:
                return settings

    try:
        from hub.config import get_settings
        return get_settings()
    except Exception:
        return None


def get_current_full_url(request: Request) -> str:
    """Возвращает текущий полный URL с учетом возможных proxy-заголовков."""
    proto = request.headers.get("x-forwarded-proto")
    host = request.headers.get("x-forwarded-host")
    if proto and host:
        path = request.url.path
        query = request.url.query
        return f"{proto}://{host}{path}" + (f"?{query}" if query else "")
    return str(request.url)


def build_sso_redirect_url(request: Request, settings: Any = None) -> str:
    """Формирует URL редиректа на SSO-логин с параметром next=<urlencoded текущий полный URL>."""
    if settings is None:
        settings = get_settings_from_request(request)
    sso_login_url = (getattr(settings, "sso_login_url", "") or "").strip() or "/sso/login"
    current_url = get_current_full_url(request)
    encoded_next = urllib.parse.quote(current_url, safe="")
    sep = "&" if "?" in sso_login_url else "?"
    return f"{sso_login_url}{sep}next={encoded_next}"


def get_login_redirect_url(request: Request, settings: Any = None) -> str:
    """Возвращает URL для перенаправления неавторизованного пользователя (/login или SSO)."""
    if settings is None:
        settings = get_settings_from_request(request)
    sso_secret = (getattr(settings, "sso_secret", "") or "").strip()
    if sso_secret:
        return build_sso_redirect_url(request, settings)
    return "/login"


def get_user_from_request(request: Request) -> Optional[str]:
    """
    Проверяет наличие авторизованного пользователя в сессии или через SSO cookie
    (с поддержкой переопределения через app.dependency_overrides в тестах).
    Возвращает имя пользователя или None.
    """
    scope = getattr(request, "scope", None)
    app = scope.get("app") if isinstance(scope, dict) else None
    override = getattr(app, "dependency_overrides", {}).get(require_user) if app is not None else None
    if override is not None:
        try:
            sig = inspect.signature(override)
            res = override(request) if len(sig.parameters) > 0 else override()
            if isinstance(res, str) and res.strip():
                return res.strip()
            return None
        except HTTPException:
            return None

    try:
        return require_user(request)
    except HTTPException:
        return None


def require_web_user(request: Request) -> str:
    """
    FastAPI dependency для веб-страниц: требует сессию пользователя или SSO,
    иначе инициирует редирект 303 на /login (или SSO-логин).
    """
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": get_login_redirect_url(request)},
        )
    return user


@dataclass
class NavLinkItem:
    """Элемент верхней навигационной полоски инструментов владельца."""
    title: str
    url: str
    is_active: bool = False

    @property
    def name(self) -> str:
        return self.title

    @property
    def active(self) -> bool:
        return self.is_active

    def __getitem__(self, idx: int | str) -> Any:
        if idx == 0 or idx == "title" or idx == "name":
            return self.title
        if idx == 1 or idx == "url":
            return self.url
        if idx == 2 or idx == "is_active" or idx == "active":
            return self.is_active
        raise KeyError(idx)


def get_nav_links(request: Request | None = None) -> list[NavLinkItem]:
    """
    Возвращает список элементов навигационной полоски переключателя инструментов
    владельца (NavLinkItem) на основе настроек приложения или get_settings().
    """
    settings = None
    if request is not None:
        app = getattr(request, "app", None)
        if app is not None:
            settings = getattr(getattr(app, "state", None), "settings", None)

    if settings is None:
        try:
            from hub.config import get_settings
            settings = get_settings()
        except Exception:
            return []

    parsed = getattr(settings, "parsed_nav_links", None)
    if parsed is None:
        try:
            from hub.config import parse_nav_links
            parsed = parse_nav_links(getattr(settings, "nav_links", ""))
        except Exception:
            return []

    if not parsed:
        return []

    active_title = (getattr(settings, "nav_active", "") or "").strip().lower()
    items: list[NavLinkItem] = []
    for title, url in parsed:
        is_active = bool(active_title and title.strip().lower() == active_title)
        items.append(NavLinkItem(title=title, url=url, is_active=is_active))
    return items


TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Регистрация глобальных переменных шаблонизатора Jinja2
templates.env.globals["get_nav_links"] = get_nav_links
templates.env.globals["get_nav_items"] = get_nav_links
try:
    from hub.config import get_settings
    templates.env.globals["get_settings"] = get_settings
except Exception:
    pass

web_router = APIRouter()


@web_router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: Optional[str] = None) -> Response:
    """Страница входа в панель DroidHub."""
    user = get_user_from_request(request)
    if user:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    settings = get_settings_from_request(request)
    sso_secret = (getattr(settings, "sso_secret", "") or "").strip()
    if sso_secret:
        return RedirectResponse(
            url=build_sso_redirect_url(request, settings),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    nav_links = get_nav_links(request)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "error": bool(error),
            "nav_links": nav_links,
            "nav_items": nav_links,
        },
    )


@web_router.get("/", response_class=HTMLResponse)
async def devices_page(request: Request) -> Response:
    """Главная страница панели: список устройств со статусами."""
    user = get_user_from_request(request)
    if not user:
        return RedirectResponse(
            url=get_login_redirect_url(request),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    manager = getattr(request.app.state, "manager", None)
    if manager is None:
        try:
            from hub.devices.actions import get_manager
            manager = get_manager()
        except Exception:
            manager = None

    if manager is None:
        try:
            from hub.config import get_settings, load_devices
            from hub.devices.manager import DeviceManager
            settings = get_settings()
            if settings.devices_file and Path(settings.devices_file).exists():
                manager = DeviceManager(load_devices(settings.devices_file))
        except Exception:
            manager = None

    devices: list[Any] = []
    if manager is not None:
        try:
            devices = manager.statuses()
        except Exception:
            devices = []

    nav_links = get_nav_links(request)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "user": user,
            "devices": devices,
            "nav_links": nav_links,
            "nav_items": nav_links,
        },
    )


@web_router.get("/device/{device_id}", response_class=HTMLResponse)
async def device_page(request: Request, device_id: str) -> Response:
    """Страница управления устройством: экран, кнопки, ввод текста, приложения."""
    user = get_user_from_request(request)
    if not user:
        return RedirectResponse(
            url=get_login_redirect_url(request),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    manager = getattr(request.app.state, "manager", None)
    if manager is None:
        try:
            from hub.devices.actions import get_manager
            manager = get_manager()
        except Exception:
            manager = None

    if manager is None:
        try:
            from hub.config import get_settings, load_devices
            from hub.devices.manager import DeviceManager
            settings = get_settings()
            if settings.devices_file and Path(settings.devices_file).exists():
                manager = DeviceManager(load_devices(settings.devices_file))
        except Exception:
            manager = None

    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Устройство '{device_id}' не найдено",
        )

    try:
        device = manager.get(device_id)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Устройство '{device_id}' не найдено",
        )

    device_status = None
    try:
        for s in manager.statuses():
            sid = s.get("id") if isinstance(s, dict) else getattr(s, "id", None)
            if sid == device_id:
                device_status = s
                break
    except Exception:
        pass

    nav_links = get_nav_links(request)
    return templates.TemplateResponse(
        request=request,
        name="device.html",
        context={
            "user": user,
            "device_id": device_id,
            "device": device,
            "device_status": device_status,
            "nav_links": nav_links,
            "nav_items": nav_links,
        },
    )


__all__ = [
    "web_router",
    "require_user",
    "require_web_user",
    "get_user_from_request",
    "get_settings_from_request",
    "get_current_full_url",
    "build_sso_redirect_url",
    "get_login_redirect_url",
    "templates",
    "get_nav_links",
    "NavLinkItem",
]
