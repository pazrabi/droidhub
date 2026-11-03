from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
import pytest
from starlette.middleware.sessions import SessionMiddleware

import hub.auth
from hub import sso
from hub.config import DeviceConfig, Settings
from hub.devices import actions
from hub.devices.manager import DeviceManager, DeviceStatus
from hub.web.views import (
    TEMPLATES_DIR,
    get_user_from_request,
    require_user,
    require_web_user,
    web_router,
)


JS_FILES = ("common.js", "devices-list.js", "device-page.js")


def get_all_js(client: TestClient) -> str:
    """Весь клиентский JS (app.js и модули js/*.js) одним текстом."""
    parts = [client.get("/static/app.js")] + [client.get(f"/static/js/{name}") for name in JS_FILES]
    for res in parts:
        assert res.status_code == 200
        assert "javascript" in res.headers["content-type"]
    return "\n".join(res.text for res in parts)


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
    app = FastAPI(title="DroidHub Web Test")
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
    assert "Вход в DroidHub" in response.text
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
    assert "Вход в DroidHub" in response.text
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

    # 6. Элементы адаптивности и отклика
    assert 'id="quality-select"' in html
    assert 'value="360"' in html
    assert 'value="540"' in html
    assert 'value="720"' in html
    assert 'id="stream-updating"' in html
    assert 'id="frame-age"' in html

    # 7. Блок «Питание» и кнопки управления питанием
    assert 'id="power-card"' in html
    assert "Питание" in html
    assert 'id="btn-power-wake"' in html
    assert 'data-power="wake"' in html
    assert "Разбудить" in html
    assert 'id="btn-power-sleep"' in html
    assert 'data-power="sleep"' in html
    assert "Экран выкл." in html
    assert 'id="btn-power-reboot"' in html
    assert 'data-power="reboot"' in html
    assert "Перезагрузить" in html
    assert 'id="btn-power-shutdown"' in html
    assert 'data-power="shutdown"' in html
    assert "Выключить телефон" in html

    # 8. Телеметрия устройства (заряд и температура)
    assert 'id="device-telemetry"' in html
    assert 'id="device-battery-val"' in html
    assert "85%" in html
    assert 'id="device-temp-val"' in html
    assert "29.5°C" in html
    assert "temp-neutral" in html
    assert 'id="device-overheat-badge"' in html


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

    js = get_all_js(client)
    # Проверка ключевой логики в JS
    assert "fetchScreenshot" in js
    assert "X-Screen-Width" in js
    assert "X-Screen-Height" in js
    assert "/api/devices/" in js
    assert "/screenshot" in js
    assert "/tap" in js
    assert "/swipe" in js
    assert "/key" in js
    assert "/text" in js
    assert "/open_app" in js
    assert "/apps" in js

    # Проверка механизмов быстрого отклика и адаптивности
    assert "needsAnotherFrame" in js
    assert "targetIntervalMs" in js
    assert "stream-updating" in js
    assert "frame-age" in js
    assert "showTapEffect" in js
    assert "600" in js
    assert "quality-select" in js
    assert "localStorage" in js
    assert "visibilitychange" in js
    assert "document.hidden" in js


def test_responsive_ui_and_adaptive_streaming(mock_manager: DeviceManager) -> None:
    """
    Проверка требований по улучшению отклика:
    (1) немедленный запрос кадра без наложения (needsAnotherFrame, requestImmediateFrame);
    (2) адаптивный интервал таймера;
    (3) индикатор «обновляется…» и возраст кадра («кадр N с назад»);
    (4) визуальный отклик при тапе (исчезает через 600 мс);
    (5) селектор качества (360/540/720), сохранение в localStorage, max_width;
    (6) пауза при скрытой вкладке (visibilitychange).
    """
    app = create_web_app(mock_manager)
    client = TestClient(app)

    # 1. Проверка HTML страницы устройства
    client.post("/test-login")
    res_page = client.get("/device/realme")
    assert res_page.status_code == 200
    html = res_page.text

    # Селектор качества в панели
    assert 'id="quality-select"' in html
    assert '<option value="360"' in html
    assert '<option value="540"' in html
    assert '<option value="720"' in html

    # Индикаторы обновления и возраста кадра
    assert 'id="stream-updating"' in html
    assert "обновляется" in html
    assert 'id="frame-age"' in html

    # Кружок касания
    assert 'id="tap-indicator"' in html

    # 2. Проверка JS
    js = get_all_js(client)

    # (1) Запрос без ожидания таймера и флаг «нужен ещё один»
    assert "requestImmediateFrame" in js
    assert "needsAnotherFrame" in js

    # (2) Адаптивный интервал: max(0, целевой интервал − время предыдущего кадра), целевой 1 с
    assert "targetIntervalMs = 1000" in js or "targetIntervalMs" in js
    assert "Math.max(0," in js

    # (3) Индикатор «обновляется…» и возраст кадра («кадр N с назад»)
    assert "stream-updating" in js
    assert "обновляется" in js
    assert "frame-age" in js
    assert "с назад" in js

    # (4) Исчезновение кружка касания через 600 мс
    assert "600" in js
    assert "showTapEffect" in js

    # (5) Селектор качества, localStorage и max_width
    assert "localStorage" in js
    assert "max_width" in js
    assert "quality-select" in js

    # (6) Пауза при скрытой вкладке
    assert "visibilitychange" in js
    assert "document.hidden" in js


# =====================================================================
# 6. Тесты управления питанием, телеметрии и алертов перегрева (Этап 3.5)
# =====================================================================


def test_index_page_temperature_ranges_and_overheat() -> None:
    """
    Проверка цветовой индикации температуры и кнопки выключения на карточках:
    - < 40 °C: нейтрально (temp-neutral), без пометки ПЕРЕГРЕВ, кнопка «Выключить» скрыта;
    - 40–44 °C: жёлтый (temp-warning), без пометки ПЕРЕГРЕВ, кнопка «Выключить» скрыта;
    - ≥ 45 °C: красный (temp-danger), с пометкой «ПЕРЕГРЕВ» и заметной кнопкой «Выключить» прямо в карточке.
    """
    dev_normal = DeviceConfig(id="dev_normal", name="Normal Phone", host="100.64.0.10")
    dev_warn = DeviceConfig(id="dev_warn", name="Warm Phone", host="100.64.0.11")
    dev_hot = DeviceConfig(id="dev_hot", name="Hot Phone", host="100.64.0.12")

    manager = DeviceManager([dev_normal, dev_warn, dev_hot])
    manager._statuses = {
        "dev_normal": DeviceStatus(
            id="dev_normal",
            name="Normal Phone",
            online=True,
            battery=75,
            temperature=36.5,
            screen_on=True,
            last_seen=1000.0,
            error=None,
        ),
        "dev_warn": DeviceStatus(
            id="dev_warn",
            name="Warm Phone",
            online=True,
            battery=50,
            temperature=42.0,
            screen_on=True,
            last_seen=1000.0,
            error=None,
        ),
        "dev_hot": DeviceStatus(
            id="dev_hot",
            name="Hot Phone",
            online=True,
            battery=90,
            temperature=47.5,
            screen_on=True,
            last_seen=1000.0,
            error=None,
        ),
    }

    app = create_web_app(manager)
    client = TestClient(app)
    client.post("/test-login")

    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    # 1. dev_normal (< 40 °C): нейтрально, кнопка выключения скрыта
    assert "36.5°C" in html
    assert 'id="temp-dev_normal"' in html
    assert "temp-neutral" in html
    # Бейдж перегрева и кнопка выключения скрыты
    assert 'id="overheat-dev_normal" style="display: none;"' in html
    assert 'id="btn-shutdown-dev_normal"' in html
    assert 'id="btn-shutdown-dev_normal" data-device-id="dev_normal" data-device-name="Normal Phone" style="display: none;"' in html

    # 2. dev_warn (40–44 °C): жёлтый (temp-warning), перегрев скрыт
    assert "42.0°C" in html or "42°C" in html
    assert 'id="temp-dev_warn"' in html
    assert "temp-warning" in html
    assert 'id="overheat-dev_warn" style="display: none;"' in html
    assert 'id="btn-shutdown-dev_warn" data-device-id="dev_warn" data-device-name="Warm Phone" style="display: none;"' in html

    # 3. dev_hot (≥ 45 °C): красный (temp-danger), пометка «ПЕРЕГРЕВ», заметная кнопка «Выключить»
    assert "47.5°C" in html
    assert 'id="temp-dev_hot"' in html
    assert "temp-danger" in html
    assert 'id="overheat-dev_hot" style=""' in html or 'id="overheat-dev_hot">' in html or ('id="overheat-dev_hot"' in html and 'style="display: none;"' not in html.split('id="overheat-dev_hot"')[1].split('>')[0])
    assert "ПЕРЕГРЕВ" in html
    # Кнопка «Выключить» в карточке dev_hot не скрыта
    assert 'id="btn-shutdown-dev_hot"' in html
    # Проверяем, что у кнопки dev_hot нет style="display: none;"
    btn_hot_tag = html.split('id="btn-shutdown-dev_hot"')[1].split('>')[0]
    assert 'display: none' not in btn_hot_tag
    assert "Выключить" in html


def test_device_page_overheat_state() -> None:
    """На странице перегретого устройства отображаются красный цвет температуры и бейдж «ПЕРЕГРЕВ»."""
    dev_hot = DeviceConfig(id="hot_phone", name="Hot Phone", host="100.64.0.12")
    manager = DeviceManager([dev_hot])
    manager._statuses = {
        "hot_phone": DeviceStatus(
            id="hot_phone",
            name="Hot Phone",
            online=True,
            battery=99,
            temperature=48.2,
            screen_on=True,
            last_seen=1000.0,
            error=None,
        ),
    }

    app = create_web_app(manager)
    client = TestClient(app)
    client.post("/test-login")

    response = client.get("/device/hot_phone")
    assert response.status_code == 200
    html = response.text

    assert "48.2°C" in html
    assert "temp-danger" in html
    assert 'id="device-overheat-badge"' in html
    assert "ПЕРЕГРЕВ" in html
    # Проверяем, что у бейджа нет display: none
    badge_tag = html.split('id="device-overheat-badge"')[1].split('>')[0]
    assert "display: none" not in badge_tag


def test_power_confirmation_modal_and_warning_text(mock_manager: DeviceManager) -> None:
    """
    Проверка модального окна и предупреждения:
    - Модальное окно в DOM (base.html) с кнопками Отмена и Подтвердить;
    - Чёткое русское предупреждение в JS:
      «Выключенный телефон нельзя включить удалённо. Зарядка при подключённом кабеле продолжится. Выключить?»
    """
    app = create_web_app(mock_manager)
    client = TestClient(app)
    client.post("/test-login")

    # 1. Проверяем наличие разметки модального окна на странице
    response = client.get("/device/realme")
    assert response.status_code == 200
    html = response.text

    assert 'id="power-modal"' in html
    assert 'id="modal-title"' in html
    assert 'id="modal-warning-text"' in html
    assert 'id="modal-btn-confirm"' in html
    assert 'id="modal-btn-cancel"' in html
    assert 'id="modal-btn-close"' in html

    # 2. Проверяем наличие предупреждения в app.js
    js = get_all_js(client)

    expected_warning = (
        "Выключенный телефон нельзя включить удалённо. "
        "Зарядка при подключённом кабеле продолжится. Выключить?"
    )
    assert expected_warning in js
    assert "PowerModal" in js


def test_power_js_logic_and_polling_contract() -> None:
    """
    Проверка клиентского JS контракта:
    (1) Запрос POST /api/devices/{id}/power с {"action": ..., "confirm": true};
    (2) Действия: wake, sleep, reboot, shutdown;
    (3) Обновление значений с тем же интервалом, что и список устройств, без накладывания запросов;
    (4) Обработка порогов температуры: 40 и 45 °C с пометкой ПЕРЕГРЕВ.
    """
    app = create_web_app()
    client = TestClient(app)

    js = get_all_js(client)

    # (1) Эндпоинт POST /power и confirm: true
    assert "/power" in js
    assert "sendPowerAction" in js
    assert "POST" in js
    assert "action" in js
    assert "confirm" in js

    # (2) Действия питания
    assert "wake" in js
    assert "sleep" in js
    assert "reboot" in js
    assert "shutdown" in js

    # (3) Одинаковый интервал обновления без накладывания запросов
    assert "DEVICES_POLL_INTERVAL_MS" in js
    assert "isUpdatingDevices" in js
    assert "isFetchingStatus" in js

    # (4) Пороги температуры и ПЕРЕГРЕВ
    assert "45" in js
    assert "40" in js
    assert "temp-danger" in js
    assert "temp-warning" in js
    assert "temp-neutral" in js
    assert "isOverheat" in js


def test_power_css_styling() -> None:
    """Проверка наличия стилей для блоков питания, модального окна и цветов температуры."""
    app = create_web_app()
    client = TestClient(app)

    res_css = client.get("/static/style.css")
    assert res_css.status_code == 200
    css = res_css.text

    assert ".temp-neutral" in css
    assert ".temp-warning" in css
    assert ".temp-danger" in css
    assert ".badge-overheat" in css
    assert ".btn-shutdown-card" in css
    assert ".power-card" in css
    assert ".power-buttons-grid" in css
    assert ".btn-power-shutdown" in css
    assert ".modal-backdrop" in css
    assert ".modal-window" in css
    assert ".modal-warning-text" in css


# =====================================================================
# 10. Тесты верхней полоски переключателя инструментов владельца
# =====================================================================


def test_owner_nav_bar_absent_when_nav_links_empty(
    mock_manager: DeviceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """При пустом nav_links полоска вкладок отсутствует на всех страницах."""
    monkeypatch.delenv("HUB_NAV_LINKS", raising=False)
    monkeypatch.delenv("HUB_NAV_ACTIVE", raising=False)

    app = create_web_app(mock_manager)
    client = TestClient(app)

    # 1. На странице логина
    res_login = client.get("/login")
    assert res_login.status_code == 200
    assert "owner-nav-bar" not in res_login.text
    assert "owner-nav-tab" not in res_login.text

    # 2. На главной странице
    client.post("/test-login")
    res_index = client.get("/")
    assert res_index.status_code == 200
    assert "owner-nav-bar" not in res_index.text
    assert "owner-nav-tab" not in res_index.text

    # 3. На странице устройства
    res_device = client.get("/device/realme")
    assert res_device.status_code == 200
    assert "owner-nav-bar" not in res_device.text
    assert "owner-nav-tab" not in res_device.text


def test_owner_nav_bar_present_when_nav_links_set(
    mock_manager: DeviceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    При заданных ссылках полоска отображается:
    - активная вкладка подсвечена (.active) и не кликабельна (без тега <a>/href),
    - остальные ссылки открываются в той же вкладке (без target=_blank, без rel="noopener").
    """
    monkeypatch.setenv(
        "HUB_NAV_LINKS",
        "Gemini Pool|https://pool.molokohub.site/,DroidHub|https://droidhub.molokohub.site/",
    )
    monkeypatch.setenv("HUB_NAV_ACTIVE", "DroidHub")

    app = create_web_app(mock_manager)
    client = TestClient(app)
    client.post("/test-login")

    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    # Полоска присутствует
    assert 'class="owner-nav-bar"' in html or "owner-nav-bar" in html

    # Активная вкладка DroidHub: подсвечена и БЕЗ ссылки
    assert "DroidHub" in html
    assert 'class="owner-nav-tab active"' in html
    # DroidHub не должен быть кликабельной ссылкой
    assert '<a href="https://droidhub.molokohub.site/"' not in html
    assert '<a href="https://droidhub.molokohub.site/">DroidHub</a>' not in html
    assert '<span class="owner-nav-tab active" aria-current="page">DroidHub</span>' in html

    # Неактивная вкладка Gemini Pool: кликабельная ссылка в той же вкладке
    assert '<a href="https://pool.molokohub.site/" class="owner-nav-tab">Gemini Pool</a>' in html
    # Никаких target=_blank и rel=noopener
    assert 'target="_blank"' not in html
    assert 'rel="noopener"' not in html


def test_owner_nav_bar_case_insensitive_active(
    mock_manager: DeviceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Активная вкладка определяется без учёта регистра символов."""
    monkeypatch.setenv(
        "HUB_NAV_LINKS",
        "Gemini Pool|https://pool.molokohub.site/,DroidHub|https://droidhub.molokohub.site/",
    )

    app = create_web_app(mock_manager)
    client = TestClient(app)
    client.post("/test-login")

    # Нижний регистр: droidhub
    monkeypatch.setenv("HUB_NAV_ACTIVE", "droidhub")
    res_lower = client.get("/")
    assert '<span class="owner-nav-tab active" aria-current="page">DroidHub</span>' in res_lower.text
    assert '<a href="https://droidhub.molokohub.site/"' not in res_lower.text

    # Верхний регистр: DROIDHUB
    monkeypatch.setenv("HUB_NAV_ACTIVE", "DROIDHUB")
    res_upper = client.get("/")
    assert '<span class="owner-nav-tab active" aria-current="page">DroidHub</span>' in res_upper.text
    assert '<a href="https://droidhub.molokohub.site/"' not in res_upper.text

    # Переключение на gemini pool (без учета регистра)
    monkeypatch.setenv("HUB_NAV_ACTIVE", "gemini pool")
    res_pool = client.get("/")
    assert '<span class="owner-nav-tab active" aria-current="page">Gemini Pool</span>' in res_pool.text
    assert '<a href="https://pool.molokohub.site/"' not in res_pool.text
    assert '<a href="https://droidhub.molokohub.site/" class="owner-nav-tab">DroidHub</a>' in res_pool.text


def test_owner_nav_bar_on_login_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """На странице входа полоска тоже показывается."""
    monkeypatch.setenv(
        "HUB_NAV_LINKS",
        "Gemini Pool|https://pool.molokohub.site/,DroidHub|https://droidhub.molokohub.site/",
    )
    monkeypatch.setenv("HUB_NAV_ACTIVE", "DroidHub")

    app = create_web_app()
    client = TestClient(app)

    response = client.get("/login")
    assert response.status_code == 200
    html = response.text

    # Полоска на странице логина
    assert "owner-nav-bar" in html
    assert '<span class="owner-nav-tab active" aria-current="page">DroidHub</span>' in html
    assert '<a href="https://pool.molokohub.site/" class="owner-nav-tab">Gemini Pool</a>' in html


def test_owner_nav_bar_from_app_settings(mock_manager: DeviceManager) -> None:
    """Полоска корректно получает настройки через app.state.settings."""
    app = create_web_app(mock_manager)
    app.state.settings = Settings(
        nav_links="Pool|https://pool.example.com,Hub|https://hub.example.com",
        nav_active="Hub",
    )
    client = TestClient(app)
    client.post("/test-login")

    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    assert "owner-nav-bar" in html
    assert '<span class="owner-nav-tab active" aria-current="page">Hub</span>' in html
    assert '<a href="https://pool.example.com" class="owner-nav-tab">Pool</a>' in html


def test_owner_nav_bar_broken_and_javascript_links_skipped(
    mock_manager: DeviceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Битые ссылки и javascript: схемы отбрасываются при рендеринге страницы."""
    monkeypatch.setenv(
        "HUB_NAV_LINKS",
        "Safe|https://safe.example.com,Evil|javascript:alert(1),Broken|,|NoTitle",
    )
    monkeypatch.setenv("HUB_NAV_ACTIVE", "Safe")

    app = create_web_app(mock_manager)
    client = TestClient(app)
    client.post("/test-login")

    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    assert "owner-nav-bar" in html
    assert "Safe" in html
    assert "javascript" not in html
    assert "Evil" not in html
    assert "Broken" not in html


def test_owner_nav_bar_css_styling() -> None:
    """Проверка CSS-стилей для верхней полоски: высота ~56px, тёмно-синий фон, таблетки, скролл."""
    app = create_web_app()
    client = TestClient(app)

    res_css = client.get("/static/style.css")
    assert res_css.status_code == 200
    css = res_css.text

    assert ".owner-nav-bar" in css
    assert "56px" in css
    assert "#0b1329" in css
    assert ".owner-nav-container" in css
    assert "overflow-x: auto" in css
    assert ".owner-nav-tab" in css
    assert ".owner-nav-tab.active" in css
    assert "border-radius: 9999px" in css


# =====================================================================
# 11. Тесты этапа 5: единый вход (SSO), редиректы и аутентификация в вебе
# =====================================================================


def create_full_web_app(manager: DeviceManager | None = None) -> FastAPI:
    """Создает приложение с веб-роутером, auth_router и api_router для комплексных тестов."""
    from hub.api.routes import api_router
    from hub.auth import auth_router

    app = create_web_app(manager)
    app.include_router(auth_router)
    app.include_router(api_router)
    return app


def _setup_web_sso_mock(
    monkeypatch: pytest.MonkeyPatch,
    secret: str = "test_sso_secret",
    login_url: str = "https://molokohub.site/sso/login",
    cookie_domain: str = ".molokohub.site",
    cookie_name: str = "molokohub_sso",
) -> None:
    monkeypatch.setenv("HUB_SSO_SECRET", secret)
    monkeypatch.setenv("HUB_SSO_LOGIN_URL", login_url)
    monkeypatch.setenv("HUB_SSO_COOKIE_DOMAIN", cookie_domain)

    def mock_verify_cookie(value: str, sec: str, now: float | None = None) -> str | None:
        if sec != secret:
            return None
        if value == "valid_web_sso_cookie":
            return "sso_user_alice"
        return None

    monkeypatch.setattr(sso, "verify_cookie", mock_verify_cookie, raising=False)


def test_web_index_with_valid_sso_cookie(
    mock_manager: DeviceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET / с валидным SSO-cookie открывает страницу панели (200 OK, отображает имя)."""
    _setup_web_sso_mock(monkeypatch)
    app = create_web_app(mock_manager)
    client = TestClient(app)
    client.cookies.set("molokohub_sso", "valid_web_sso_cookie")

    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Подключенные устройства" in response.text
    assert "sso_user_alice" in response.text
    assert "Realme C21Y" in response.text


def test_web_device_with_valid_sso_cookie(
    mock_manager: DeviceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /device/{id} с валидным SSO-cookie открывает экран устройства (200 OK)."""
    _setup_web_sso_mock(monkeypatch)
    app = create_web_app(mock_manager)
    client = TestClient(app)
    client.cookies.set("molokohub_sso", "valid_web_sso_cookie")

    response = client.get("/device/realme")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Realme C21Y" in response.text
    assert "sso_user_alice" in response.text


def test_web_login_page_with_valid_sso_cookie_redirects_to_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Если пользователь уже авторизован по SSO, локальный /login редиректит 303 на /."""
    _setup_web_sso_mock(monkeypatch)
    app = create_web_app()
    client = TestClient(app)
    client.cookies.set("molokohub_sso", "valid_web_sso_cookie")

    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_web_forged_sso_cookie_rejected_and_redirects(
    mock_manager: DeviceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Поддельный SSO-cookie отвергается; при включенном SSO запрос редиректит на SSO-логин."""
    _setup_web_sso_mock(monkeypatch)
    app = create_web_app(mock_manager)
    client = TestClient(app)
    client.cookies.set("molokohub_sso", "forged_evil_cookie")

    # Главная страница
    res_index = client.get("/", follow_redirects=False)
    assert res_index.status_code == 303
    assert res_index.headers["location"] == "https://molokohub.site/sso/login?next=http%3A%2F%2Ftestserver%2F"

    # Страница устройства
    res_dev = client.get("/device/realme", follow_redirects=False)
    assert res_dev.status_code == 303
    assert res_dev.headers["location"] == "https://molokohub.site/sso/login?next=http%3A%2F%2Ftestserver%2Fdevice%2Frealme"


def test_web_redirect_with_correct_next_parameters(
    mock_manager: DeviceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    При непустом sso_secret страницы панели без сессии и SSO-cookie
    редиректят на sso_login_url?next=<urlencoded текущий полный URL>,
    а локальный GET /login тоже редиректит туда.
    """
    _setup_web_sso_mock(monkeypatch)
    app = create_web_app(mock_manager)
    client = TestClient(app)

    # 1. GET / -> next=http%3A%2F%2Ftestserver%2F
    res1 = client.get("/", follow_redirects=False)
    assert res1.status_code == 303
    assert res1.headers["location"] == "https://molokohub.site/sso/login?next=http%3A%2F%2Ftestserver%2F"

    # 2. GET /device/realme -> next=http%3A%2F%2Ftestserver%2Fdevice%2Frealme
    res2 = client.get("/device/realme", follow_redirects=False)
    assert res2.status_code == 303
    assert res2.headers["location"] == "https://molokohub.site/sso/login?next=http%3A%2F%2Ftestserver%2Fdevice%2Frealme"

    # 3. GET /device/realme?tab=apps&filter=system -> сохраняет и кодирует query string
    res3 = client.get("/device/realme?tab=apps&filter=system", follow_redirects=False)
    assert res3.status_code == 303
    assert res3.headers["location"] == (
        "https://molokohub.site/sso/login?next=http%3A%2F%2Ftestserver%2Fdevice%2Frealme%3Ftab%3Dapps%26filter%3Dsystem"
    )

    # 4. Локальный GET /login при включенном SSO тоже редиректит туда
    res4 = client.get("/login", follow_redirects=False)
    assert res4.status_code == 303
    assert res4.headers["location"] == "https://molokohub.site/sso/login?next=http%3A%2F%2Ftestserver%2Flogin"


def test_web_api_unauthenticated_returns_401_not_redirect(
    mock_manager: DeviceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    /api без аутентификации по-прежнему возвращает 401 (не редирект),
    даже при включенном SSO. Поддельный cookie даёт 401, валидный даёт 200.
    """
    _setup_web_sso_mock(monkeypatch)
    app = create_full_web_app(mock_manager)
    client = TestClient(app)

    # 1. Запрос без авторизации -> 401 (НЕ 302/303)
    res_no_auth = client.get("/api/devices")
    assert res_no_auth.status_code == 401
    assert res_no_auth.json()["detail"] == "Не авторизован"
    assert "location" not in res_no_auth.headers

    # 2. Запрос с поддельным cookie -> 401
    client.cookies.set("molokohub_sso", "bad_forged_cookie")
    res_bad = client.get("/api/devices")
    assert res_bad.status_code == 401
    assert "location" not in res_bad.headers

    # 3. Запрос с валидным SSO-cookie -> 200
    client.cookies.set("molokohub_sso", "valid_web_sso_cookie")
    res_ok = client.get("/api/devices")
    assert res_ok.status_code == 200
    devices_data = res_ok.json()
    assert isinstance(devices_data, list)
    assert len(devices_data) == 2


def test_web_disabled_sso_original_behavior(
    mock_manager: DeviceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Если sso_secret пуст, поведение полностью как сейчас (редиректы на /login, форма входа)."""
    _setup_web_sso_mock(monkeypatch, secret="")
    app = create_web_app(mock_manager)
    client = TestClient(app)

    # 1. GET / без сессии редиректит на /login
    res1 = client.get("/", follow_redirects=False)
    assert res1.status_code == 303
    assert res1.headers["location"] == "/login"

    # 2. GET /device/{id} без сессии редиректит на /login
    res2 = client.get("/device/realme", follow_redirects=False)
    assert res2.status_code == 303
    assert res2.headers["location"] == "/login"

    # 3. GET /login отдает форму 200 OK (не редирект)
    res3 = client.get("/login")
    assert res3.status_code == 200
    assert "Вход в DroidHub" in res3.text
    assert '<form action="/login"' in res3.text

    # 4. Наличие SSO-cookie при выключенном SSO не дает доступ
    client.cookies.set("molokohub_sso", "valid_web_sso_cookie")
    res4 = client.get("/", follow_redirects=False)
    assert res4.status_code == 303
    assert res4.headers["location"] == "/login"


def test_web_logout_deletes_sso_cookie_with_domain(
    mock_manager: DeviceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /logout панели дополнительно удаляет SSO-cookie (с domain=sso_cookie_domain)."""
    _setup_web_sso_mock(
        monkeypatch,
        secret="test_sso_secret",
        cookie_name="molokohub_sso",
        cookie_domain=".molokohub.site",
    )
    app = create_full_web_app(mock_manager)
    client = TestClient(app)

    # Авторизуемся через сессию
    client.post("/test-login")

    # Выполняем POST /logout
    res = client.post("/logout", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/login"

    set_cookies = res.headers.get_list("set-cookie") if hasattr(res.headers, "get_list") else [res.headers.get("set-cookie", "")]
    all_set_cookie = " ".join(set_cookies)

    assert "molokohub_sso" in all_set_cookie
    assert "Domain=.molokohub.site" in all_set_cookie
    assert "Max-Age=0" in all_set_cookie or "expires=" in all_set_cookie

