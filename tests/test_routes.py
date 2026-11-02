from __future__ import annotations

import sys
import types
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import APIRouter, FastAPI, HTTPException
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
