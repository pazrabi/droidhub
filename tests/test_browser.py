from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub.api.routes import api_router, public_router, require_api, reset_rate_limiter
from hub.config import DeviceConfig
from hub.devices import actions, browser
from hub.devices.manager import DeviceManager


# --- капча ---

@pytest.mark.parametrize(
    "url,title,text,expected",
    [
        ("https://x.ru/showcaptcha?x=1", "", "", True),
        ("https://yandex.ru/", "Ой!", "Подтвердите, что вы не робот", True),
        ("https://x.ru/", "Just a moment...", "", True),
        ("https://www.aviasales.ru/search/X", "KUF → EVN", "12 300 ₽ Самара — Ереван", False),
    ],
)
def test_detect_captcha(url: str, title: str, text: str, expected: bool) -> None:
    assert browser.detect_captcha(url, title, text) is expected


# --- ожидание страницы и отбор JSON ---

class FakeCdp:
    def __init__(self, events: list[dict]) -> None:
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        for e in events:
            self.events.put_nowait(e)
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        self.calls.append((method, params or {}))
        if method == "Network.getResponseBody":
            return {"body": '{"price": 100}', "base64Encoded": False}
        return {}


async def test_wait_for_page_finishes_after_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser, "QUIET_SECONDS", 0.2)
    events = [
        {"method": "Network.requestWillBeSent", "params": {"requestId": "1"}},
        {"method": "Network.responseReceived", "params": {
            "requestId": "1", "type": "XHR",
            "response": {"url": "https://x.ru/api/results", "status": 200, "mimeType": "application/json"}}},
        {"method": "Network.loadingFinished", "params": {"requestId": "1"}},
        {"method": "Page.loadEventFired", "params": {}},
    ]
    cdp = FakeCdp(events)
    captured: dict = {}
    assert await browser._wait_for_page(cdp, 5.0, captured) is True
    assert captured["1"]["url"] == "https://x.ru/api/results"


async def test_wait_for_page_times_out_when_no_load() -> None:
    cdp = FakeCdp([])
    assert await browser._wait_for_page(cdp, 0.3, {}) is False


async def test_collect_json_filters_by_default_and_patterns() -> None:
    captured = {
        "1": {"url": "https://x.ru/api/results", "status": 200, "mime": "application/json", "type": "XHR"},
        "2": {"url": "https://x.ru/logo.png", "status": 200, "mime": "image/png", "type": "Image"},
        "3": {"url": "https://x.ru/page.html", "status": 200, "mime": "text/html", "type": "Document"},
        "4": {"url": "https://t.ru/track", "status": 200, "mime": "application/json", "type": "Fetch"},
    }
    only_json = await browser._collect_json(FakeCdp([]), captured, [])
    assert [r["url"] for r in only_json] == ["https://x.ru/api/results", "https://t.ru/track"]

    by_pattern = await browser._collect_json(FakeCdp([]), captured, ["/api/results"])
    assert [r["url"] for r in by_pattern] == ["https://x.ru/api/results"]
    assert by_pattern[0]["body"] == '{"price": 100}'


# --- публичные функции ---

@pytest.fixture
def manager() -> DeviceManager:
    mgr = DeviceManager([DeviceConfig(id="realme", name="Realme", host="100.64.0.1", port=5555)])
    actions.set_manager(mgr)
    return mgr


async def test_fetch_page_rejects_non_http(manager: DeviceManager) -> None:
    with pytest.raises(ValueError, match="http"):
        await browser.fetch_page("realme", "file:///etc/passwd")


async def test_locked_serializes_requests_per_device() -> None:
    order: list[str] = []

    async def job(name: str, delay: float) -> str:
        order.append(f"start {name}")
        await asyncio.sleep(delay)
        order.append(f"end {name}")
        return name

    await asyncio.gather(
        browser._locked("dev-a", lambda: job("1", 0.1)),
        browser._locked("dev-a", lambda: job("2", 0.0)),
    )
    assert order == ["start 1", "end 1", "start 2", "end 2"]


async def test_locked_busy_when_queue_too_long(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser, "LOCK_WAIT_SECONDS", 0.1)

    async def slow() -> None:
        await asyncio.sleep(0.5)

    first = asyncio.create_task(browser._locked("dev-b", slow))
    await asyncio.sleep(0.01)
    with pytest.raises(browser.BrowserBusy):
        await browser._locked("dev-b", slow)
    await first


# --- HTTP ---

@pytest.fixture
def client(manager: DeviceManager) -> TestClient:
    reset_rate_limiter()
    app = FastAPI()
    app.dependency_overrides[require_api] = lambda: "bot"
    app.include_router(public_router)
    app.include_router(api_router)
    return TestClient(app)


def test_api_fetch_page(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    result = {"url": "https://x.ru/", "title": "T", "text": "hello", "captcha": False, "json_responses": []}
    mock = AsyncMock(return_value=result)
    monkeypatch.setattr(browser, "fetch_page", mock)
    res = client.post(
        "/api/devices/realme/fetch_page",
        json={"url": "https://x.ru/", "wait_seconds": 5, "capture_json": ["/api/"]},
    )
    assert res.status_code == 200
    assert res.json()["text"] == "hello"
    mock.assert_called_once_with("realme", "https://x.ru/", 5.0, ["/api/"], 200000, actor="bot")


@pytest.mark.parametrize(
    "exc,code",
    [
        (browser.BrowserUnavailable("Chrome не отвечает"), 502),
        (browser.BrowserBusy("занято"), 429),
        (ValueError("плохой адрес"), 400),
    ],
)
def test_api_fetch_page_errors(client: TestClient, monkeypatch: pytest.MonkeyPatch, exc: Exception, code: int) -> None:
    monkeypatch.setattr(browser, "fetch_page", AsyncMock(side_effect=exc))
    res = client.post("/api/devices/realme/fetch_page", json={"url": "https://x.ru/"})
    assert res.status_code == code
    assert str(exc) in res.json()["detail"]


def test_api_tabs_and_text(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser, "browser_tabs", AsyncMock(return_value=[{"index": 0, "title": "A", "url": "u"}]))
    monkeypatch.setattr(browser, "page_text", AsyncMock(return_value={"text": "abc"}))
    assert client.get("/api/devices/realme/browser/tabs").json()[0]["title"] == "A"
    res = client.get("/api/devices/realme/browser/text?tab_index=1")
    assert res.json() == {"text": "abc"}
    browser.page_text.assert_called_once_with("realme", 1, 200000, actor="bot")
