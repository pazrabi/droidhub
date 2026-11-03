import pytest

pytest.importorskip("mcp")

import asyncio
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from hub.config import DeviceConfig, Settings
from hub.devices import actions
from hub.devices.manager import DeviceManager, DeviceStatus
from hub.audit import get_audit, init_audit
import hub.mcp_server as mcp_mod
from hub.mcp_server import Client
from hub.main import create_app


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        db_path=":memory:",
        devices_file="",
        mcp_token="test-mcp-token-12345",
        rate_limit_per_min=60,
    )


@pytest.fixture
def mock_device_manager():
    devs = [
        DeviceConfig(id="realme", name="Realme C21Y", host="100.64.0.1", port=5555),
        DeviceConfig(id="redmi", name="Redmi Note 11", host="100.64.0.2", port=5555),
    ]
    mgr = DeviceManager(devs)
    actions.set_manager(mgr)
    return mgr


# --- 1. Проверка списка инструментов и отсутствия shell ---

def test_tool_list_and_no_shell() -> None:
    """Проверяет наличие всех 15 обязательных инструментов и отсутствие shell."""
    server = mcp_mod.mcp_server
    assert server is not None

    tool_objs = server._tool_manager.list_tools()
    tool_names = {t.name for t in tool_objs}

    expected_tools = {
        "list_devices",
        "device_status",
        "screenshot",
        "look",
        "tap_index",
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
    }
    assert expected_tools.issubset(tool_names), f"Не хватает инструментов: {expected_tools - tool_names}"
    assert "shell" not in tool_names, "Инструмент 'shell' НЕ должен быть зарегистрирован!"


def test_tools_accept_device_id_and_have_good_descriptions() -> None:
    """Каждый инструмент принимает device_id, описания содержат RU/EN строки, ui_dump/screenshot/look и пиксели."""
    server = mcp_mod.mcp_server
    assert server is not None

    for tool in server._tool_manager.list_tools():
        name = tool.name
        # Проверяем наличие параметра device_id
        properties = tool.parameters.get("properties", {})
        assert "device_id" in properties, f"Инструмент '{name}' должен принимать device_id"

        desc = tool.description or ""
        assert len(desc) > 20, f"У инструмента '{name}' слишком короткое описание: {desc!r}"

        # Проверяем наличие двух строк описания (RU и EN)
        lines = [line.strip() for line in desc.strip().splitlines() if line.strip()]
        assert len(lines) == 2, f"У инструмента '{name}' должно быть 2 строки описания (RU и EN): {desc!r}"

        # Для инструментов действий проверяем упоминание ui_dump/screenshot или пикселей
        if name in ("tap", "swipe", "screenshot", "look", "tap_index"):
            assert "пиксел" in desc.lower(), f"Описание '{name}' должно содержать указание на пиксели"
        if name in ("tap", "tap_element", "type_text"):
            assert ("ui_dump" in desc) or ("screenshot" in desc) or ("look" in desc), (
                f"Описание '{name}' должно рекомендовать ui_dump, look или screenshot"
            )


# --- 2. Проверка авторизации ---

def test_auth_disabled_when_token_empty() -> None:
    """При пустом mcp_token MCP возвращает 503 (отключён)."""
    settings = Settings(db_path=":memory:", devices_file="", mcp_token="")
    app = create_app(settings)

    with TestClient(app) as client:
        # Запрос без заголовков
        r1 = client.post("/mcp/")
        assert r1.status_code == 503
        assert "отключен" in r1.json().get("detail", "").lower()

        # Запрос с Bearer
        r2 = client.post("/mcp/", headers={"Authorization": "Bearer any-token"})
        assert r2.status_code == 503

        # Запрос на секретный путь
        r3 = client.post("/mcp/any-secret/")
        assert r3.status_code == 503


def test_auth_bearer_token(test_settings: Settings) -> None:
    """Проверка авторизации по заголовку Authorization: Bearer <mcp_token>."""
    app = create_app(test_settings)

    with TestClient(app) as client:
        # 1. Без заголовка -> 401
        r_no_auth = client.post("/mcp/")
        assert r_no_auth.status_code == 401
        assert "WWW-Authenticate" in r_no_auth.headers

        # 2. Неверный Bearer -> 401
        r_wrong = client.post("/mcp/", headers={"Authorization": "Bearer wrong-token"})
        assert r_wrong.status_code == 401

        # 3. Верный Bearer -> 200 (или корректный MCP-ответ при initialize)
        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            },
        }
        r_ok = client.post(
            "/mcp/",
            json=init_payload,
            headers={
                "Authorization": f"Bearer {test_settings.mcp_token}",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert r_ok.status_code == 200


def test_auth_secret_path(test_settings: Settings) -> None:
    """Проверка авторизации по секретному пути /mcp/<mcp_token>."""
    app = create_app(test_settings)

    with TestClient(app) as client:
        # 1. Неверный токен в пути -> 401
        r_wrong = client.post("/mcp/wrong-token-value/")
        assert r_wrong.status_code == 401

        # 2. Верный токен в пути -> 200
        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            },
        }
        r_ok = client.post(
            f"/mcp/{test_settings.mcp_token}/",
            json=init_payload,
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert r_ok.status_code == 200


def test_direct_mcp_app_routing(test_settings: Settings) -> None:
    """Прямое обращение к standalone mcp_app переписывает пути в корень (не 404)."""
    mcp_app, _ = mcp_mod.create_mcp_app(test_settings)
    init_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0"},
        },
    }

    with TestClient(mcp_app) as client:
        # 1. Прямой вызов с /mcp/<token>/
        r1 = client.post(
            f"/mcp/{test_settings.mcp_token}/",
            json=init_payload,
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert r1.status_code == 200

        # 2. Прямой вызов с /<token>/
        r2 = client.post(
            f"/{test_settings.mcp_token}/",
            json=init_payload,
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert r2.status_code == 200

        # 3. Прямой вызов с /mcp/ и Bearer заголовком
        r3 = client.post(
            "/mcp/",
            json=init_payload,
            headers={
                "Authorization": f"Bearer {test_settings.mcp_token}",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert r3.status_code == 200


def test_extract_path_token_logic() -> None:
    """_extract_path_token возвращает токен из пути и остаток пути без префикса /mcp."""
    extract = mcp_mod.MCPAuthMiddleware._extract_path_token
    token = "secret123"
    for path in (f"/mcp/{token}/", f"/mcp/{token}", f"/{token}/", f"/{token}"):
        assert extract(path) == (token, "/")
    assert extract(f"/mcp/{token}/sse") == (token, "/sse")
    # Нет токена в пути
    assert extract("/mcp/") == ("", "/mcp/")
    assert extract("/") == ("", "/")


@pytest.mark.asyncio
async def test_non_http_websocket_security() -> None:
    """Не-HTTP соединения (websocket) требуют авторизации, иначе закрываются с кодом 1008."""
    called_scopes: list[dict] = []

    async def dummy_app(scope, receive, send):
        called_scopes.append(dict(scope))
        await send({"type": "websocket.accept"})

    mw = mcp_mod.MCPAuthMiddleware(dummy_app, get_token=lambda: "ws-secret")

    # 1. Неавторизованный websocket закрывается с кодом 1008
    events: list[dict] = []
    scope_unauth = {"type": "websocket", "path": "/mcp/", "headers": []}

    async def send_1(msg):
        events.append(msg)

    await mw(scope_unauth, None, send_1)
    assert len(called_scopes) == 0
    assert events == [{"type": "websocket.close", "code": 1008}]

    # 2. Авторизованный через заголовок Bearer проходит
    events.clear()
    scope_bearer = {
        "type": "websocket",
        "path": "/mcp/",
        "headers": [(b"authorization", b"Bearer ws-secret")],
    }
    await mw(scope_bearer, None, send_1)
    assert len(called_scopes) == 1
    assert called_scopes[-1]["path"] == "/"

    # 3. Авторизованный через секретный путь проходит с переписанным путем
    events.clear()
    scope_path = {"type": "websocket", "path": "/mcp/ws-secret/", "headers": []}
    await mw(scope_path, None, send_1)
    assert len(called_scopes) == 2
    assert called_scopes[-1]["path"] == "/"

    # 4. При пустом токене (сервер выключен) websocket закрывается
    mw_off = mcp_mod.MCPAuthMiddleware(dummy_app, get_token=lambda: "")
    events.clear()
    await mw_off(scope_bearer, None, send_1)
    assert len(called_scopes) == 2  # не вызвался dummy_app
    assert events == [{"type": "websocket.close", "code": 1008}]

    # 5. Lifespan проходит без авторизации
    scope_lifespan = {"type": "lifespan"}
    await mw(scope_lifespan, None, send_1)
    assert len(called_scopes) == 3


# --- 3. Проверка вызова инструментов с замоканными actions ---

@pytest.mark.asyncio
async def test_call_tap_action(mock_device_manager) -> None:
    """Вызов tap вызывает actions.tap с actor='mcp'."""
    with patch("hub.devices.actions.tap", new_callable=AsyncMock) as mock_tap:
        res = await mcp_mod.tap("realme", 150, 300)
        assert isinstance(res, str)
        assert "150" in res and "300" in res
        mock_tap.assert_called_once_with("realme", 150, 300, actor="mcp")


@pytest.mark.asyncio
async def test_call_swipe_action(mock_device_manager) -> None:
    """Вызов swipe вызывает actions.swipe с actor='mcp'."""
    with patch("hub.devices.actions.swipe", new_callable=AsyncMock) as mock_swipe:
        res = await mcp_mod.swipe("realme", 100, 200, 100, 800, ms=400)
        assert isinstance(res, str)
        assert "выполнен" in res.lower()
        mock_swipe.assert_called_once_with("realme", 100, 200, 100, 800, ms=400, actor="mcp")


@pytest.mark.asyncio
async def test_call_screenshot_action(mock_device_manager) -> None:
    """Вызов screenshot возвращает объект картинки и вызывает actions.screenshot с actor='mcp' (max_width по умолчанию 540)."""
    fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIFfake"
    with patch("hub.devices.actions.screenshot", new_callable=AsyncMock, return_value=fake_jpeg) as mock_screen:
        img = await mcp_mod.screenshot("realme", max_width=640)
        mock_screen.assert_called_once_with("realme", max_width=640, actor="mcp")
        assert img is not None
        assert getattr(img, "data", None) == fake_jpeg

    # Проверка дефолтного max_width=540
    with patch("hub.devices.actions.screenshot", new_callable=AsyncMock, return_value=fake_jpeg) as mock_screen:
        await mcp_mod.screenshot("realme")
        mock_screen.assert_called_once_with("realme", max_width=540, actor="mcp")


@pytest.mark.asyncio
async def test_call_ui_dump_action(mock_device_manager) -> None:
    """Вызов ui_dump вызывает actions.ui_dump с actor='mcp'."""
    dump_data = [{"text": "Кнопка", "center": [100, 200], "clickable": True}]
    with patch("hub.devices.actions.ui_dump", new_callable=AsyncMock, return_value=dump_data) as mock_dump:
        res = await mcp_mod.ui_dump("realme")
        assert res == dump_data
        mock_dump.assert_called_once_with("realme", actor="mcp")


@pytest.mark.asyncio
async def test_call_tap_element_action(mock_device_manager) -> None:
    """Вызов tap_element вызывает actions.tap_element с actor='mcp'."""
    elem = {"text": "Войти", "center": [250, 400]}
    with patch("hub.devices.actions.tap_element", new_callable=AsyncMock, return_value=elem) as mock_te:
        res = await mcp_mod.tap_element("realme", text="Войти")
        assert isinstance(res, str)
        assert "Войти" in res
        mock_te.assert_called_once_with("realme", text="Войти", resource_id=None, actor="mcp")


@pytest.mark.asyncio
async def test_call_type_text_action(mock_device_manager) -> None:
    """Вызов type_text вызывает actions.type_text с actor='mcp'."""
    with patch("hub.devices.actions.type_text", new_callable=AsyncMock) as mock_type:
        res = await mcp_mod.type_text("realme", "Привет мир")
        assert isinstance(res, str)
        assert "Привет мир" in res
        mock_type.assert_called_once_with("realme", "Привет мир", actor="mcp")


@pytest.mark.asyncio
async def test_call_press_key_action(mock_device_manager) -> None:
    """Вызов press_key вызывает actions.key с actor='mcp'."""
    with patch("hub.devices.actions.key", new_callable=AsyncMock) as mock_key:
        res = await mcp_mod.press_key("realme", "home")
        assert isinstance(res, str)
        assert "home" in res
        mock_key.assert_called_once_with("realme", "home", actor="mcp")


@pytest.mark.asyncio
async def test_call_open_app_action(mock_device_manager) -> None:
    """Вызов open_app вызывает actions.open_app с actor='mcp'."""
    with patch("hub.devices.actions.open_app", new_callable=AsyncMock) as mock_app:
        res = await mcp_mod.open_app("realme", "com.android.chrome")
        assert isinstance(res, str)
        assert "com.android.chrome" in res
        mock_app.assert_called_once_with("realme", "com.android.chrome", actor="mcp")


@pytest.mark.asyncio
async def test_call_list_apps_action(mock_device_manager) -> None:
    """Вызов list_apps вызывает actions.list_apps с actor='mcp'."""
    packages = ["com.android.chrome", "com.android.settings"]
    with patch("hub.devices.actions.list_apps", new_callable=AsyncMock, return_value=packages) as mock_apps:
        res = await mcp_mod.list_apps("realme")
        assert res == packages
        mock_apps.assert_called_once_with("realme", actor="mcp")


@pytest.mark.asyncio
async def test_call_open_url_action(mock_device_manager) -> None:
    """Вызов open_url вызывает actions.open_url с actor='mcp'."""
    with patch("hub.devices.actions.open_url", new_callable=AsyncMock) as mock_url:
        res = await mcp_mod.open_url("realme", "https://ya.ru")
        assert isinstance(res, str)
        assert "https://ya.ru" in res
        mock_url.assert_called_once_with("realme", "https://ya.ru", actor="mcp")


@pytest.mark.asyncio
async def test_call_wait_action(mock_device_manager) -> None:
    """Вызов wait вызывает actions.wait с actor='mcp'."""
    with patch("hub.devices.actions.wait", new_callable=AsyncMock) as mock_wait:
        res = await mcp_mod.wait("realme", 0.5)
        assert res.get("ok") is True
        mock_wait.assert_called_once_with("realme", 0.5, actor="mcp")


@pytest.mark.asyncio
async def test_call_list_devices(mock_device_manager) -> None:
    """Вызов list_devices возвращает список устройств со статусами."""
    res = await mcp_mod.list_devices()
    assert isinstance(res, list)
    dev_ids = {d["id"] for d in res}
    assert "realme" in dev_ids
    assert "redmi" in dev_ids


@pytest.mark.asyncio
async def test_call_device_status(mock_device_manager) -> None:
    """Вызов device_status опрашивает менеджер и возвращает статус устройства."""
    status_obj = DeviceStatus(id="realme", name="Realme C21Y", online=True, battery=85, temperature=28.5)
    with patch.object(mock_device_manager, "refresh", new_callable=AsyncMock, return_value=status_obj):
        res = await mcp_mod.device_status("realme")
        assert res["id"] == "realme"
        assert res["online"] is True
        assert res["battery"] == 85
        assert res["temperature"] == 28.5


@pytest.mark.asyncio
async def test_rate_limit_exceeded(mock_device_manager) -> None:
    """При превышении лимита действий инструмент вызывает ошибку."""
    limiter = mcp_mod.limiter
    if limiter is not None:
        limiter.reset("realme")
        # Имитируем исчерпание лимита через проверку
        with patch.object(limiter, "check", new_callable=AsyncMock, return_value=False):
            with pytest.raises(RuntimeError, match="Превышен лимит"):
                await mcp_mod.tap("realme", 100, 100)


@pytest.mark.asyncio
async def test_server_call_tool_interface(mock_device_manager) -> None:
    """Вызов через официальный интерфейс mcp_server.call_tool."""
    server = mcp_mod.mcp_server
    assert server is not None

    with patch("hub.devices.actions.tap", new_callable=AsyncMock) as mock_tap:
        call_res = await server.call_tool("tap", {"device_id": "realme", "x": 50, "y": 75})
        mock_tap.assert_called_once_with("realme", 50, 75, actor="mcp")
        assert not call_res.is_error


# --- 4. Проверка look и tap_index ---

@pytest.mark.asyncio
async def test_look_formatting_filtering(mock_device_manager) -> None:
    """Фильтрация: исключаются пустые неинтерактивные элементы, остаются только значимые."""
    raw_data = [
        {"class": "android.view.View", "bounds": [0, 0, 100, 100], "center": [50, 50]},  # не значимый
        {"class": "android.widget.TextView", "text": "Заголовок", "center": [50, 100]},  # значимый (есть text)
        {"class": "android.widget.ImageView", "content_desc": "Иконка", "center": [50, 150]},  # значимый (есть content_desc)
        {"class": "android.view.View", "clickable": True, "center": [50, 200]},  # значимый (clickable)
        {"class": "android.widget.ScrollView", "scrollable": True, "center": [50, 250]},  # значимый (scrollable)
        {"class": "android.widget.CheckBox", "checkable": True, "center": [50, 300]},  # значимый (checkable)
        {"class": "android.widget.EditText", "editable": True, "center": [50, 350]},  # значимый (editable)
    ]
    with patch("hub.devices.actions.ui_dump", new_callable=AsyncMock, return_value=raw_data):
        res = await mcp_mod.look("realme")
        assert "Заголовок" in res
        assert "Иконка" in res
        assert "(кликабельно)" in res
        assert "(прокрутка)" in res
        assert "(выбор)" in res
        assert "(ввод)" in res
        lines = [line for line in res.splitlines() if line.startswith("[")]
        assert len(lines) == 6


@pytest.mark.asyncio
async def test_look_formatting_numbering_and_order(mock_device_manager) -> None:
    """Сортировка сверху вниз (по Y) и сквозная нумерация [1], [2], [3]... плюс заголовок с пакетом/активити."""
    raw_data = [
        {"package": "com.android.settings", "activity": ".SettingsActivity", "class": "Button", "text": "Низ", "center": [100, 500], "clickable": True},
        {"class": "TextView", "text": "Верх", "center": [100, 50]},
        {"class": "Button", "text": "Середина", "center": [100, 250], "clickable": True},
    ]
    with patch("hub.devices.actions.ui_dump", new_callable=AsyncMock, return_value=raw_data):
        res = await mcp_mod.look("realme")
        lines = res.splitlines()
        # Заголовок
        assert lines[0] == "Экран: com.android.settings / .SettingsActivity"
        # Порядок: Верх (Y=50), Середина (Y=250), Низ (Y=500)
        item_lines = [l for l in lines if l.startswith("[")]
        assert len(item_lines) == 3
        assert item_lines[0].startswith("[1] Текст «Верх»")
        assert "центр=(100,50)" in item_lines[0]
        assert item_lines[1].startswith("[2] Кнопка «Середина» (кликабельно)")
        assert "центр=(100,250)" in item_lines[1]
        assert item_lines[2].startswith("[3] Кнопка «Низ» (кликабельно)")
        assert "центр=(100,500)" in item_lines[2]


@pytest.mark.asyncio
async def test_look_formatting_truncation(mock_device_manager) -> None:
    """Длинный текст обрезается до 80 символов."""
    long_text = "А" * 120
    raw_data = [{"class": "TextView", "text": long_text, "center": [100, 100]}]
    with patch("hub.devices.actions.ui_dump", new_callable=AsyncMock, return_value=raw_data):
        res = await mcp_mod.look("realme")
        expected_part = "«" + ("А" * 80) + "»"
        assert expected_part in res
        assert ("А" * 81) not in res


@pytest.mark.asyncio
async def test_look_formatting_limit(mock_device_manager) -> None:
    """Лимит не больше 120 элементов с пометкой (показано 120 из Y)."""
    raw_data = [
        {"class": "Button", "text": f"Кнопка {i}", "clickable": True, "center": [10, i * 10]}
        for i in range(1, 141)  # 140 элементов
    ]
    with patch("hub.devices.actions.ui_dump", new_callable=AsyncMock, return_value=raw_data):
        res = await mcp_mod.look("realme")
        item_lines = [l for l in res.splitlines() if l.startswith("[")]
        assert len(item_lines) == 120
        assert "[120]" in item_lines[-1]
        assert "[121]" not in res
        assert "(показано 120 из 140)" in res


@pytest.mark.asyncio
async def test_look_deduplication(mock_device_manager) -> None:
    """Удаление дубликатов элементов."""
    raw_data = [
        {"class": "Button", "text": "Войти", "clickable": True, "center": [100, 200]},
        {"class": "Button", "text": "Войти", "clickable": True, "center": [100, 200]},  # полный дубликат
        {"class": "FrameLayout", "text": "", "clickable": True, "center": [100, 200]},  # пустой контейнер на том же центре
    ]
    with patch("hub.devices.actions.ui_dump", new_callable=AsyncMock, return_value=raw_data):
        res = await mcp_mod.look("realme")
        item_lines = [l for l in res.splitlines() if l.startswith("[")]
        assert len(item_lines) == 1
        assert item_lines[0].startswith("[1] Кнопка «Войти»")


@pytest.mark.asyncio
async def test_tap_index_success(mock_device_manager) -> None:
    """Успешный tap_index по индексу из последнего look."""
    raw_data = [
        {"class": "Button", "text": "Первая", "clickable": True, "center": [120, 240]},
        {"class": "Button", "text": "Вторая", "clickable": True, "center": [300, 600]},
    ]
    with patch("hub.devices.actions.ui_dump", new_callable=AsyncMock, return_value=raw_data):
        await mcp_mod.look("realme")

    with patch("hub.devices.actions.tap", new_callable=AsyncMock) as mock_tap:
        res = await mcp_mod.tap_index("realme", 2)
        assert isinstance(res, str)
        assert "[2]" in res
        assert "300" in res and "600" in res
        mock_tap.assert_called_once_with("realme", 300, 600, actor="mcp")


@pytest.mark.asyncio
async def test_tap_index_no_look(mock_device_manager) -> None:
    """tap_index без предварительного вызова look возвращает понятную ошибку."""
    mcp_mod._look_cache.clear()
    with pytest.raises(ValueError, match="(?i)сначала вызови look"):
        await mcp_mod.tap_index("realme", 1)


@pytest.mark.asyncio
async def test_tap_index_expired(mock_device_manager) -> None:
    """tap_index с устаревшим (>120 с) look возвращает ошибку 'сначала вызови look'."""
    raw_data = [{"class": "Button", "text": "OK", "clickable": True, "center": [100, 100]}]
    with patch("hub.devices.actions.ui_dump", new_callable=AsyncMock, return_value=raw_data):
        await mcp_mod.look("realme")

    # Искусственно состариваем запись в кеше на 130 секунд
    mcp_mod._look_cache["realme"].timestamp -= 130.0

    with pytest.raises(ValueError, match="(?i)сначала вызови look"):
        await mcp_mod.tap_index("realme", 1)


@pytest.mark.asyncio
async def test_tap_index_invalid_index(mock_device_manager) -> None:
    """tap_index с несуществующим индексом возвращает ошибку 'сначала вызови look'."""
    raw_data = [{"class": "Button", "text": "OK", "clickable": True, "center": [100, 100]}]
    with patch("hub.devices.actions.ui_dump", new_callable=AsyncMock, return_value=raw_data):
        await mcp_mod.look("realme")

    with pytest.raises(ValueError, match="(?i)сначала вызови look"):
        await mcp_mod.tap_index("realme", 999)


# --- 5. Проверка авторизации по токенам клиентов (Этап 4) ---

def test_auth_unknown_token_401(test_settings: Settings, monkeypatch) -> None:
    """Неизвестный токен возвращает 401 как по Bearer, так и по пути /mcp/<token>."""
    def fake_resolve(token: str, settings: Settings | None = None) -> Client | None:
        if token == "known-token":
            return Client(name="test_client")
        return None

    monkeypatch.setattr("hub.mcp_server.auth.resolve_token", fake_resolve)
    app = create_app(test_settings)

    with TestClient(app) as client:
        # 1. Неизвестный Bearer -> 401
        r_bearer = client.post("/mcp/", headers={"Authorization": "Bearer unknown-token-xyz"})
        assert r_bearer.status_code == 401
        assert "WWW-Authenticate" in r_bearer.headers

        # 2. Неизвестный токен в пути -> 401
        r_path = client.post("/mcp/unknown-token-xyz/")
        assert r_path.status_code == 401

        # 3. Неизвестный токен без слэша в пути -> 401
        r_path_noslash = client.post("/mcp/unknown-token-xyz")
        assert r_path_noslash.status_code == 401


def test_auth_empty_token_401(test_settings: Settings, monkeypatch) -> None:
    """Пустой токен (в заголовке или в пути) возвращает 401, когда сервер включен."""
    def fake_resolve(token: str, settings: Settings | None = None) -> Client | None:
        if token == "valid-token":
            return Client(name="valid_client")
        return None

    monkeypatch.setattr("hub.mcp_server.auth.resolve_token", fake_resolve)
    app = create_app(test_settings)

    with TestClient(app) as client:
        # 1. Пустой Bearer -> 401
        r1 = client.post("/mcp/", headers={"Authorization": "Bearer "})
        assert r1.status_code == 401

        # 2. Bearer только с пробелами -> 401
        r2 = client.post("/mcp/", headers={"Authorization": "Bearer    "})
        assert r2.status_code == 401

        # 3. Путь /mcp/ без токена и заголовка -> 401
        r3 = client.post("/mcp/")
        assert r3.status_code == 401

        # 4. Путь /mcp/   / с пробелами -> 401
        r4 = client.post("/mcp/   /")
        assert r4.status_code == 401


def test_two_clients_with_different_actors_and_limits(
    test_settings: Settings,
    mock_device_manager,
    monkeypatch,
) -> None:
    """Два клиента с разными actor и лимитами: авторизация по Bearer и пути, actor передается в actions, проверка разных лимитов."""
    mcp_mod.limiter.reset()
    clients_db = {
        "token-pool": Client(name="pool", rate_limit_per_min=5, allow_power=True),
        "token-claude": Client(name="claude", rate_limit_per_min=2, allow_power=False),
    }

    def fake_resolve(token: str, settings: Settings | None = None) -> Client | None:
        return clients_db.get(token)

    monkeypatch.setattr("hub.mcp_server.auth.resolve_token", fake_resolve)
    app = create_app(test_settings)

    with patch("hub.devices.actions.tap", new_callable=AsyncMock) as mock_tap:
        with TestClient(app) as client:
            # Клиент 1: pool через Bearer токен (лимит 5)
            r1 = client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "c1", "version": "1.0"},
                    },
                },
                headers={
                    "Authorization": "Bearer token-pool",
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert r1.status_code == 200
            sess_pool = r1.headers.get("mcp-session-id")

            # Вызов 1 pool -> успешно (1 из 5)
            r2 = client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "tap",
                        "arguments": {"device_id": "realme", "x": 100, "y": 200},
                    },
                },
                headers={
                    "Authorization": "Bearer token-pool",
                    "mcp-session-id": sess_pool,
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert r2.status_code == 200
            assert '"isError":false' in r2.text
            mock_tap.assert_called_once_with("realme", 100, 200, actor="pool")

            # Клиент 2: claude через путь /mcp/<token> (лимит 2)
            mock_tap.reset_mock()
            r3 = client.post(
                "/mcp/token-claude/",
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "c2", "version": "1.0"},
                    },
                },
                headers={"Accept": "application/json, text/event-stream"},
            )
            assert r3.status_code == 200
            sess_claude = r3.headers.get("mcp-session-id")

            # Вызов 1 claude -> успешно (1 из 2)
            r4 = client.post(
                "/mcp/token-claude/",
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "tap",
                        "arguments": {"device_id": "realme", "x": 50, "y": 70},
                    },
                },
                headers={
                    "mcp-session-id": sess_claude,
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert r4.status_code == 200
            assert '"isError":false' in r4.text
            mock_tap.assert_called_once_with("realme", 50, 70, actor="claude")

            # Вызов 2 claude -> успешно (2 из 2)
            mock_tap.reset_mock()
            r5 = client.post(
                "/mcp/token-claude/",
                json={
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "tap",
                        "arguments": {"device_id": "realme", "x": 51, "y": 71},
                    },
                },
                headers={
                    "mcp-session-id": sess_claude,
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert r5.status_code == 200
            assert '"isError":false' in r5.text
            mock_tap.assert_called_once_with("realme", 51, 71, actor="claude")

            # Вызов 3 claude -> ошибка превышения лимита (порог 2 исчерпан)
            mock_tap.reset_mock()
            r6 = client.post(
                "/mcp/token-claude/",
                json={
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {
                        "name": "tap",
                        "arguments": {"device_id": "realme", "x": 52, "y": 72},
                    },
                },
                headers={
                    "mcp-session-id": sess_claude,
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert r6.status_code == 200
            assert '"isError":true' in r6.text
            mock_tap.assert_not_called()

            # Клиент pool (лимит 5) продолжает успешно выполнять действия (вызовы 2, 3, 4, 5)
            for req_id, x in enumerate([101, 102, 103, 104], start=7):
                mock_tap.reset_mock()
                r_pool = client.post(
                    "/mcp/",
                    json={
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "method": "tools/call",
                        "params": {
                            "name": "tap",
                            "arguments": {"device_id": "realme", "x": x, "y": 200},
                        },
                    },
                    headers={
                        "Authorization": "Bearer token-pool",
                        "mcp-session-id": sess_pool,
                        "Accept": "application/json, text/event-stream",
                    },
                )
                assert r_pool.status_code == 200
                assert '"isError":false' in r_pool.text
                mock_tap.assert_called_once_with("realme", x, 200, actor="pool")

            # Вызов 6 pool -> превышение лимита pool (порог 5 исчерпан)
            mock_tap.reset_mock()
            r_pool_blocked = client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 20,
                    "method": "tools/call",
                    "params": {
                        "name": "tap",
                        "arguments": {"device_id": "realme", "x": 999, "y": 999},
                    },
                },
                headers={
                    "Authorization": "Bearer token-pool",
                    "mcp-session-id": sess_pool,
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert r_pool_blocked.status_code == 200
            assert '"isError":true' in r_pool_blocked.text
            mock_tap.assert_not_called()


@pytest.mark.asyncio
async def test_actor_in_audit_records(
    test_settings: Settings,
    mock_device_manager,
    monkeypatch,
) -> None:
    """Actor во всех вызовах actions и audit равен client.name (а не 'mcp')."""
    init_audit(":memory:")
    c_pool = Client(name="pool", rate_limit_per_min=10)
    c_claude = Client(name="claude", rate_limit_per_min=10)

    # 1. Вызовы под клиентом pool
    with mcp_mod.client_context(c_pool):
        # list_devices логирует audit
        await mcp_mod.list_devices()

        # look логирует audit
        dump_data = [{"class": "TextView", "text": "Кнопка", "center": [10, 20]}]
        with patch("hub.devices.actions.ui_dump", new_callable=AsyncMock, return_value=dump_data):
            await mcp_mod.look("realme")

        # tap_index логирует audit и вызывает actions.tap с actor='pool'
        with patch("hub.devices.actions.tap", new_callable=AsyncMock) as mock_tap:
            await mcp_mod.tap_index("realme", 1)
            mock_tap.assert_called_once_with("realme", 10, 20, actor="pool")

    # 2. Вызовы под клиентом claude
    with mcp_mod.client_context(c_claude):
        await mcp_mod.list_devices()

        with patch("hub.devices.actions.swipe", new_callable=AsyncMock) as mock_swipe:
            await mcp_mod.swipe("realme", 1, 2, 3, 4)
            mock_swipe.assert_called_once_with("realme", 1, 2, 3, 4, ms=300, actor="claude")

    aud = get_audit()
    assert aud is not None
    entries = await aud.recent(limit=20)
    actors = [e["actor"] for e in entries]

    # Проверяем, что в записях есть 'pool' и 'claude', и нет константы 'mcp'
    assert "pool" in actors
    assert "claude" in actors
    assert "mcp" not in actors


@pytest.mark.asyncio
async def test_rate_limits_independence(
    test_settings: Settings,
    mock_device_manager,
    monkeypatch,
) -> None:
    """Лимит действий считается на пару (клиент, устройство); один клиент не должен расходовать лимит другого."""
    mcp_mod.limiter.reset()

    c_pool = Client(name="pool", rate_limit_per_min=2)
    c_claude = Client(name="claude", rate_limit_per_min=2)

    # Pool выполняет 2 действия на realme (лимит 2)
    with mcp_mod.client_context(c_pool):
        with patch("hub.devices.actions.tap", new_callable=AsyncMock):
            await mcp_mod.tap("realme", 1, 1)
            await mcp_mod.tap("realme", 2, 2)
            # 3-е действие pool на realme превышает лимит
            with pytest.raises(RuntimeError, match="Превышен лимит"):
                await mcp_mod.tap("realme", 3, 3)

    # Claude на realme НЕ должен пострадать от исчерпания лимита клиентом pool!
    with mcp_mod.client_context(c_claude):
        with patch("hub.devices.actions.tap", new_callable=AsyncMock):
            await mcp_mod.tap("realme", 10, 10)
            await mcp_mod.tap("realme", 20, 20)
            # 3-е действие claude на realme превышает его собственный лимит
            with pytest.raises(RuntimeError, match="Превышен лимит"):
                await mcp_mod.tap("realme", 30, 30)

    # Pool на другом устройстве (redmi) не исчерпан: лимит на пару (клиент, устройство)
    with mcp_mod.client_context(c_pool):
        with patch("hub.devices.actions.tap", new_callable=AsyncMock):
            await mcp_mod.tap("redmi", 1, 1)


@pytest.mark.asyncio
async def test_fallback_rate_limit_from_settings(
    test_settings: Settings,
    mock_device_manager,
    monkeypatch,
) -> None:
    """Если client.rate_limit_per_min не задан (None), используется settings.rate_limit_per_min."""
    mcp_mod.limiter.reset()

    c_no_limit = Client(name="client_no_limit", rate_limit_per_min=None)

    with patch("hub.mcp_server.ratelimit.get_settings") as mock_get_settings:
        mock_get_settings.return_value = Settings(rate_limit_per_min=2)
        with mcp_mod.client_context(c_no_limit):
            with patch("hub.devices.actions.tap", new_callable=AsyncMock):
                await mcp_mod.tap("realme", 1, 1)
                await mcp_mod.tap("realme", 2, 2)
                with pytest.raises(RuntimeError, match="Превышен лимит"):
                    await mcp_mod.tap("realme", 3, 3)


def test_token_not_logged_anywhere(
    test_settings: Settings,
    caplog,
    monkeypatch,
) -> None:
    """Токены авторизации не логируются в логах сервера."""
    secret_token = "super-secret-token-value-xyz987"
    app = create_app(test_settings)

    with caplog.at_level("DEBUG"):
        with TestClient(app) as client:
            client.post("/mcp/", headers={"Authorization": f"Bearer {secret_token}"})

    # Сервер не должен логировать токен ни в одном лог-сообщении
    server_records = [r for r in caplog.records if not r.name.startswith("httpx")]
    for record in server_records:
        assert secret_token not in record.message
        assert secret_token not in str(record.args)
