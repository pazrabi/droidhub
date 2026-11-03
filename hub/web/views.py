from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
import urllib.parse

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from hub.auth import get_sso_user, require_user
from hub.config import Settings, get_settings, parse_nav_links

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

web_router = APIRouter()


def get_settings_from_request(request: Request) -> Settings:
    """Настройки приложения: request.app.state.settings, иначе get_settings()."""
    settings = getattr(request.app.state, "settings", None)
    return settings if settings is not None else get_settings()


def get_current_full_url(request: Request) -> str:
    """Текущий полный URL с учётом proxy-заголовков."""
    proto = request.headers.get("x-forwarded-proto")
    host = request.headers.get("x-forwarded-host")
    if proto and host:
        query = request.url.query
        return f"{proto}://{host}{request.url.path}" + (f"?{query}" if query else "")
    return str(request.url)


def build_sso_redirect_url(request: Request, settings: Settings | None = None) -> str:
    """URL единого входа с параметром next=<текущий полный URL>."""
    settings = settings or get_settings_from_request(request)
    login_url = (settings.sso_login_url or "").strip() or "/sso/login"
    sep = "&" if "?" in login_url else "?"
    return f"{login_url}{sep}next={urllib.parse.quote(get_current_full_url(request), safe='')}"


def get_login_redirect_url(request: Request, settings: Settings | None = None) -> str:
    """Куда отправить неавторизованного: единый вход (если настроен SSO) или /login."""
    settings = settings or get_settings_from_request(request)
    if (settings.sso_secret or "").strip():
        return build_sso_redirect_url(request, settings)
    return "/login"


def get_user_from_request(request: Request) -> Optional[str]:
    """Имя пользователя из сессии или SSO-cookie, иначе None. Учитывает dependency_overrides (тесты)."""
    override = request.app.dependency_overrides.get(require_user)
    if override is not None:
        user = override(request) if inspect.signature(override).parameters else override()
        return user.strip() if isinstance(user, str) and user.strip() else None

    sso_user = get_sso_user(request)
    if sso_user:
        return sso_user
    try:
        return require_user(request)
    except HTTPException:
        return None


def require_web_user(request: Request) -> str:
    """Зависимость для веб-страниц: без входа редирект 303 на /login или единый вход."""
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": get_login_redirect_url(request)},
        )
    return user


@dataclass
class NavLinkItem:
    """Вкладка переключателя инструментов владельца."""

    title: str
    url: str
    is_active: bool = False


def get_nav_links(request: Request) -> list[NavLinkItem]:
    """Вкладки из HUB_NAV_LINKS; активная задаётся HUB_NAV_ACTIVE."""
    settings = get_settings_from_request(request)
    active = (settings.nav_active or "").strip().lower()
    return [
        NavLinkItem(title=title, url=url, is_active=bool(active and title.strip().lower() == active))
        for title, url in parse_nav_links(settings.nav_links)
    ]


def _redirect_to_login(request: Request) -> RedirectResponse:
    return RedirectResponse(url=get_login_redirect_url(request), status_code=status.HTTP_303_SEE_OTHER)


def _render(request: Request, name: str, **context: Any) -> Response:
    return templates.TemplateResponse(
        request=request, name=name, context={"nav_links": get_nav_links(request), **context}
    )


@web_router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: Optional[str] = None) -> Response:
    """Страница входа в панель DroidHub (при настроенном SSO уводит на единый вход)."""
    if get_user_from_request(request):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    settings = get_settings_from_request(request)
    if (settings.sso_secret or "").strip():
        return RedirectResponse(
            url=build_sso_redirect_url(request, settings), status_code=status.HTTP_303_SEE_OTHER
        )
    return _render(request, "login.html", error=bool(error))


@web_router.get("/", response_class=HTMLResponse)
async def devices_page(request: Request) -> Response:
    """Главная страница: список устройств со статусами."""
    user = get_user_from_request(request)
    if not user:
        return _redirect_to_login(request)
    manager = getattr(request.app.state, "manager", None)
    devices = manager.statuses() if manager is not None else []
    return _render(request, "index.html", user=user, devices=devices)


@web_router.get("/device/{device_id}", response_class=HTMLResponse)
async def device_page(request: Request, device_id: str) -> Response:
    """Страница управления устройством: экран, кнопки, ввод текста, приложения."""
    user = get_user_from_request(request)
    if not user:
        return _redirect_to_login(request)
    manager = getattr(request.app.state, "manager", None)
    not_found = HTTPException(status.HTTP_404_NOT_FOUND, f"Устройство '{device_id}' не найдено")
    if manager is None:
        raise not_found
    try:
        device = manager.get(device_id)
    except KeyError:
        raise not_found
    device_status = next((s for s in manager.statuses() if s.id == device_id), None)
    return _render(
        request, "device.html",
        user=user, device_id=device_id, device=device, device_status=device_status,
    )


__all__ = [
    "web_router", "require_user", "require_web_user", "get_user_from_request",
    "get_settings_from_request", "get_current_full_url", "build_sso_redirect_url",
    "get_login_redirect_url", "templates", "TEMPLATES_DIR", "get_nav_links", "NavLinkItem",
]
