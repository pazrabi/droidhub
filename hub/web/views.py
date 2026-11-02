from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

# Импорт зависимости авторизации по контракту CLAUDE.md / PLAN.md
try:
    from hub.auth import require_user
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


def get_user_from_request(request: Request) -> Optional[str]:
    """
    Проверяет наличие авторизованного пользователя в сессии через require_user
    (с поддержкой переопределения через app.dependency_overrides в тестах).
    Возвращает имя пользователя или None.
    """
    override = getattr(request.app, "dependency_overrides", {}).get(require_user)
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
    FastAPI dependency для веб-страниц: требует сессию пользователя,
    иначе инициирует редирект 303 на /login.
    """
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return user


TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

web_router = APIRouter()


@web_router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: Optional[str] = None) -> Response:
    """Страница входа в панель Phone Hub."""
    user = get_user_from_request(request)
    if user:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": bool(error)},
    )


@web_router.get("/", response_class=HTMLResponse)
async def devices_page(request: Request) -> Response:
    """Главная страница панели: список устройств со статусами."""
    user = get_user_from_request(request)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

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

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "user": user,
            "devices": devices,
        },
    )


@web_router.get("/device/{device_id}", response_class=HTMLResponse)
async def device_page(request: Request, device_id: str) -> Response:
    """Страница управления устройством: экран, кнопки, ввод текста, приложения."""
    user = get_user_from_request(request)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

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
            if s.id == device_id:
                device_status = s
                break
    except Exception:
        pass

    return templates.TemplateResponse(
        request=request,
        name="device.html",
        context={
            "user": user,
            "device_id": device_id,
            "device": device,
            "device_status": device_status,
        },
    )


__all__ = [
    "web_router",
    "require_user",
    "require_web_user",
    "get_user_from_request",
    "templates",
]
