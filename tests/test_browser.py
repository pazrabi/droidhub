from __future__ import annotations

import asyncio
import time
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


# --- Новые тесты для времени (t, t_first_match) и woke ---

class FakeDevToolsContext:
    def __init__(self, cdp: Any) -> None:
        self.cdp = cdp

    async def __aenter__(self) -> "FakeDevToolsContext":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        pass

    async def new_tab(self, url: str = "about:blank") -> dict:
        return {"id": "fake-tab-id"}

    async def connect(self, tab: dict) -> Any:
        return self.cdp

    async def close_tab(self, tab_id: str) -> None:
        pass


class FakeFullCdp:
    def __init__(self, events: list[dict] | None = None, body_map: dict[str, str] | None = None) -> None:
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        for e in (events or []):
            self.events.put_nowait(e)
        self.body_map = body_map or {}

    async def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        if method == "Network.getResponseBody":
            rid = (params or {}).get("requestId", "")
            return {"body": self.body_map.get(rid, '{"status": "ok"}'), "base64Encoded": False}
        return {}

    async def close(self) -> None:
        pass


async def test_timing_responses_grow_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Несколько ответов приходят друг за другом с задержкой: поле t растёт по порядку."""
    monkeypatch.setattr(browser, "QUIET_SECONDS", 0.1)

    cdp = FakeCdp([])

    async def _feed() -> None:
        for ev in _request("1", "https://x.ru/api/1"):
            cdp.events.put_nowait(ev)
        await asyncio.sleep(0.04)
        for ev in _request("2", "https://x.ru/api/2"):
            cdp.events.put_nowait(ev)
        await asyncio.sleep(0.04)
        for ev in _request("3", "https://x.ru/api/3"):
            cdp.events.put_nowait(ev)
        cdp.events.put_nowait({"method": "Page.loadEventFired", "params": {}})

    feeder = asyncio.create_task(_feed())

    captured: dict = {}
    bodies: dict = {}
    finish_times: dict[str, float] = {}
    started = time.monotonic()

    ok = await browser._wait_for_page(
        cdp, 2.0, captured, [], bodies, started=started, finish_times=finish_times
    )
    assert ok is True
    await feeder

    out = await browser._collect_json(bodies, finish_times=finish_times)
    assert len(out) == 3
    assert [r["url"] for r in out] == [
        "https://x.ru/api/1",
        "https://x.ru/api/2",
        "https://x.ru/api/3",
    ]
    t_vals = [r["t"] for r in out]
    assert all(isinstance(t, float) for t in t_vals)
    assert t_vals[0] <= t_vals[1] <= t_vals[2]
    assert t_vals[2] > t_vals[0]


async def test_fetch_page_woke_true(manager: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_page возвращает woke=True, если экран пришлось будить."""
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=True))
    cdp = FakeFullCdp([{"method": "Page.loadEventFired", "params": {}}])
    monkeypatch.setattr(browser, "_DevTools", lambda serial: FakeDevToolsContext(cdp))
    monkeypatch.setattr(browser, "_read_page", AsyncMock(return_value={
        "url": "https://example.com/", "title": "Example", "text": "Page text", "truncated": False, "captcha": False
    }))
    monkeypatch.setattr(browser, "QUIET_SECONDS", 0.05)

    res = await browser.fetch_page("realme", "https://example.com/")
    assert res["woke"] is True


async def test_fetch_page_woke_false(manager: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_page возвращает woke=False, если экран уже был включен."""
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))
    cdp = FakeFullCdp([{"method": "Page.loadEventFired", "params": {}}])
    monkeypatch.setattr(browser, "_DevTools", lambda serial: FakeDevToolsContext(cdp))
    monkeypatch.setattr(browser, "_read_page", AsyncMock(return_value={
        "url": "https://example.com/", "title": "Example", "text": "Page text", "truncated": False, "captcha": False
    }))
    monkeypatch.setattr(browser, "QUIET_SECONDS", 0.05)

    res = await browser.fetch_page("realme", "https://example.com/")
    assert res["woke"] is False


async def test_fetch_page_t_first_match_and_timing(manager: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """Проверка t_first_match: null если подходящих ответов нет, и float если есть."""
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))
    monkeypatch.setattr(browser, "_read_page", AsyncMock(return_value={
        "url": "https://example.com/", "title": "Example", "text": "Page text", "truncated": False, "captcha": False
    }))
    monkeypatch.setattr(browser, "QUIET_SECONDS", 0.05)

    # 1. capture_json задан, но подходящих ответов нет -> t_first_match is None
    events_no_match = (
        _request("1", "https://example.com/api/other")
        + [{"method": "Page.loadEventFired", "params": {}}]
    )
    cdp1 = FakeFullCdp(events_no_match)
    monkeypatch.setattr(browser, "_DevTools", lambda serial: FakeDevToolsContext(cdp1))

    res1 = await browser.fetch_page("realme", "https://example.com/", capture_json=["/api/target"])
    assert res1["t_first_match"] is None
    assert len(res1["json_responses"]) == 0

    # 2. capture_json задан и есть подходящий ответ -> t_first_match является числом float
    events_with_match = (
        _request("1", "https://example.com/api/target")
        + [{"method": "Page.loadEventFired", "params": {}}]
    )
    cdp2 = FakeFullCdp(events_with_match)
    monkeypatch.setattr(browser, "_DevTools", lambda serial: FakeDevToolsContext(cdp2))

    res2 = await browser.fetch_page("realme", "https://example.com/", capture_json=["/api/target"])
    assert isinstance(res2["t_first_match"], float)
    assert res2["t_first_match"] >= 0.0
    assert len(res2["json_responses"]) == 1
    assert res2["json_responses"][0]["t"] == res2["t_first_match"]

    # 3. capture_json пуст -> любой возвращённый ответ подходит, t_first_match число float
    events_empty_capture = (
        _request("1", "https://example.com/api/anything")
        + [{"method": "Page.loadEventFired", "params": {}}]
    )
    cdp3 = FakeFullCdp(events_empty_capture)
    monkeypatch.setattr(browser, "_DevTools", lambda serial: FakeDevToolsContext(cdp3))

    res3 = await browser.fetch_page("realme", "https://example.com/", capture_json=[])
    assert isinstance(res3["t_first_match"], float)
    assert res3["t_first_match"] >= 0.0
    assert res3["t_first_match"] == res3["json_responses"][0]["t"]


# --- Тесты для replay_post ---

class ReplayFakeCdp:
    def __init__(
        self,
        events: list[dict],
        evaluate_response: dict | None = None,
        evaluate_exception: Exception | None = None,
        post_data: str | None = None,
    ) -> None:
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        for e in events:
            self.events.put_nowait(e)
        self.evaluate_response = evaluate_response
        self.evaluate_exception = evaluate_exception
        self.post_data = post_data
        self.evaluate_calls: list[dict] = []
        self.get_post_data_calls: list[dict] = []

    async def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        params = params or {}
        if method == "Network.getResponseBody":
            return {"body": '{"initial": 10}', "base64Encoded": False}
        if method == "Network.getRequestPostData":
            self.get_post_data_calls.append(params)
            if self.post_data is not None:
                return {"postData": self.post_data}
            return {}
        if method == "Runtime.evaluate":
            self.evaluate_calls.append(params)
            if self.evaluate_exception is not None:
                raise self.evaluate_exception
            if self.evaluate_response is not None:
                return self.evaluate_response
            return {"result": {"value": {"ok": True, "status": 200, "body": '{"replayed": 50}'}}}
        return {}

    async def close(self) -> None:
        pass


def _post_request_events(
    rid: str,
    url: str,
    post_data: str | None = None,
    headers: dict | None = None,
) -> list[dict]:
    req: dict[str, Any] = {
        "url": url,
        "method": "POST",
        "headers": headers or {"Content-Type": "application/json"},
    }
    if post_data is not None:
        req["postData"] = post_data
    return [
        {"method": "Network.requestWillBeSent", "params": {"requestId": rid, "request": req}},
        {
            "method": "Network.responseReceived",
            "params": {
                "requestId": rid,
                "type": "XHR",
                "response": {"url": url, "status": 200, "mimeType": "application/json"},
            },
        },
        {"method": "Network.loadingFinished", "params": {"requestId": rid}},
    ]


async def test_fetch_page_replay_post_success(manager: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """Перехват POST, перезапись полей в fetch, рост t и флаг replayed: True."""
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))
    monkeypatch.setattr(browser, "_read_page", AsyncMock(return_value={
        "url": "https://avia.ru/", "title": "Avia", "text": "Offers", "truncated": False, "captcha": False
    }))
    monkeypatch.setattr(browser, "QUIET_SECONDS", 0.05)

    headers = {
        "Content-Type": "application/json",
        "Host": "avia.ru",
        "Cookie": "session=xyz",
        "Authorization": "Bearer token123",
        "Content-Length": "34",
    }
    events = (
        _post_request_events("1", "https://avia.ru/avia/offers/v2?query=1", post_data='{"limit": 10, "page": 1}', headers=headers)
        + [{"method": "Page.loadEventFired", "params": {}}]
    )
    cdp = ReplayFakeCdp(events)
    monkeypatch.setattr(browser, "_DevTools", lambda serial: FakeDevToolsContext(cdp))

    res = await browser.fetch_page(
        "realme",
        "https://avia.ru/",
        replay_post={"url_contains": "/avia/offers/v2", "set": {"limit": 50, "extra": "yes"}},
    )

    # 1. Проверяем, что в fetch ушло тело с перезаписанными полями и отфильтрованными заголовками
    assert len(cdp.evaluate_calls) == 1
    eval_call = cdp.evaluate_calls[0]
    assert eval_call.get("awaitPromise") is True
    assert eval_call.get("returnByValue") is True
    expr = eval_call["expression"]
    assert 'fetch("https://avia.ru/avia/offers/v2?query=1"' in expr
    assert '"credentials": "include"' in expr or "credentials: 'include'" in expr
    assert '"Content-Type": "application/json"' in expr or '"content-type": "application/json"' in expr
    assert "Authorization" in expr
    # Служебные заголовки удалены
    assert "Cookie" not in expr and "cookie" not in expr
    assert "Host" not in expr and "host" not in expr
    assert "Content-Length" not in expr and "content-length" not in expr
    # Тело содержит перезаписанные поля
    assert '"limit": 50' in expr
    assert '"extra": "yes"' in expr
    assert '"page": 1' in expr

    # 2. Проверяем json_responses: начальный + повторный с replayed: True и ростом t
    responses = res["json_responses"]
    assert len(responses) == 2
    orig_resp, replay_resp = responses[0], responses[1]
    assert orig_resp["url"] == "https://avia.ru/avia/offers/v2?query=1"
    assert orig_resp["body"] == '{"initial": 10}'
    assert orig_resp.get("replayed") is not True

    assert replay_resp["url"] == "https://avia.ru/avia/offers/v2?query=1"
    assert replay_resp["body"] == '{"replayed": 50}'
    assert replay_resp["replayed"] is True
    assert isinstance(replay_resp["t"], float)
    assert replay_resp["t"] >= orig_resp["t"]
    assert res["finished"] is True


async def test_fetch_page_replay_post_via_get_request_post_data(manager: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """Если postData не пришёл в Network.requestWillBeSent, берём через Network.getRequestPostData."""
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))
    monkeypatch.setattr(browser, "_read_page", AsyncMock(return_value={
        "url": "https://avia.ru/", "title": "Avia", "text": "Offers", "truncated": False, "captcha": False
    }))
    monkeypatch.setattr(browser, "QUIET_SECONDS", 0.05)

    # post_data=None в событии
    events = (
        _post_request_events("r10", "https://avia.ru/api/tickets", post_data=None)
        + [{"method": "Page.loadEventFired", "params": {}}]
    )
    cdp = ReplayFakeCdp(events, post_data='{"offset": 0, "size": 10}')
    monkeypatch.setattr(browser, "_DevTools", lambda serial: FakeDevToolsContext(cdp))

    res = await browser.fetch_page(
        "realme",
        "https://avia.ru/",
        replay_post={"url_contains": "/api/tickets", "set": {"size": 100}},
    )

    assert len(cdp.get_post_data_calls) == 1
    assert cdp.get_post_data_calls[0]["requestId"] == "r10"
    assert len(cdp.evaluate_calls) == 1
    assert '"size": 100' in cdp.evaluate_calls[0]["expression"]
    assert len(res["json_responses"]) == 2
    assert res["json_responses"][1]["replayed"] is True


async def test_fetch_page_replay_post_not_intercepted_silently_skipped(manager: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """Если POST не перехвачен, повтор молча пропускается без ошибок."""
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))
    monkeypatch.setattr(browser, "_read_page", AsyncMock(return_value={
        "url": "https://avia.ru/", "title": "Avia", "text": "Offers", "truncated": False, "captcha": False
    }))
    monkeypatch.setattr(browser, "QUIET_SECONDS", 0.05)

    events = (
        _request("1", "https://avia.ru/api/other")
        + [{"method": "Page.loadEventFired", "params": {}}]
    )
    cdp = ReplayFakeCdp(events)
    monkeypatch.setattr(browser, "_DevTools", lambda serial: FakeDevToolsContext(cdp))

    res = await browser.fetch_page(
        "realme",
        "https://avia.ru/",
        replay_post={"url_contains": "/avia/offers/v2", "set": {"limit": 50}},
    )
    assert len(cdp.evaluate_calls) == 0
    assert len(res["json_responses"]) == 1
    assert "replayed" not in res["json_responses"][0]


async def test_fetch_page_replay_post_body_not_json_dict_silently_skipped(manager: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """Если тело перехваченного запроса не JSON-словарь (строка или массив), повтор пропускается."""
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))
    monkeypatch.setattr(browser, "_read_page", AsyncMock(return_value={
        "url": "https://avia.ru/", "title": "Avia", "text": "Offers", "truncated": False, "captcha": False
    }))
    monkeypatch.setattr(browser, "QUIET_SECONDS", 0.05)

    # Тело запроса — JSON массив, а не словарь
    events = (
        _post_request_events("1", "https://avia.ru/api/offers", post_data="[1, 2, 3]")
        + [{"method": "Page.loadEventFired", "params": {}}]
    )
    cdp = ReplayFakeCdp(events)
    monkeypatch.setattr(browser, "_DevTools", lambda serial: FakeDevToolsContext(cdp))

    res = await browser.fetch_page(
        "realme",
        "https://avia.ru/",
        replay_post={"url_contains": "/api/offers", "set": {"limit": 50}},
    )
    assert len(cdp.evaluate_calls) == 0
    assert len(res["json_responses"]) == 1


async def test_fetch_page_replay_post_fetch_error_silently_skipped(manager: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """Если fetch завершился ошибкой или вернул не-2xx статус, повтор пропускается."""
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))
    monkeypatch.setattr(browser, "_read_page", AsyncMock(return_value={
        "url": "https://avia.ru/", "title": "Avia", "text": "Offers", "truncated": False, "captcha": False
    }))
    monkeypatch.setattr(browser, "QUIET_SECONDS", 0.05)

    # 1. Fetch вернул 500
    events1 = (
        _post_request_events("1", "https://avia.ru/api/offers", post_data='{"limit": 10}')
        + [{"method": "Page.loadEventFired", "params": {}}]
    )
    cdp1 = ReplayFakeCdp(
        events1,
        evaluate_response={"result": {"value": {"ok": False, "status": 500, "body": "server error"}}},
    )
    monkeypatch.setattr(browser, "_DevTools", lambda serial: FakeDevToolsContext(cdp1))
    res1 = await browser.fetch_page(
        "realme", "https://avia.ru/", replay_post={"url_contains": "/api/offers", "set": {"limit": 50}}
    )
    assert len(res1["json_responses"]) == 1

    # 2. Runtime.evaluate бросил исключение
    events2 = (
        _post_request_events("1", "https://avia.ru/api/offers", post_data='{"limit": 10}')
        + [{"method": "Page.loadEventFired", "params": {}}]
    )
    cdp2 = ReplayFakeCdp(
        events2,
        evaluate_exception=browser.BrowserError("JS runtime error"),
    )
    monkeypatch.setattr(browser, "_DevTools", lambda serial: FakeDevToolsContext(cdp2))
    res2 = await browser.fetch_page(
        "realme", "https://avia.ru/", replay_post={"url_contains": "/api/offers", "set": {"limit": 50}}
    )
    assert len(res2["json_responses"]) == 1


async def test_fetch_page_replay_post_validation(manager: DeviceManager) -> None:
    """Валидация: url_contains непустая строка, set словарь."""
    with pytest.raises(ValueError, match="url_contains"):
        await browser.fetch_page("realme", "https://x.ru", replay_post={"url_contains": "", "set": {}})

    with pytest.raises(ValueError, match="set"):
        await browser.fetch_page("realme", "https://x.ru", replay_post={"url_contains": "/api", "set": "not-a-dict"})

    with pytest.raises(ValueError, match="словарём"):
        await browser.fetch_page("realme", "https://x.ru", replay_post="invalid")
