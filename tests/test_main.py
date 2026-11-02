from __future__ import annotations

import os
from pathlib import Path
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.middleware.sessions import SessionMiddleware

# Заглушки параллельных модулей hub.auth и hub.web.views
if "hub.auth" not in sys.modules:
    auth_mod = types.ModuleType("hub.auth")
    auth_mod.auth_router = APIRouter()

    async def require_api() -> str:
        return "test-user"

    auth_mod.require_api = require_api  # type: ignore
    sys.modules["hub.auth"] = auth_mod

if "hub.web.views" not in sys.modules:
    web_mod = types.ModuleType("hub.web.views")
    web_mod.web_router = APIRouter()
    sys.modules["hub.web.views"] = web_mod

import hub.main as main_module
from hub.config import DeviceConfig, Settings
from hub.devices import actions
from hub.devices.adb import AdbError
from hub.devices.manager import DeviceManager
from hub.main import app_factory, create_app


def test_no_module_level_app_instance() -> None:
    """Контракт: app = create_app() на уровне модуля НЕ создавать."""
    assert not hasattr(main_module, "app")
    assert hasattr(main_module, "app_factory")
    assert callable(main_module.app_factory)


def test_app_factory_creates_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """app_factory возвращает экземпляр FastAPI."""
    settings = Settings(db_path=":memory:", devices_file="")
    monkeypatch.setattr("hub.main.get_settings", lambda: settings)

    app = app_factory()
    assert isinstance(app, FastAPI)
    assert app.title == "Phone Hub"


def test_create_app_with_custom_settings(tmp_path: Path) -> None:
    """create_app принимает пользовательские Settings."""
    db_file = tmp_path / "test.db"
    settings = Settings(
        db_path=str(db_file),
        devices_file="",
        session_secret="custom-secret-key",
    )
    app = create_app(settings)
    assert app.state.settings is settings
    assert app.state.settings.session_secret == "custom-secret-key"


def test_lifespan_starts_and_stops_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """Контракт: lifespan запускает и останавливает DeviceManager."""
    mock_start = AsyncMock()
    mock_stop = AsyncMock()
    monkeypatch.setattr(DeviceManager, "start", mock_start)
    monkeypatch.setattr(DeviceManager, "stop", mock_stop)

    settings = Settings(db_path=":memory:", devices_file="")
    app = create_app(settings)

    with TestClient(app):
        mock_start.assert_called_once()
        mock_stop.assert_not_called()

    mock_stop.assert_called_once()


def test_session_middleware_cookie_secure(monkeypatch: pytest.MonkeyPatch) -> None:
    """SessionMiddleware настраивает https_only согласно HUB_COOKIE_SECURE."""
    # 1. HUB_COOKIE_SECURE=true
    monkeypatch.setenv("HUB_COOKIE_SECURE", "true")
    app_secure = create_app(Settings(db_path=":memory:", devices_file=""))
    middleware_secure = [m for m in app_secure.user_middleware if m.cls == SessionMiddleware]
    assert len(middleware_secure) == 1
    assert middleware_secure[0].kwargs["https_only"] is True

    # 2. HUB_COOKIE_SECURE=false
    monkeypatch.setenv("HUB_COOKIE_SECURE", "false")
    app_insecure = create_app(Settings(db_path=":memory:", devices_file=""))
    middleware_insecure = [m for m in app_insecure.user_middleware if m.cls == SessionMiddleware]
    assert len(middleware_insecure) == 1
    assert middleware_insecure[0].kwargs["https_only"] is False


def test_routers_included(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_app подключает public_router, api_router, auth_router, web_router."""
    # Добавим тестовый роут в auth_router и web_router заглушек
    from hub.auth import auth_router  # type: ignore
    from hub.web.views import web_router  # type: ignore

    @auth_router.get("/test-auth-route")
    async def _test_auth():
        return {"auth": True}

    @web_router.get("/test-web-route")
    async def _test_web():
        return {"web": True}

    settings = Settings(db_path=":memory:", devices_file="")
    app = create_app(settings)

    client = TestClient(app)

    # 1. public_router (/api/health)
    res_health = client.get("/api/health")
    assert res_health.status_code == 200
    assert res_health.json() == {"status": "ok"}

    # 2. auth_router
    res_auth = client.get("/test-auth-route")
    assert res_auth.status_code == 200
    assert res_auth.json() == {"auth": True}

    # 3. web_router
    res_web = client.get("/test-web-route")
    assert res_web.status_code == 200
    assert res_web.json() == {"web": True}


def test_static_mounted_if_dir_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Статика /static монтируется, если директория hub/web/static существует."""
    fake_static = tmp_path / "static"
    fake_static.mkdir()
    (fake_static / "style.css").write_text("body { color: black; }", encoding="utf-8")

    monkeypatch.setattr("hub.main._get_static_dir", lambda: fake_static)

    settings = Settings(db_path=":memory:", devices_file="")
    app = create_app(settings)

    # Проверяем, что в app.routes есть роут /static
    route_paths = [getattr(r, "path", None) for r in app.routes]
    assert "/static" in route_paths

    client = TestClient(app)
    res = client.get("/static/style.css")
    assert res.status_code == 200
    assert "color: black" in res.text


def test_exception_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Глобальные обработчики AdbError -> 502 и KeyError -> 404."""
    settings = Settings(db_path=":memory:", devices_file="")
    app = create_app(settings)

    @app.get("/trigger-adb-error")
    async def _trigger_adb():
        raise AdbError("Тестовая ошибка ADB")

    @app.get("/trigger-key-error")
    async def _trigger_key():
        raise KeyError("device_missing")

    client = TestClient(app)

    res_adb = client.get("/trigger-adb-error")
    assert res_adb.status_code == 502
    assert "Тестовая ошибка ADB" in res_adb.json()["detail"]

    res_key = client.get("/trigger-key-error")
    assert res_key.status_code == 404
    assert "device_missing" in res_key.json()["detail"]
