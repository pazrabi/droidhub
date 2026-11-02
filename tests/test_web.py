from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
import pytest
from starlette.middleware.sessions import SessionMiddleware

from hub.config import DeviceConfig
from hub.devices import actions
from hub.devices.manager import DeviceManager, DeviceStatus
from hub.web.views import (
    TEMPLATES_DIR,
    get_user_from_request,
    require_user,
    require_web_user,
    web_router,
)


@pytest.fixture
def sample_devices() -> list[DeviceConfig]:
    return [
        DeviceConfig(id="realme", name="Realme C21Y", host="100.64.0.1", port=5555),
        DeviceConfig(id="redmi", name="Redmi Note 11", host="100.64.0.2", port=5555),
    ]


@pytest.fixture
def mock_manager(sample_devices: list[DeviceConfig]) -> DeviceManager:
    manager = DeviceManager(sample_devices)
    # Заполняем кэш статусов для тестирования отображения карточек
    status_realme = DeviceStatus(
        id="realme",
        name="Realme C21Y",
        online=True,
        battery=85,
        temperature=29.5,
        screen_on=True,
        last_seen=1000.0,
        error=None,
    )
    status_redmi = DeviceStatus(
        id="redmi",
        name="Redmi Note 11",
        online=False,
        battery=None,
        temperature=None,
        screen_on=None,
        last_seen=None,
        error="Устройство не отвечает",
    )
    manager._statuses = {
        "realme": status_realme,
        "redmi": status_redmi,
    }
    return manager


def create_web_app(manager: DeviceManager | None = None) -> FastAPI:
    app = FastAPI(title="Phone Hub Web Test")
    app.add_middleware(SessionMiddleware, secret_key="test-web-secret-key-12345")

    if manager is not None:
        app.state.manager = manager
        actions.set_manager(manager)

    app.include_router(web_router)

    static_dir = TEMPLATES_DIR.parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Вспомогательный маршрут для установки сессии в тестах
    @app.post("/test-login")
    async def _test_login(request: Request):
        request.session["user"] = "admin_tester"
        return {"ok": True, "user": "admin_tester"}

    return app


# =====================================================================
# 1. Тесты редиректов без сессии
# =====================================================================


def test_index_unauthorized_redirects_to_login(mock_manager: DeviceManager) -> None:
    """GET / без сессии должен возвращать 303 редирект на /login."""
    app = create_web_app(mock_manager)
    client = TestClient(app)

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_device_unauthorized_redirects_to_login(mock_manager: DeviceManager) -> None:
    """GET /device/{id} без сессии должен возвращать 303 редирект на /login."""
    app = create_web_app(mock_manager)
    client = TestClient(app)

    response = client.get("/device/realme", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_index_unauthorized_follow_redirects_shows_login_page(mock_manager: DeviceManager) -> None:
    """GET / с follow_redirects=True должен приводить на страницу логина."""
    app = create_web_app(mock_manager)
    client = TestClient(app)

    response = client.get("/", follow_redirects=True)
    assert response.status_code == 200
    assert "Вход в Phone Hub" in response.text
    assert 'name="username"' in response.text
    assert 'name="password"' in response.text


# =====================================================================
# 2. Тесты страницы /login
# =====================================================================


def test_login_page_renders_form() -> None:
    """Страница /login должна рендерить форму входа и необходимые поля."""
    app = create_web_app()
    client = TestClient(app)

    response = client.get("/login")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Вход в Phone Hub" in response.text
    assert '<form action="/login" method="post"' in response.text
    assert 'id="username"' in response.text
    assert 'id="password"' in response.text
    assert 'id="btn-login-submit"' in response.text


def test_login_page_renders_error_alert() -> None:
    """При ?error=1 на странице /login должно отображаться сообщение об ошибке."""
    app = create_web_app()
    client = TestClient(app)

    response = client.get("/login?error=1")
    assert response.status_code == 200
    assert 'id="login-error-alert"' in response.text
    assert "Неверный логин или пароль" in response.text


def test_login_page_redirects_if_already_authenticated() -> None:
    """Если пользователь уже авторизован, /login должен редиректить на /."""
    app = create_web_app()
    client = TestClient(app)

    # Авторизуемся через тестовый эндпоинт
    res_login = client.post("/test-login")
    assert res_login.status_code == 200

    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


# =====================================================================
# 3. Тесты главной страницы со списком устройств (/)
# =====================================================================


def test_index_page_with_session(mock_manager: DeviceManager) -> None:
    """GET / с авторизацией должен отображать карточки устройств и их статусы."""
    app = create_web_app(mock_manager)
    client = TestClient(app)

    client.post("/test-login")
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Подключенные устройства" in response.text
    assert "admin_tester" in response.text

    # Проверка карточек устройств
    assert "Realme C21Y" in response.text
    assert "Redmi Note 11" in response.text
    assert "В сети" in response.text
    assert "Не в сети" in response.text
    assert "85%" in response.text
    assert "29.5°C" in response.text

    # Ссылки на экраны устройств
    assert '/device/realme' in response.text
    assert '/device/redmi' in response.text

    # Кнопка выхода
    assert '/logout' in response.text


def test_index_page_empty_devices() -> None:
    """При отсутствии устройств должна отображаться заглушка."""
    empty_manager = DeviceManager([])
    app = create_web_app(empty_manager)
    client = TestClient(app)

    client.post("/test-login")
    response = client.get("/")

    assert response.status_code == 200
    assert "Нет подключенных устройств" in response.text


def test_index_page_with_dependency_override(mock_manager: DeviceManager) -> None:
    """Проверка работы через FastAPI dependency_overrides."""
    app = create_web_app(mock_manager)
    app.dependency_overrides[require_user] = lambda: "mocked_user"
    client = TestClient(app)

    response = client.get("/")
    assert response.status_code == 200
    assert "mocked_user" in response.text


# =====================================================================
# 4. Тесты страницы управления устройством (/device/{id})
# =====================================================================


def test_device_page_renders_all_required_elements(mock_manager: DeviceManager) -> None:
    """
    Страница /device/{id} должна содержать:
    - имя/id устройства
    - контейнер экрана (screen-img)
    - навигационные кнопки Назад, Домой, Недавние, Питание
    - поле ввода текста
    - список приложений и поиск
    """
    app = create_web_app(mock_manager)
    client = TestClient(app)

    client.post("/test-login")
    response = client.get("/device/realme")

    assert response.status_code == 200
    html = response.text

    # 1. Заголовок и id устройства
    assert "Realme C21Y" in html
    assert "realme" in html

    # 2. Экран
    assert 'id="screen-img"' in html
    assert 'id="screen-viewport"' in html

    # 3. Кнопки Назад / Домой / Недавние / Питание
    assert 'id="btn-back"' in html
    assert 'data-key="back"' in html
    assert "Назад" in html

    assert 'id="btn-home"' in html
    assert 'data-key="home"' in html
    assert "Домой" in html

    assert 'id="btn-recents"' in html
    assert 'data-key="recents"' in html
    assert "Недавние" in html

    assert 'id="btn-power"' in html
    assert 'data-key="power"' in html
    assert "Питание" in html

    # 4. Ввод текста
    assert 'id="text-form"' in html
    assert 'id="text-input"' in html
    assert 'id="btn-send-text"' in html

    # 5. Приложения
    assert 'id="apps-list"' in html
    assert 'id="app-search"' in html
    assert 'id="btn-refresh-apps"' in html


def test_device_page_not_found(mock_manager: DeviceManager) -> None:
    """Несуществующее устройство должно возвращать 404."""
    app = create_web_app(mock_manager)
    client = TestClient(app)

    client.post("/test-login")
    response = client.get("/device/unknown_device_123")
    assert response.status_code == 404
    assert "не найдено" in response.text


# =====================================================================
# 5. Тесты статических файлов (CSS и JS)
# =====================================================================


def test_static_files_served() -> None:
    """Проверка отдачи style.css и app.js."""
    app = create_web_app()
    client = TestClient(app)

    res_css = client.get("/static/style.css")
    assert res_css.status_code == 200
    assert "text/css" in res_css.headers["content-type"]
    assert "--bg-body" in res_css.text
    assert "screen-img" in res_css.text

    res_js = client.get("/static/app.js")
    assert res_js.status_code == 200
    assert "javascript" in res_js.headers["content-type"]
    # Проверка ключевой логики в JS
    assert "fetchScreenshot" in res_js.text
    assert "X-Screen-Width" in res_js.text
    assert "X-Screen-Height" in res_js.text
    assert "/api/devices/" in res_js.text
    assert "/screenshot" in res_js.text
    assert "/tap" in res_js.text
    assert "/swipe" in res_js.text
    assert "/key" in res_js.text
    assert "/text" in res_js.text
    assert "/open_app" in res_js.text
    assert "/apps" in res_js.text
