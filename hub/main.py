from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from hub.alerts import AlertMonitor
from hub.api.routes import api_router, public_router
from hub.audit import init_audit
from hub.config import Settings, get_settings, load_devices
from hub.devices import actions
from hub.devices.adb import AdbError
from hub.devices.manager import DeviceManager

try:
    from hub.mcp_server import create_mcp_app
except (ImportError, ModuleNotFoundError):
    create_mcp_app = None  # type: ignore

# Контракты параллельной разработки
try:
    from hub.auth import auth_router
except (ImportError, ModuleNotFoundError):
    from fastapi import APIRouter
    auth_router = APIRouter()

try:
    from hub.web.views import web_router
except (ImportError, ModuleNotFoundError):
    from fastapi import APIRouter
    web_router = APIRouter()

try:
    from hub.sso_routes import sso_router
except (ImportError, ModuleNotFoundError):
    sso_router = None  # type: ignore


def _get_static_dir() -> Path:
    return Path(__file__).resolve().parent / "web" / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Создает и настраивает приложение FastAPI DroidHub по контракту."""
    if settings is None:
        settings = get_settings()

    devices = []
    if settings.devices_file and Path(settings.devices_file).exists():
        try:
            devices = load_devices(settings.devices_file)
        except Exception:
            devices = []

    init_audit(settings.db_path)
    manager = DeviceManager(devices)
    actions.set_manager(manager)
    monitor = AlertMonitor(manager=manager, settings=settings)

    mcp_app = None
    mcp_lifespan_ctx = None
    if create_mcp_app is not None:
        try:
            mcp_app, mcp_lifespan_ctx = create_mcp_app(settings)
        except (ImportError, ModuleNotFoundError, RuntimeError) as exc:
            logging.getLogger(__name__).warning("MCP server initialization failed: %s", exc, exc_info=True)
            mcp_app = None
            mcp_lifespan_ctx = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await manager.start()
        await monitor.start()
        try:
            if mcp_lifespan_ctx is not None:
                async with mcp_lifespan_ctx():
                    yield
            else:
                yield
        finally:
            await monitor.stop()
            await manager.stop()

    app = FastAPI(title="DroidHub", lifespan=lifespan)
    app.state.settings = settings
    app.state.manager = manager
    app.state.monitor = monitor

    # SessionMiddleware
    cookie_secure = os.getenv("HUB_COOKIE_SECURE", "false").lower() in ("true", "1", "yes")
    secret_key = settings.session_secret or "insecure-dev-session-key"
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key,
        https_only=cookie_secure,
    )

    # Подключение роутеров
    app.include_router(public_router)
    app.include_router(api_router)
    app.include_router(auth_router)
    app.include_router(web_router)
    if settings.sso_secret and sso_router is not None:
        app.include_router(sso_router)

    # Статические файлы
    static_dir = _get_static_dir()
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # MCP-сервер
    if mcp_app is not None:
        app.mount("/mcp", mcp_app)

    # Обработчики ошибок
    @app.exception_handler(AdbError)
    async def _adb_error_handler(request: Request, exc: AdbError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(exc) or "Ошибка ADB"})

    @app.exception_handler(KeyError)
    async def _key_error_handler(request: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc) or "Не найдено"})

    return app


def app_factory() -> FastAPI:
    """Ленивая фабрика для запуска uvicorn hub.main:app_factory --factory."""
    return create_app()
