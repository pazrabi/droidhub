from __future__ import annotations

import sys
import types
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from PIL import Image
import pytest

TEST_ACTOR = "test-user"

from hub.api.routes import (
    api_router,
    clear_screen_size_cache,
    parse_wm_size,
    public_router,
    require_api,
    reset_rate_limiter,
)
from hub.config import DeviceConfig, Settings
from hub.devices import actions
from hub.devices.adb import AdbError, AdbTimeout, DeviceOffline
from hub.devices.manager import DeviceManager, DeviceStatus


def _create_test_jpeg(width: int = 100, height: int = 200) -> bytes:
    img = Image.new("RGB", (width, height), color="blue")
    buf = BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
def mock_manager() -> DeviceManager:
    devs = [
        DeviceConfig(id="realme", name="Realme C21Y", host="100.64.0.1", port=5555),
        DeviceConfig(id="redmi", name="Redmi Note 11", host="100.64.0.2", port=5555),
    ]
    mgr = DeviceManager(devs)
    actions.set_manager(mgr)
    return mgr


@pytest.fixture
def app(mock_manager: DeviceManager) -> FastAPI:
    test_app = FastAPI()

    async def _fake_require_api() -> str:
        return "test-user"

    test_app.dependency_overrides[require_api] = _fake_require_api
    test_app.include_router(public_router)
    test_app.include_router(api_router)
    return test_app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    clear_screen_size_cache()
    reset_rate_limiter()
    return TestClient(app)


def test_public_health(client: TestClient) -> None:
    """GET /api/health отдаёт 200 ok без авторизации."""
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_require_api_unauthorized(app: FastAPI, client: TestClient) -> None:
    """Проверка отказа в доступе при сбое авторизации (401)."""
    async def mock_unauthorized() -> str:
        raise HTTPException(status_code=401, detail="Не авторизован")

    app.dependency_overrides[require_api] = mock_unauthorized
    try:
        res = client.get("/api/devices")
        assert res.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_get_devices(client: TestClient, mock_manager: DeviceManager) -> None:
    """GET /api/devices возвращает список устройств."""
    res = client.get("/api/devices")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    assert len(data) == 2
    ids = [d["id"] for d in data]
    assert "realme" in ids
    assert "redmi" in ids


@pytest.mark.asyncio
async def test_parse_wm_size() -> None:
    """Тестирование парсинга вывода команды wm size."""
    assert parse_wm_size("Physical size: 1080x2400\n") == (1080, 2400)
    assert parse_wm_size("Physical size: 1080x2400\nOverride size: 720x1600\n") == (720, 1600)
    assert parse_wm_size("") is None
    assert parse_wm_size("unknown output") is None


def test_screenshot_headers_and_caching(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    GET /api/devices/{id}/screenshot отдаёт JPEG,
    заголовки X-Screen-Width / X-Screen-Height через wm size, и кеширует размер.
    """
    test_jpeg = _create_test_jpeg(360, 800)
    mock_screenshot = AsyncMock(return_value=test_jpeg)
    monkeypatch.setattr(actions, "screenshot", mock_screenshot)

    wm_size_call_count = 0

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        nonlocal wm_size_call_count
        if "wm size" in cmd:
            wm_size_call_count += 1
            return "Physical size: 1080x2400\n"
        return ""

    monkeypatch.setattr("hub.api.routes.adb_shell", mock_adb_shell)

    # 1. Первый запрос: должен вызвать wm size
    res1 = client.get("/api/devices/realme/screenshot?max_width=480")
    assert res1.status_code == 200
    assert res1.headers["content-type"] == "image/jpeg"
    assert res1.headers["X-Screen-Width"] == "1080"
    assert res1.headers["X-Screen-Height"] == "2400"
    assert res1.content == test_jpeg
    assert wm_size_call_count == 1
    mock_screenshot.assert_called_with("realme", max_width=480, actor=TEST_ACTOR)

    # 2. Второй запрос: wm size НЕ должен вызываться снова, так как размер закеширован!
    res2 = client.get("/api/devices/realme/screenshot")
    assert res2.status_code == 200
    assert res2.headers["X-Screen-Width"] == "1080"
    assert res2.headers["X-Screen-Height"] == "2400"
    assert wm_size_call_count == 1  # Всё ещё 1! Кеширование сработало


def test_tap_action(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/devices/{id}/tap вызывает actions.tap с actor."""
    mock_tap = AsyncMock(return_value=None)
    monkeypatch.setattr(actions, "tap", mock_tap)

    res = client.post("/api/devices/realme/tap", json={"x": 150, "y": 300})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    mock_tap.assert_called_once_with("realme", 150, 300, actor=TEST_ACTOR)


def test_swipe_action(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/devices/{id}/swipe вызывает actions.swipe."""
    mock_swipe = AsyncMock(return_value=None)
    monkeypatch.setattr(actions, "swipe", mock_swipe)

    res = client.post(
        "/api/devices/realme/swipe",
        json={"x1": 100, "y1": 500, "x2": 100, "y2": 100, "ms": 400},
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True
    mock_swipe.assert_called_once_with("realme", 100, 500, 100, 100, ms=400, actor=TEST_ACTOR)


def test_text_action(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/devices/{id}/text вызывает actions.type_text."""
    mock_type = AsyncMock(return_value=None)
    monkeypatch.setattr(actions, "type_text", mock_type)

    res = client.post("/api/devices/realme/text", json={"text": "Привет мир"})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    mock_type.assert_called_once_with("realme", "Привет мир", actor=TEST_ACTOR)


def test_key_action(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/devices/{id}/key вызывает actions.key."""
    mock_key = AsyncMock(return_value=None)
    monkeypatch.setattr(actions, "key", mock_key)

    res = client.post("/api/devices/realme/key", json={"name": "home"})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    mock_key.assert_called_once_with("realme", "home", actor=TEST_ACTOR)


def test_open_app_action(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/devices/{id}/open_app вызывает actions.open_app."""
    mock_open = AsyncMock(return_value=None)
    monkeypatch.setattr(actions, "open_app", mock_open)

    res = client.post("/api/devices/realme/open_app", json={"package": "com.android.chrome"})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    mock_open.assert_called_once_with("realme", "com.android.chrome", actor=TEST_ACTOR)


def test_list_apps_action(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/devices/{id}/apps вызывает actions.list_apps."""
    mock_apps = AsyncMock(return_value=["com.android.chrome", "com.android.settings"])
    monkeypatch.setattr(actions, "list_apps", mock_apps)

    res = client.get("/api/devices/realme/apps")
    assert res.status_code == 200
    assert res.json() == ["com.android.chrome", "com.android.settings"]
    mock_apps.assert_called_once_with("realme", actor=TEST_ACTOR)


def test_ui_dump_action(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/devices/{id}/ui вызывает actions.ui_dump."""
    expected_ui = [{"text": "OK", "resource_id": "btn_ok", "center": [100, 200]}]
    mock_ui = AsyncMock(return_value=expected_ui)
    monkeypatch.setattr(actions, "ui_dump", mock_ui)

    res = client.get("/api/devices/realme/ui")
    assert res.status_code == 200
    assert res.json() == expected_ui
    mock_ui.assert_called_once_with("realme", actor=TEST_ACTOR)


def test_audit_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/audit возвращает недавние записи журнала аудита."""
    mock_records = [{"id": 1, "actor": "admin", "action": "tap", "ok": True}]
    mock_audit = MagicMock()
    mock_audit.recent = AsyncMock(return_value=mock_records)
    monkeypatch.setattr("hub.audit.get_audit", lambda: mock_audit)

    res = client.get("/api/audit?limit=10")
    assert res.status_code == 200
    assert res.json() == mock_records
    mock_audit.recent.assert_called_once_with(limit=10)


def test_unknown_device_returns_404(client: TestClient) -> None:
    """Неизвестный device_id мапится в 404."""
    res_tap = client.post("/api/devices/unknown_dev/tap", json={"x": 10, "y": 10})
    assert res_tap.status_code == 404
    assert "не найдено" in res_tap.json()["detail"].lower()

    res_screen = client.get("/api/devices/unknown_dev/screenshot")
    assert res_screen.status_code == 404

    res_apps = client.get("/api/devices/unknown_dev/apps")
    assert res_apps.status_code == 404

    res_power = client.post("/api/devices/unknown_dev/power", json={"action": "wake"})
    assert res_power.status_code == 404


def test_adb_errors_mapping_502(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ошибки ADB мапятся в 502 с понятным текстом."""
    # 1. DeviceOffline -> 502
    monkeypatch.setattr(
        actions,
        "tap",
        AsyncMock(side_effect=DeviceOffline("Устройство 'realme' оффлайн или не найдено")),
    )
    res1 = client.post("/api/devices/realme/tap", json={"x": 10, "y": 10})
    assert res1.status_code == 502
    assert "оффлайн" in res1.json()["detail"]

    # 2. AdbTimeout -> 502
    monkeypatch.setattr(
        actions,
        "tap",
        AsyncMock(side_effect=AdbTimeout("Превышено время ожидания команды ADB (15.0 с)")),
    )
    res2 = client.post("/api/devices/realme/tap", json={"x": 10, "y": 10})
    assert res2.status_code == 502
    assert "время ожидания" in res2.json()["detail"]

    # 3. AdbError -> 502
    monkeypatch.setattr(
        actions,
        "tap",
        AsyncMock(side_effect=AdbError("Ошибка выполнения ADB (код 1)")),
    )
    res3 = client.post("/api/devices/realme/tap", json={"x": 10, "y": 10})
    assert res3.status_code == 502
    assert "Ошибка выполнения ADB" in res3.json()["detail"]


def test_rate_limiter_mapping_429(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Лимит rate_limit_per_min действий на устройство со скользящим окном мапится в 429."""
    # Устанавливаем лимит 2 действия в минуту
    settings = Settings(rate_limit_per_min=2)
    monkeypatch.setattr("hub.api.routes.get_settings", lambda: settings)
    monkeypatch.setattr(actions, "tap", AsyncMock(return_value=None))

    # Первые 2 действия разрешены
    res1 = client.post("/api/devices/realme/tap", json={"x": 10, "y": 10})
    assert res1.status_code == 200

    res2 = client.post("/api/devices/realme/tap", json={"x": 10, "y": 10})
    assert res2.status_code == 200

    # 3-е действие превышает лимит -> 429
    res3 = client.post("/api/devices/realme/tap", json={"x": 10, "y": 10})
    assert res3.status_code == 429
    assert "лимит действий" in res3.json()["detail"].lower()

    # Лимит действует на конкретное устройство: другое устройство (redmi) должно работать
    res_other = client.post("/api/devices/redmi/tap", json={"x": 10, "y": 10})
    assert res_other.status_code == 200


def test_no_shell_endpoint_exposed() -> None:
    """Проверка безопасности: shell не отдаётся в HTTP API вообще."""
    route_paths = [r.path for r in api_router.routes] + [r.path for r in public_router.routes]
    for path in route_paths:
        assert "shell" not in path.lower()


def test_power_action_success(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/devices/{id}/power успешен для wake, sleep, reboot (confirm=True), shutdown (confirm=True)."""
    mock_power = AsyncMock(return_value=None)
    monkeypatch.setattr(actions, "power", mock_power)

    # 1. wake
    res_wake = client.post("/api/devices/realme/power", json={"action": "wake"})
    assert res_wake.status_code == 200
    assert res_wake.json() == {"ok": True, "status": "ok"}
    mock_power.assert_called_with("realme", "wake", actor=TEST_ACTOR)

    # 2. sleep
    mock_power.reset_mock()
    res_sleep = client.post("/api/devices/realme/power", json={"action": "sleep"})
    assert res_sleep.status_code == 200
    assert res_sleep.json() == {"ok": True, "status": "ok"}
    mock_power.assert_called_with("realme", "sleep", actor=TEST_ACTOR)

    # 3. reboot с confirm=True
    mock_power.reset_mock()
    res_reboot = client.post("/api/devices/realme/power", json={"action": "reboot", "confirm": True})
    assert res_reboot.status_code == 200
    assert res_reboot.json() == {"ok": True, "status": "ok"}
    mock_power.assert_called_with("realme", "reboot", actor=TEST_ACTOR)

    # 4. shutdown с confirm=True
    mock_power.reset_mock()
    res_shutdown = client.post("/api/devices/realme/power", json={"action": "shutdown", "confirm": True})
    assert res_shutdown.status_code == 200
    assert res_shutdown.json() == {"ok": True, "status": "ok"}
    mock_power.assert_called_with("realme", "shutdown", actor=TEST_ACTOR)


def test_power_action_requires_confirm_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """reboot и shutdown требуют confirm=true иначе 400 с русским текстом."""
    mock_power = AsyncMock(return_value=None)
    monkeypatch.setattr(actions, "power", mock_power)

    # 1. reboot без поля confirm
    res_reboot_no_confirm = client.post("/api/devices/realme/power", json={"action": "reboot"})
    assert res_reboot_no_confirm.status_code == 400
    detail = res_reboot_no_confirm.json()["detail"].lower()
    assert "подтвержден" in detail or "confirm" in detail

    # 2. reboot с confirm=False
    res_reboot_false = client.post("/api/devices/realme/power", json={"action": "reboot", "confirm": False})
    assert res_reboot_false.status_code == 400

    # 3. shutdown без поля confirm
    res_shutdown_no_confirm = client.post("/api/devices/realme/power", json={"action": "shutdown"})
    assert res_shutdown_no_confirm.status_code == 400
    detail = res_shutdown_no_confirm.json()["detail"].lower()
    assert "подтвержден" in detail or "confirm" in detail

    # 4. shutdown с confirm=False
    res_shutdown_false = client.post("/api/devices/realme/power", json={"action": "shutdown", "confirm": False})
    assert res_shutdown_false.status_code == 400

    # actions.power не должен вызываться
    mock_power.assert_not_called()


def test_power_action_unknown_device_404(client: TestClient) -> None:
    """Неизвестное устройство возвращает 404."""
    res = client.post("/api/devices/unknown_dev/power", json={"action": "wake"})
    assert res.status_code == 404
    assert "не найдено" in res.json()["detail"].lower()


def test_power_action_adb_errors_502(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ошибки ADB мапятся в 502 с понятным текстом."""
    # 1. DeviceOffline -> 502
    mock_power = AsyncMock(side_effect=DeviceOffline("Устройство 'realme' оффлайн или не найдено"))
    monkeypatch.setattr(actions, "power", mock_power)
    res1 = client.post("/api/devices/realme/power", json={"action": "wake"})
    assert res1.status_code == 502
    assert "оффлайн" in res1.json()["detail"]

    # 2. AdbTimeout -> 502
    mock_power = AsyncMock(side_effect=AdbTimeout("Превышено время ожидания команды ADB (15.0 с)"))
    monkeypatch.setattr(actions, "power", mock_power)
    res2 = client.post("/api/devices/realme/power", json={"action": "wake"})
    assert res2.status_code == 502
    assert "время ожидания" in res2.json()["detail"]

    # 3. AdbError -> 502
    mock_power = AsyncMock(side_effect=AdbError("Ошибка выполнения ADB (код 1)"))
    monkeypatch.setattr(actions, "power", mock_power)
    res3 = client.post("/api/devices/realme/power", json={"action": "wake"})
    assert res3.status_code == 502
    assert "Ошибка выполнения ADB" in res3.json()["detail"]


def test_power_action_invalid_action_422(client: TestClient) -> None:
    """Неверное действие возвращает 422."""
    res1 = client.post("/api/devices/realme/power", json={"action": "invalid"})
    assert res1.status_code == 422

    res2 = client.post("/api/devices/realme/power", json={"action": "restart"})
    assert res2.status_code == 422

    res3 = client.post("/api/devices/realme/power", json={})
    assert res3.status_code == 422


def test_power_action_rate_limiter_429(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Эндпоинт питания учитывает общий лимит действий на устройство (429)."""
    settings = Settings(rate_limit_per_min=1)
    monkeypatch.setattr("hub.api.routes.get_settings", lambda: settings)
    monkeypatch.setattr(actions, "power", AsyncMock(return_value=None))

    res1 = client.post("/api/devices/realme/power", json={"action": "wake"})
    assert res1.status_code == 200

    res2 = client.post("/api/devices/realme/power", json={"action": "wake"})
    assert res2.status_code == 429
    assert "лимит действий" in res2.json()["detail"].lower()


def test_get_devices_charging_field(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/devices отдаёт charging (bool|None) для каждого устройства."""
    res_default = client.get("/api/devices")
    assert res_default.status_code == 200
    data_default = res_default.json()
    assert len(data_default) == 2
    for item in data_default:
        assert "charging" in item
        assert item["charging"] is None

    monkeypatch.setattr(DeviceStatus, "charging", True, raising=False)
    monkeypatch.setattr(
        "hub.devices.manager.DeviceManager.statuses",
        lambda self: [DeviceStatus(id="realme", name="R", online=True, charging=True)],
    )
    data = client.get("/api/devices").json()
    assert data[0]["charging"] is True


try:
    from hub.auth import Client
except (ImportError, AttributeError):
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Client:
        name: str
        rate_limit_per_min: int | None = None
        allow_power: bool = True


def test_power_nopower_client_403_and_wake_200(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    POST /api/devices/{id}/power с action reboot или shutdown возвращает 403 с русским текстом,
    если request.state.client.allow_power равно False.
    wake и sleep разрешены всем (200).
    """
    mock_power = AsyncMock(return_value=None)
    monkeypatch.setattr(actions, "power", mock_power)

    nopower_client = Client(name="nopower", allow_power=False)

    async def _mock_require_api(request: Request) -> str:
        request.state.client = nopower_client
        return nopower_client.name

    app.dependency_overrides[require_api] = _mock_require_api
    try:
        # 1. reboot с confirm=True -> 403
        res_reboot = client.post("/api/devices/realme/power", json={"action": "reboot", "confirm": True})
        assert res_reboot.status_code == 403
        detail_reboot = res_reboot.json()["detail"].lower()
        assert "запрещено" in detail_reboot or "питани" in detail_reboot

        # 2. reboot без confirm -> 403
        res_reboot_no_confirm = client.post("/api/devices/realme/power", json={"action": "reboot"})
        assert res_reboot_no_confirm.status_code == 403
        detail_reboot_nc = res_reboot_no_confirm.json()["detail"].lower()
        assert "запрещено" in detail_reboot_nc or "питани" in detail_reboot_nc

        # 3. shutdown с confirm=True -> 403
        res_shutdown = client.post("/api/devices/realme/power", json={"action": "shutdown", "confirm": True})
        assert res_shutdown.status_code == 403
        detail_shutdown = res_shutdown.json()["detail"].lower()
        assert "запрещено" in detail_shutdown or "питани" in detail_shutdown

        # 4. shutdown без confirm -> 403
        res_shutdown_no_confirm = client.post("/api/devices/realme/power", json={"action": "shutdown"})
        assert res_shutdown_no_confirm.status_code == 403

        # actions.power не должен вызываться для 403
        mock_power.assert_not_called()

        # 5. wake разрешён всем -> 200
        res_wake = client.post("/api/devices/realme/power", json={"action": "wake"})
        assert res_wake.status_code == 200
        assert res_wake.json() == {"ok": True, "status": "ok"}
        mock_power.assert_called_once_with("realme", "wake", actor="nopower")

        # 6. sleep также разрешён всем -> 200
        mock_power.reset_mock()
        res_sleep = client.post("/api/devices/realme/power", json={"action": "sleep"})
        assert res_sleep.status_code == 200
        assert res_sleep.json() == {"ok": True, "status": "ok"}
        mock_power.assert_called_once_with("realme", "sleep", actor="nopower")
    finally:
        app.dependency_overrides.pop(require_api, None)


def test_independent_client_rate_limits(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Лимит действий в минуту считается на пару (клиент, устройство).
    Один клиент не расходует лимит другого: лимиты разных клиентов независимы.
    """
    monkeypatch.setattr(actions, "tap", AsyncMock(return_value=None))

    current_client: Client | None = None

    async def _dynamic_require_api(request: Request) -> str:
        if current_client is not None:
            request.state.client = current_client
            return current_client.name
        return "default"

    app.dependency_overrides[require_api] = _dynamic_require_api
    try:
        # Клиент 1: лимит 2 действия в минуту
        client_1 = Client(name="client_1", rate_limit_per_min=2)
        # Клиент 2: лимит 2 действия в минуту
        client_2 = Client(name="client_2", rate_limit_per_min=2)

        # Клиент 1 исчерпывает свой лимит на realme
        current_client = client_1
        res1 = client.post("/api/devices/realme/tap", json={"x": 10, "y": 10})
        assert res1.status_code == 200
        res2 = client.post("/api/devices/realme/tap", json={"x": 10, "y": 10})
        assert res2.status_code == 200
        res3 = client.post("/api/devices/realme/tap", json={"x": 10, "y": 10})
        assert res3.status_code == 429
        assert "лимит действий" in res3.json()["detail"].lower()

        # Клиент 2 на том же самом устройстве realme не исчерпал лимит!
        current_client = client_2
        res4 = client.post("/api/devices/realme/tap", json={"x": 10, "y": 10})
        assert res4.status_code == 200
        res5 = client.post("/api/devices/realme/tap", json={"x": 10, "y": 10})
        assert res5.status_code == 200
        # И только 3-й запрос клиента 2 блокируется
        res6 = client.post("/api/devices/realme/tap", json={"x": 10, "y": 10})
        assert res6.status_code == 429

        # Клиент 1 на другом устройстве (redmi) также имеет независимый лимит
        current_client = client_1
        res_other = client.post("/api/devices/redmi/tap", json={"x": 10, "y": 10})
        assert res_other.status_code == 200
    finally:
        app.dependency_overrides.pop(require_api, None)


def test_client_rate_limit_fallback_settings(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Если client.rate_limit_per_min не задан (None), используется settings.rate_limit_per_min.
    """
    settings = Settings(rate_limit_per_min=1)
    monkeypatch.setattr("hub.api.routes.get_settings", lambda: settings)
    monkeypatch.setattr(actions, "tap", AsyncMock(return_value=None))

    client_default = Client(name="default_limiter", rate_limit_per_min=None)

    async def _mock_require_api(request: Request) -> str:
        request.state.client = client_default
        return client_default.name

    app.dependency_overrides[require_api] = _mock_require_api
    try:
        res1 = client.post("/api/devices/realme/tap", json={"x": 10, "y": 10})
        assert res1.status_code == 200

        res2 = client.post("/api/devices/realme/tap", json={"x": 10, "y": 10})
        assert res2.status_code == 429
        assert "лимит действий" in res2.json()["detail"].lower()
    finally:
        app.dependency_overrides.pop(require_api, None)


def test_missing_client_assumed_allow_power_true(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """При отсутствии клиента в request.state считаем allow_power=True."""
    mock_power = AsyncMock(return_value=None)
    monkeypatch.setattr(actions, "power", mock_power)

    # _fake_require_api по умолчанию не выставляет request.state.client
    res = client.post("/api/devices/realme/power", json={"action": "reboot", "confirm": True})
    assert res.status_code == 200
    mock_power.assert_called_once_with("realme", "reboot", actor=TEST_ACTOR)


def test_devices_list_includes_busy_status(client: TestClient) -> None:
    """GET /api/devices отдаёт busy/activity: свободен без операций."""
    res = client.get("/api/devices")
    assert res.status_code == 200
    for item in res.json():
        assert item["busy"] is False
        assert item["activity"] is None


def test_fetch_page_route_includes_new_timing_fields(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP-ответ /api/devices/{id}/fetch_page отдаёт новые поля: t в json_responses, woke и t_first_match."""
    from hub.devices import browser

    mock_result = {
        "url": "https://example.com",
        "title": "Example",
        "text": "Hello world",
        "truncated": False,
        "captcha": False,
        "finished": True,
        "seconds": 1.2,
        "woke": True,
        "t_first_match": 0.45,
        "json_responses": [
            {
                "url": "https://example.com/api/data",
                "status": 200,
                "body": '{"ok": true}',
                "t": 0.45,
            }
        ],
    }
    monkeypatch.setattr(browser, "fetch_page", AsyncMock(return_value=mock_result))

    res = client.post(
        "/api/devices/realme/fetch_page",
        json={"url": "https://example.com", "capture_json": ["/api/"]},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["woke"] is True
    assert data["t_first_match"] == 0.45
    assert len(data["json_responses"]) == 1
    assert data["json_responses"][0]["t"] == 0.45

