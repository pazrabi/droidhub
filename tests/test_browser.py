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

    async def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        if method == "Network.getResponseBody":
            return {"body": '{"price": 100}', "base64Encoded": False}
        return {}


def _request(rid: str, url: str, rtype: str = "XHR", mime: str = "application/json") -> list[dict]:
    return [
        {"method": "Network.requestWillBeSent", "params": {"requestId": rid, "request": {"url": url}}},
        {"method": "Network.responseReceived", "params": {
            "requestId": rid, "type": rtype, "response": {"url": url, "status": 200, "mimeType": mime}}},
        {"method": "Network.loadingFinished", "params": {"requestId": rid}},
    ]


async def test_wait_for_page_finishes_after_quiet_and_collects_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser, "QUIET_SECONDS", 0.2)
    events = (
        _request("1", "https://x.ru/api/results")
        + _request("2", "https://x.ru/logo.png", "Image", "image/png")
        + _request("3", "https://x.ru/page.html", "Document", "text/html")
        + [{"method": "Page.loadEventFired", "params": {}}]
    )
    captured: dict = {}
    bodies: dict = {}
    assert await browser._wait_for_page(FakeCdp(events), 5.0, captured, [], bodies) is True
    out = await browser._collect_json(bodies)
    assert [r["url"] for r in out] == ["https://x.ru/api/results"]
    assert out[0]["body"] == '{"price": 100}'


async def test_wait_for_page_with_pattern_ignores_endless_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сайт опрашивает сервер вечно (висящий запрос), но нужный ответ получен: ждать до конца не надо."""
    monkeypatch.setattr(browser, "QUIET_SECONDS", 0.2)
    events = _request("1", "https://x.ru/search/results") + [
        {"method": "Network.requestWillBeSent", "params": {"requestId": "9", "request": {"url": "https://t.ru/poll"}}},
    ]
    captured: dict = {}
    bodies: dict = {}
    assert await browser._wait_for_page(FakeCdp(events), 5.0, captured, ["/search/results"], bodies) is True
    out = await browser._collect_json(bodies)
    assert [r["url"] for r in out] == ["https://x.ru/search/results"]


def _poll(rid: str, body_marker: str) -> list[dict]:
    return _request(rid, "https://x.ru/search/results")


class MarkerCdp(FakeCdp):
    async def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        if method == "Network.getResponseBody":
            marker = "DONE" if params["requestId"] == "3" else "PARTIAL"
            return {"body": '{"s": "%s"}' % marker, "base64Encoded": False}
        return {}


async def test_wait_for_collects_all_polls_until_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Несколько опросов одного адреса: ждём ответ с признаком конца, возвращаем все ответы по порядку."""
    monkeypatch.setattr(browser, "QUIET_SECONDS", 0.2)
    events = _request("1", "https://x.ru/search/results") + _request("2", "https://x.ru/search/results") \
        + _request("3", "https://x.ru/search/results")
    bodies: dict = {}
    ok = await browser._wait_for_page(
        MarkerCdp(events), 5.0, {}, ["/search/results"], bodies, wait_for="DONE"
    )
    assert ok is True
    out = await browser._collect_json(bodies)
    assert [r["body"] for r in out] == ['{"s": "PARTIAL"}', '{"s": "PARTIAL"}', '{"s": "DONE"}']


async def test_wait_for_not_found_times_out() -> None:
    events = _request("1", "https://x.ru/search/results")
    ok = await browser._wait_for_page(
        MarkerCdp(events), 0.6, {}, ["/search/results"], {}, wait_for="NEVER"
    )
    assert ok is False


async def test_wait_full_waits_whole_time() -> None:
    start = asyncio.get_running_loop().time()
    ok = await browser._wait_for_page(FakeCdp(_request("1", "https://x.ru/a")), 0.6, {}, [], {}, wait_full=True)
    assert ok is True
    assert asyncio.get_running_loop().time() - start >= 0.55


async def test_wait_for_page_times_out_when_no_load() -> None:
    assert await browser._wait_for_page(FakeCdp([]), 0.3, {}) is False


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
    mock.assert_called_once_with("realme", "https://x.ru/", 5.0, ["/api/"], 200000, None, False, actor="bot")


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


# --- сбои превращаются в понятные ошибки, а не в 500 ---

async def test_cdp_call_timeout_is_browser_error() -> None:
    class SilentWs:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(10)

        async def send(self, data: str) -> None:
            pass

        async def close(self) -> None:
            pass

    cdp = browser._Cdp(SilentWs())
    try:
        with pytest.raises(browser.BrowserError, match="не ответил"):
            await cdp.call("Page.navigate", {"url": "https://x.ru"}, timeout=0.1)
    finally:
        await cdp.close()


async def test_guarded_maps_network_failures() -> None:
    async def broken() -> None:
        raise ConnectionResetError("reset")

    with pytest.raises(browser.BrowserUnavailable, match="оборвалась"):
        await browser._guarded(broken)()

    async def slow() -> None:
        raise asyncio.TimeoutError()

    with pytest.raises(browser.BrowserUnavailable):
        await browser._guarded(slow)()
