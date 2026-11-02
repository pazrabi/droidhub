import pytest

pytest.importorskip("mcp")

import asyncio
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from hub.config import DeviceConfig, Settings
from hub.devices import actions
from hub.devices.manager import DeviceManager, DeviceStatus
import hub.mcp_server as mcp_mod
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
    """Проверяет наличие всех 13 обязательных инструментов и отсутствие shell."""
    server = mcp_mod.mcp_server
    assert server is not None

    tool_objs = server._tool_manager.list_tools()
    tool_names = {t.name for t in tool_objs}

    expected_tools = {
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
    }
    assert expected_tools.issubset(tool_names), f"Не хватает инструментов: {expected_tools - tool_names}"
    assert "shell" not in tool_names, "Инструмент 'shell' НЕ должен быть зарегистрирован!"


def test_tools_accept_device_id_and_have_good_descriptions() -> None:
    """Каждый инструмент принимает device_id, описания содержат ui_dump/screenshot и пиксели."""
    server = mcp_mod.mcp_server
    assert server is not None

    for tool in server._tool_manager.list_tools():
        name = tool.name
        # Проверяем наличие параметра device_id
        properties = tool.parameters.get("properties", {})
        assert "device_id" in properties, f"Инструмент '{name}' должен принимать device_id"

        desc = tool.description or ""
        assert len(desc) > 20, f"У инструмента '{name}' слишком короткое описание: {desc!r}"

        # Для инструментов действий проверяем упоминание ui_dump/screenshot или пикселей
        if name in ("tap", "swipe", "screenshot"):
            assert "пиксел" in desc.lower(), f"Описание '{name}' должно содержать указание на пиксели"
        if name in ("tap", "tap_element", "type_text"):
            assert ("ui_dump" in desc) or ("screenshot" in desc), (
                f"Описание '{name}' должно рекомендовать ui_dump или screenshot"
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


# --- 3. Проверка вызова инструментов с замоканными actions ---

@pytest.mark.asyncio
async def test_call_tap_action(mock_device_manager) -> None:
    """Вызов tap вызывает actions.tap с actor='mcp'."""
    with patch("hub.devices.actions.tap", new_callable=AsyncMock) as mock_tap:
        res = await mcp_mod.tap("realme", 150, 300)
        assert res.get("ok") is True
        mock_tap.assert_called_once_with("realme", 150, 300, actor="mcp")


@pytest.mark.asyncio
async def test_call_swipe_action(mock_device_manager) -> None:
    """Вызов swipe вызывает actions.swipe с actor='mcp'."""
    with patch("hub.devices.actions.swipe", new_callable=AsyncMock) as mock_swipe:
        res = await mcp_mod.swipe("realme", 100, 200, 100, 800, ms=400)
        assert res.get("ok") is True
        mock_swipe.assert_called_once_with("realme", 100, 200, 100, 800, ms=400, actor="mcp")


@pytest.mark.asyncio
async def test_call_screenshot_action(mock_device_manager) -> None:
    """Вызов screenshot возвращает объект картинки и вызывает actions.screenshot с actor='mcp'."""
    fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIFfake"
    with patch("hub.devices.actions.screenshot", new_callable=AsyncMock, return_value=fake_jpeg) as mock_screen:
        img = await mcp_mod.screenshot("realme", max_width=640)
        mock_screen.assert_called_once_with("realme", max_width=640, actor="mcp")
        assert img is not None
        assert getattr(img, "data", None) == fake_jpeg


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
        assert res == elem
        mock_te.assert_called_once_with("realme", text="Войти", resource_id=None, actor="mcp")


@pytest.mark.asyncio
async def test_call_type_text_action(mock_device_manager) -> None:
    """Вызов type_text вызывает actions.type_text с actor='mcp'."""
    with patch("hub.devices.actions.type_text", new_callable=AsyncMock) as mock_type:
        res = await mcp_mod.type_text("realme", "Привет мир")
        assert res.get("ok") is True
        mock_type.assert_called_once_with("realme", "Привет мир", actor="mcp")


@pytest.mark.asyncio
async def test_call_press_key_action(mock_device_manager) -> None:
    """Вызов press_key вызывает actions.key с actor='mcp'."""
    with patch("hub.devices.actions.key", new_callable=AsyncMock) as mock_key:
        res = await mcp_mod.press_key("realme", "home")
        assert res.get("ok") is True
        mock_key.assert_called_once_with("realme", "home", actor="mcp")


@pytest.mark.asyncio
async def test_call_open_app_action(mock_device_manager) -> None:
    """Вызов open_app вызывает actions.open_app с actor='mcp'."""
    with patch("hub.devices.actions.open_app", new_callable=AsyncMock) as mock_app:
        res = await mcp_mod.open_app("realme", "com.android.chrome")
        assert res.get("ok") is True
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
        assert res.get("ok") is True
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
