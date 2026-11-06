from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub.api.routes import api_router, public_router, require_api, reset_rate_limiter
from hub.config import DeviceConfig
from hub.devices import actions, activity, browser, fetch_jobs
from hub.devices.manager import DeviceManager


class FakeDevToolsContext:
    def __init__(self, cdp: Any) -> None:
        self.cdp = cdp
        self.closed_tabs: list[str] = []
        self.exited = False

    async def __aenter__(self) -> "FakeDevToolsContext":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await asyncio.sleep(0)
        self.exited = True

    async def new_tab(self, url: str = "about:blank") -> dict:
        return {"id": "fake-tab-id"}

    async def connect(self, tab: dict) -> Any:
        return self.cdp

    async def close_tab(self, tab_id: str) -> None:
        await asyncio.sleep(0)
        self.closed_tabs.append(tab_id)


class FakeFullCdp:
    def __init__(self, events: list[dict] | None = None, body_map: dict[str, str] | None = None) -> None:
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        for e in (events or []):
            self.events.put_nowait(e)
        self.body_map = body_map or {}
        self.closed = False

    async def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        if method == "Network.getResponseBody":
            rid = (params or {}).get("requestId", "")
            return {"body": self.body_map.get(rid, '{"status": "ok"}'), "base64Encoded": False}
        if method == "Runtime.evaluate":
            return {
                "result": {
                    "value": json.dumps({
                        "url": "https://example.com/",
                        "title": "Example",
                        "text": "Example page text",
                    })
                }
            }
        return {}

    async def close(self) -> None:
        await asyncio.sleep(0)
        self.closed = True


def _request_events(rid: str, url: str, rtype: str = "XHR", mime: str = "application/json") -> list[dict]:
    return [
        {"method": "Network.requestWillBeSent", "params": {"requestId": rid, "request": {"url": url}}},
        {
            "method": "Network.responseReceived",
            "params": {
                "requestId": rid,
                "type": rtype,
                "response": {"url": url, "status": 200, "mimeType": mime},
            },
        },
        {"method": "Network.loadingFinished", "params": {"requestId": rid}},
    ]


@pytest.fixture
def manager() -> DeviceManager:
    mgr = DeviceManager([DeviceConfig(id="realme", name="Realme C21Y", host="100.64.0.1", port=5555)])
    actions.set_manager(mgr)
    return mgr


@pytest.fixture(autouse=True)
async def cleanup_jobs():
    yield
    await fetch_jobs.cancel_all()


async def test_timing_responses_grow_and_slice_sequential(manager: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ответы приходят по мере появления с возрастающим t; последовательный опрос since/next не теряет и не дублирует."""
    body_map = {
        "1": '{"item": 1}',
        "2": '{"item": 2}',
        "3": '{"item": 3}',
    }
    cdp = FakeFullCdp(body_map=body_map)
    fake_dev = FakeDevToolsContext(cdp)
    monkeypatch.setattr(fetch_jobs, "_DevTools", lambda serial: fake_dev)
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))

    job_id = await fetch_jobs.create_fetch_job("realme", "https://example.com/api", wait_seconds=5.0)
    job = fetch_jobs.get_active_job(job_id)
    assert job is not None

    # Подаем первый ответ
    for ev in _request_events("1", "https://example.com/api/1"):
        cdp.events.put_nowait(ev)
    await asyncio.sleep(0.05)

    items0, next0 = job.get_slice(since=0)
    assert len(items0) == 1
    assert items0[0]["i"] == 0
    assert items0[0]["body"] == '{"item": 1}'
    assert next0 == 1

    # Подаем второй и третий ответы с задержкой
    await asyncio.sleep(0.04)
    for ev in _request_events("2", "https://example.com/api/2"):
        cdp.events.put_nowait(ev)
    await asyncio.sleep(0.04)
    for ev in _request_events("3", "https://example.com/api/3"):
        cdp.events.put_nowait(ev)
    await asyncio.sleep(0.05)

    # Опрос с since=1
    items1, next1 = job.get_slice(since=1)
    assert len(items1) == 2
    assert [r["i"] for r in items1] == [1, 2]
    assert [r["body"] for r in items1] == ['{"item": 2}', '{"item": 3}']
    assert next1 == 3

    # Повтор запроса с тем же since=1 возвращает те же данные
    items1_repeat, next1_repeat = job.get_slice(since=1)
    assert items1_repeat == items1
    assert next1_repeat == 3

    # Опрос с since=3 возвращает пустой список
    items3, next3 = job.get_slice(since=3)
    assert items3 == []
    assert next3 == 3

    # Проверка возрастания t
    all_items, _ = job.get_slice(since=0)
    assert len(all_items) == 3
    t_vals = [r["t"] for r in all_items]
    assert all(isinstance(t, float) for t in t_vals)
    assert t_vals[0] <= t_vals[1] <= t_vals[2]
    assert t_vals[2] >= t_vals[0]


async def test_network_quiet_and_wait_for_seen(manager: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """network_quiet и wait_for_seen поднимаются в нужный момент (QUIET_SECONDS уменьшен)."""
    monkeypatch.setattr(fetch_jobs, "QUIET_SECONDS", 0.1)
    body_map = {
        "1": '{"status": "loading"}',
        "2": '{"status": "ready"}',
    }
    cdp = FakeFullCdp(body_map=body_map)
    fake_dev = FakeDevToolsContext(cdp)
    monkeypatch.setattr(fetch_jobs, "_DevTools", lambda serial: fake_dev)
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))

    job_id = await fetch_jobs.create_fetch_job(
        "realme", "https://example.com/", wait_for="ready", capture_json=["/api/"]
    )
    job = fetch_jobs.get_active_job(job_id)
    assert job is not None
    assert job.wait_for_seen is False
    assert job.is_network_quiet() is False

    # Первый запрос без wait_for
    for ev in _request_events("1", "https://example.com/api/step1"):
        cdp.events.put_nowait(ev)
    await asyncio.sleep(0.04)
    assert job.wait_for_seen is False

    # Второй запрос содержит wait_for="ready"
    for ev in _request_events("2", "https://example.com/api/step2"):
        cdp.events.put_nowait(ev)
    await asyncio.sleep(0.03)

    assert job.wait_for_seen is True

    # Ждём завершения затишья сети QUIET_SECONDS (0.1 с)
    await asyncio.sleep(0.12)
    assert job.is_network_quiet() is True
    assert job.state == "done"
    assert job.finished is True


async def test_finish_closes_tab_releases_lock_and_second_finish_404(manager: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """finish закрывает вкладку и снимает замок, повторно тела не отдаёт (json_responses пусто), второй finish -> 404."""
    cdp = FakeFullCdp()
    fake_dev = FakeDevToolsContext(cdp)
    monkeypatch.setattr(fetch_jobs, "_DevTools", lambda serial: fake_dev)
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=True))
    monkeypatch.setattr(browser, "_read_page", AsyncMock(return_value={
        "url": "https://example.com/",
        "title": "Example Title",
        "text": "Full text of page",
        "truncated": False,
        "captcha": False,
    }))

    job_id = await fetch_jobs.create_fetch_job("realme", "https://example.com/")
    job = fetch_jobs.get_active_job(job_id)
    assert job is not None

    lock = browser._get_lock("realme")
    assert lock.locked() is True

    result = await job.finish()
    assert result["url"] == "https://example.com/"
    assert result["title"] == "Example Title"
    assert result["text"] == "Full text of page"
    assert result["json_responses"] == []
    assert result["woke"] is True
    assert isinstance(result["seconds"], float)

    # Замок снят, вкладка закрыта
    assert lock.locked() is False
    assert "fake-tab-id" in fake_dev.closed_tabs

    # Второй finish вызывает ошибку 404
    assert fetch_jobs.get_active_job(job_id) is None
    with pytest.raises(KeyError, match="закрыто"):
        await job.finish()


async def test_delete_and_auto_cancel_release_lock_and_idempotent(manager: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """DELETE и автоотмена (CLIENT_SILENCE_SECONDS уменьшить до ~0.3 с) закрывают вкладку и снимают замок; второй DELETE не падает."""
    # 1. DELETE
    cdp1 = FakeFullCdp()
    fake_dev1 = FakeDevToolsContext(cdp1)
    monkeypatch.setattr(fetch_jobs, "_DevTools", lambda serial: fake_dev1)
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))

    job_id1 = await fetch_jobs.create_fetch_job("realme", "https://example.com/1")
    lock = browser._get_lock("realme")
    assert lock.locked() is True

    # Первый cancel_job возвращает True
    res1 = await fetch_jobs.cancel_job(job_id1)
    assert res1 is True
    assert lock.locked() is False
    assert "fake-tab-id" in fake_dev1.closed_tabs

    # Второй cancel_job идемпотентен и возвращает False
    res2 = await fetch_jobs.cancel_job(job_id1)
    assert res2 is False

    # Неизвестный job_id тоже возвращает False
    assert await fetch_jobs.cancel_job("unknown_job_id") is False

    # 2. Автоотмена
    monkeypatch.setattr(fetch_jobs, "CLIENT_SILENCE_SECONDS", 0.25)
    cdp2 = FakeFullCdp()
    fake_dev2 = FakeDevToolsContext(cdp2)
    monkeypatch.setattr(fetch_jobs, "_DevTools", lambda serial: fake_dev2)

    job_id2 = await fetch_jobs.create_fetch_job("realme", "https://example.com/2")
    assert lock.locked() is True

    # Ждём автоотмены по молчанию клиента > 0.25 с
    await asyncio.sleep(0.35)

    assert lock.locked() is False
    assert "fake-tab-id" in fake_dev2.closed_tabs
    assert fetch_jobs.get_active_job(job_id2) is None


async def test_max_seconds_transitions_to_done(manager: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """По max_seconds (уменьшенному) задание само переходит в состояние done."""
    monkeypatch.setattr(fetch_jobs, "MIN_MAX_SECONDS", 0.1)
    cdp = FakeFullCdp()
    fake_dev = FakeDevToolsContext(cdp)
    monkeypatch.setattr(fetch_jobs, "_DevTools", lambda serial: fake_dev)
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))

    job_id = await fetch_jobs.create_fetch_job("realme", "https://example.com/", max_seconds=0.2)
    job = fetch_jobs.get_active_job(job_id)
    assert job is not None
    assert job.state == "running"

    await asyncio.sleep(0.3)
    assert job.state == "done"
    assert job.finished is False

    # Вкладка и замок остаются открытыми в ожидании finish
    lock = browser._get_lock("realme")
    assert lock.locked() is True

    monkeypatch.setattr(browser, "_read_page", AsyncMock(return_value={
        "url": "https://example.com/",
        "title": "Title",
        "text": "Text",
        "truncated": False,
        "captcha": False,
    }))
    res = await job.finish()
    assert res["finished"] is False
    assert lock.locked() is False


async def test_parallel_second_job_busy_and_starts_after_delete(manager: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """Параллельное второе задание на тот же телефон получает отказ «занят» (429/BrowserBusy), после DELETE первого следующее стартует сразу."""
    cdp = FakeFullCdp()
    fake_dev = FakeDevToolsContext(cdp)
    monkeypatch.setattr(fetch_jobs, "_DevTools", lambda serial: fake_dev)
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))

    job_id1 = await fetch_jobs.create_fetch_job("realme", "https://example.com/1")

    # Второе задание на то же устройство получает BrowserBusy
    with pytest.raises(fetch_jobs.BrowserBusy, match="занят"):
        await fetch_jobs.create_fetch_job("realme", "https://example.com/2")

    # Отменяем первое
    await fetch_jobs.cancel_job(job_id1)

    # Следующее задание стартует сразу без ожидания
    job_id2 = await fetch_jobs.create_fetch_job("realme", "https://example.com/3")
    assert job_id2 != job_id1
    assert fetch_jobs.get_active_job(job_id2) is not None


async def test_activity_tracked_during_job_and_cleared_after(manager: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """Задание видно в activity.current(device_id) пока идёт, и пропадает после закрытия."""
    cdp = FakeFullCdp()
    fake_dev = FakeDevToolsContext(cdp)
    monkeypatch.setattr(fetch_jobs, "_DevTools", lambda serial: fake_dev)
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))

    job_id = await fetch_jobs.create_fetch_job("realme", "https://example.com/", actor="test-bot")
    cur = activity.current("realme")
    assert cur is not None
    assert cur["action"] == "fetch_job"
    assert cur["label"] == "читает сайт"
    assert cur["actor"] == "test-bot"

    await fetch_jobs.cancel_job(job_id)
    assert activity.current("realme") is None


async def test_job_failed_releases_lock_and_error_reported(manager: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """При падении соединения задание переходит в failed, снимает замок, GET отдаёт state=failed и error."""
    class BrokenCdp(FakeFullCdp):
        async def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
            if method == "Page.navigate":
                raise browser.BrowserUnavailable("Chrome оборвал соединение")
            return await super().call(method, params, timeout)

    cdp = BrokenCdp()
    fake_dev = FakeDevToolsContext(cdp)
    monkeypatch.setattr(fetch_jobs, "_DevTools", lambda serial: fake_dev)
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))

    job_id = await fetch_jobs.create_fetch_job("realme", "https://example.com/")
    job = fetch_jobs.get_active_job(job_id)
    assert job is not None

    await asyncio.sleep(0.05)
    assert job.state == "failed"
    assert "Chrome оборвал соединение" in str(job.error)

    # Замок снят автоматически
    lock = browser._get_lock("realme")
    assert lock.locked() is False

    # finish по failed заданию вызывает ошибку
    with pytest.raises(browser.BrowserError):
        await job.finish()


async def test_long_polling_wakes_up_early_on_new_data(manager: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """Длинный опрос завершается раньше 1 с, как только появляется новый ответ."""
    cdp = FakeFullCdp(body_map={"1": '{"ok": true}'})
    fake_dev = FakeDevToolsContext(cdp)
    monkeypatch.setattr(fetch_jobs, "_DevTools", lambda serial: fake_dev)
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))

    job_id = await fetch_jobs.create_fetch_job("realme", "https://example.com/")
    job = fetch_jobs.get_active_job(job_id)
    assert job is not None

    async def _feed_delayed():
        await asyncio.sleep(0.1)
        for ev in _request_events("1", "https://example.com/api/test"):
            cdp.events.put_nowait(ev)

    t0 = time.monotonic()
    asyncio.create_task(_feed_delayed())
    await job.wait_for_data(since=0, timeout=1.0)
    elapsed = time.monotonic() - t0

    # Проснулись раньше таймаута в 1 с (примерно за 0.1-0.2 с)
    assert elapsed < 0.6
    items, next_i = job.get_slice(since=0)
    assert len(items) == 1
    assert next_i == 1


async def test_create_fetch_job_cancelled_releases_lock_and_activity(manager: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """При отмене создания задания (asyncio.CancelledError) замок и активность освобождаются."""
    async def _cancelled_ensure_awake(*args: Any, **kwargs: Any) -> bool:
        raise asyncio.CancelledError()

    monkeypatch.setattr(actions, "ensure_awake", _cancelled_ensure_awake)
    lock = browser._get_lock("realme")
    assert lock.locked() is False

    with pytest.raises(asyncio.CancelledError):
        await fetch_jobs.create_fetch_job("realme", "https://example.com/")

    assert lock.locked() is False
    assert activity.current("realme") is None


async def test_failed_job_delayed_remove(manager: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """Упавшее задание удаляется из реестра памяти через delayed_remove."""
    class BrokenCdp(FakeFullCdp):
        async def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
            if method == "Page.navigate":
                raise browser.BrowserUnavailable("Chrome упал")
            return await super().call(method, params, timeout)

    cdp = BrokenCdp()
    fake_dev = FakeDevToolsContext(cdp)
    monkeypatch.setattr(fetch_jobs, "_DevTools", lambda serial: fake_dev)
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))

    job_id = await fetch_jobs.create_fetch_job("realme", "https://example.com/")
    job = fetch_jobs.get_active_job(job_id)
    assert job is not None
    await asyncio.sleep(0.05)
    assert job.state == "failed"
    assert job_id in fetch_jobs._jobs

    # Запускаем delayed_remove с нулевой задержкой
    await job._delayed_remove(0.0)
    assert job_id not in fetch_jobs._jobs


async def test_slow_body_keeps_running_and_quiet_only_after_body_stored(
    manager: DeviceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """«ответ пришёл быстро, а тело отдаётся медленно»: Network.getResponseBody в поддельном CDP

    отвечает через asyncio.sleep(1.0+) с крупным телом (>100 КБ); пока тело не получено,
    state остаётся running и network_quiet=False, после получения тела ответ появляется в GET
    и только потом наступает done/quiet.
    """
    monkeypatch.setattr(fetch_jobs, "QUIET_SECONDS", 0.1)

    class SlowBodyCdp(FakeFullCdp):
        async def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
            if method == "Network.getResponseBody":
                await asyncio.sleep(1.05)
                return {"body": "x" * 120_000, "base64Encoded": False}
            return await super().call(method, params, timeout)

    cdp = SlowBodyCdp()
    fake_dev = FakeDevToolsContext(cdp)
    monkeypatch.setattr(fetch_jobs, "_DevTools", lambda serial: fake_dev)
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))

    job_id = await fetch_jobs.create_fetch_job(
        "realme", "https://example.com/", capture_json=["/api/search"], max_seconds=10.0
    )
    job = fetch_jobs.get_active_job(job_id)
    assert job is not None

    for ev in _request_events("1", "https://example.com/api/search"):
        cdp.events.put_nowait(ev)

    # Даём время на обработку loadingFinished
    await asyncio.sleep(0.05)

    # Проверяем в середине загрузки тела:
    # Тело ещё в пути (sleep 1.05 с), поэтому state=running, network_quiet=False
    await asyncio.sleep(0.25)
    assert job.state == "running"
    assert job.is_network_quiet() is False
    items_mid, next_mid = job.get_slice(since=0)
    assert items_mid == []
    assert next_mid == 0

    # Ждём завершения загрузки тела
    await job.wait_for_data(since=0, timeout=1.5)

    # Тело получено и появилось в GET
    items, next_i = job.get_slice(since=0)
    assert len(items) == 1
    assert len(items[0]["body"]) == 120_000
    assert next_i == 1

    # Сразу после получения тела тишина ещё не наступила (QUIET_SECONDS=0.1 с)
    # state остаётся running и network_quiet=False
    assert job.state == "running"
    assert job.is_network_quiet() is False

    # Только после истечения QUIET_SECONDS наступает quiet и done
    await asyncio.sleep(0.15)
    assert job.is_network_quiet() is True
    assert job.state == "done"
    assert job.finished is True


async def test_finish_waits_for_inflight_body_and_returns_unserved(
    manager: DeviceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """finish, вызванный ПОКА тело в пути, дожидается его и возвращает в json_responses

    (ещё не выданный клиенту ответ), а второй такой ответ, уже выданный через GET,
    в finish не повторяется.
    """
    monkeypatch.setattr(fetch_jobs, "QUIET_SECONDS", 0.1)

    class MultiBodyCdp(FakeFullCdp):
        async def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
            if method == "Network.getResponseBody":
                rid = (params or {}).get("requestId", "")
                if rid == "1":
                    return {"body": '{"first": 1}', "base64Encoded": False}
                if rid == "2":
                    await asyncio.sleep(0.3)
                    return {"body": '{"second": 2}', "base64Encoded": False}
            if method == "Runtime.evaluate":
                return {
                    "result": {
                        "value": json.dumps({
                            "url": "https://example.com/",
                            "title": "Example",
                            "text": "Example text",
                        })
                    }
                }
            return await super().call(method, params, timeout)

    cdp = MultiBodyCdp()
    fake_dev = FakeDevToolsContext(cdp)
    monkeypatch.setattr(fetch_jobs, "_DevTools", lambda serial: fake_dev)
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))

    job_id = await fetch_jobs.create_fetch_job(
        "realme", "https://example.com/", capture_json=["/api/"]
    )
    job = fetch_jobs.get_active_job(job_id)
    assert job is not None

    # Подаем первый ответ, он отдается быстро
    for ev in _request_events("1", "https://example.com/api/1"):
        cdp.events.put_nowait(ev)
    await asyncio.sleep(0.05)

    # Клиент получает первый ответ через GET
    items1, next1 = job.get_slice(since=0)
    assert len(items1) == 1
    assert items1[0]["body"] == '{"first": 1}'
    assert next1 == 1

    # Подаем второй ответ с медленным получением тела (0.3 с)
    for ev in _request_events("2", "https://example.com/api/2"):
        cdp.events.put_nowait(ev)
    await asyncio.sleep(0.05)

    # В этот момент тело 2 ещё в пути
    assert job._has_inflight_bodies() is True

    # Клиент зовёт finish ПОКА тело 2 в пути
    res = await job.finish()

    # finish дождался тела 2 и вернул его в json_responses
    json_res = res["json_responses"]
    assert len(json_res) == 1
    assert json_res[0]["url"] == "https://example.com/api/2"
    assert json_res[0]["body"] == '{"second": 2}'
    assert json_res[0]["status"] == 200
    assert isinstance(json_res[0]["t"], float)

    # Первый ответ, уже выданный через GET, в finish не повторяется!
    assert all(r["body"] != '{"first": 1}' for r in json_res)

    # Вкладка закрыта, замок снят
    lock = browser._get_lock("realme")
    assert lock.locked() is False
    assert "fake-tab-id" in fake_dev.closed_tabs


async def test_finish_body_wait_timeout_does_not_hang(
    manager: DeviceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """При превышении FINISH_BODY_WAIT_SECONDS finish не зависает, а возвращает что есть,

    вкладка закрыта, замок снят.
    """
    monkeypatch.setattr(fetch_jobs, "FINISH_BODY_WAIT_SECONDS", 0.1)

    class HangingBodyCdp(FakeFullCdp):
        async def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
            if method == "Network.getResponseBody":
                await asyncio.sleep(5.0)  # Дольше FINISH_BODY_WAIT_SECONDS (0.1 с)
                return {"body": '{"slow": true}', "base64Encoded": False}
            if method == "Runtime.evaluate":
                return {
                    "result": {
                        "value": json.dumps({
                            "url": "https://example.com/",
                            "title": "Example",
                            "text": "Page text after timeout",
                        })
                    }
                }
            return await super().call(method, params, timeout)

    cdp = HangingBodyCdp()
    fake_dev = FakeDevToolsContext(cdp)
    monkeypatch.setattr(fetch_jobs, "_DevTools", lambda serial: fake_dev)
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))

    job_id = await fetch_jobs.create_fetch_job(
        "realme", "https://example.com/", capture_json=["/api/slow"]
    )
    job = fetch_jobs.get_active_job(job_id)
    assert job is not None

    for ev in _request_events("1", "https://example.com/api/slow"):
        cdp.events.put_nowait(ev)
    await asyncio.sleep(0.05)

    # finish вызывается, когда тело висит
    t0 = time.monotonic()
    res = await job.finish()
    elapsed = time.monotonic() - t0

    # finish не завис на 5 секунд, а завершился около 0.1 с
    assert elapsed < 0.6
    assert res["text"] == "Page text after timeout"
    assert res["json_responses"] == []

    # Вкладка закрыта, замок снят
    lock = browser._get_lock("realme")
    assert lock.locked() is False
    assert "fake-tab-id" in fake_dev.closed_tabs


async def test_max_seconds_done_even_if_body_in_flight(
    manager: DeviceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """по max_seconds задание переходит в done, даже если тело ещё не пришло."""
    monkeypatch.setattr(fetch_jobs, "MIN_MAX_SECONDS", 0.1)
    monkeypatch.setattr(fetch_jobs, "QUIET_SECONDS", 1.0)

    class HangingBodyCdp(FakeFullCdp):
        async def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
            if method == "Network.getResponseBody":
                await asyncio.sleep(3.0)
                return {"body": '{"slow": true}', "base64Encoded": False}
            return await super().call(method, params, timeout)

    cdp = HangingBodyCdp()
    fake_dev = FakeDevToolsContext(cdp)
    monkeypatch.setattr(fetch_jobs, "_DevTools", lambda serial: fake_dev)
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))

    job_id = await fetch_jobs.create_fetch_job(
        "realme", "https://example.com/", capture_json=["/api/slow"], max_seconds=0.2
    )
    job = fetch_jobs.get_active_job(job_id)
    assert job is not None

    for ev in _request_events("1", "https://example.com/api/slow"):
        cdp.events.put_nowait(ev)
    await asyncio.sleep(0.05)

    assert job.state == "running"
    assert job.is_network_quiet() is False

    # Ждём истечения max_seconds (0.2 с)
    await asyncio.sleep(0.25)
    assert job.state == "done"
    # Тело всё ещё не пришло (sleep 3.0 c)
    assert job._has_inflight_bodies() is True

    # Замок всё ещё удерживается до вызова finish или cancel
    lock = browser._get_lock("realme")
    assert lock.locked() is True

    await job.cancel()
    assert lock.locked() is False


async def test_fetch_body_warning_logged_on_none_or_exception(
    manager: DeviceManager, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """При None или исключении в _fetch_body_and_store пишется logger.warning с job_id и url."""
    class BrokenBodyCdp(FakeFullCdp):
        async def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
            if method == "Network.getResponseBody":
                raise browser.BrowserError("Тело не найдено в кеше Chrome")
            return await super().call(method, params, timeout)

    cdp = BrokenBodyCdp()
    fake_dev = FakeDevToolsContext(cdp)
    monkeypatch.setattr(fetch_jobs, "_DevTools", lambda serial: fake_dev)
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))

    with caplog.at_level(logging.WARNING, logger="hub.devices.fetch_jobs"):
        job_id = await fetch_jobs.create_fetch_job(
            "realme", "https://example.com/", capture_json=["/api/broken"]
        )
        job = fetch_jobs.get_active_job(job_id)
        assert job is not None

        for ev in _request_events("1", "https://example.com/api/broken"):
            cdp.events.put_nowait(ev)
        await asyncio.sleep(0.1)

        # Проверяем, что в логе появилось предупреждение с job_id и url
        assert any(
            job_id in record.message and "https://example.com/api/broken" in record.message
            for record in caplog.records
        )


# --- Тесты для replay_post в fetch_jobs ---

class ReplayFetchJobCdp(FakeFullCdp):
    def __init__(
        self,
        events: list[dict] | None = None,
        body_map: dict[str, str] | None = None,
        evaluate_response: dict | None = None,
        evaluate_delay: float = 0.0,
        evaluate_exception: Exception | None = None,
        post_data_map: dict[str, str] | None = None,
    ) -> None:
        super().__init__(events, body_map)
        self.evaluate_response = evaluate_response
        self.evaluate_delay = evaluate_delay
        self.evaluate_exception = evaluate_exception
        self.post_data_map = post_data_map or {}
        self.evaluate_calls: list[dict] = []

    async def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        params = params or {}
        if method == "Network.getRequestPostData":
            rid = params.get("requestId", "")
            return {"postData": self.post_data_map.get(rid, '{"limit": 10}')}
        if method == "Runtime.evaluate":
            expr = params.get("expression", "")
            if "fetch(" in expr:
                self.evaluate_calls.append(params)
                if self.evaluate_delay > 0:
                    await asyncio.sleep(self.evaluate_delay)
                if self.evaluate_exception is not None:
                    raise self.evaluate_exception
                if self.evaluate_response is not None:
                    return self.evaluate_response
                return {"result": {"value": {"ok": True, "status": 200, "body": '{"offers": [1, 2, 3]}'}}}
        return await super().call(method, params, timeout)


def _post_events(
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


async def test_fetch_jobs_replay_post_flow_and_streaming(manager: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """В задании ответ попадает в responses со сквозным i, стримится без потерь/дублей, учитывается в inflight/quiet."""
    cdp = ReplayFetchJobCdp(evaluate_delay=0.08)
    fake_dev = FakeDevToolsContext(cdp)
    monkeypatch.setattr(fetch_jobs, "_DevTools", lambda serial: fake_dev)
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))

    job_id = await fetch_jobs.create_fetch_job(
        "realme",
        "https://example.com/search",
        capture_json=["/api/offers"],
        replay_post={"url_contains": "/api/offers", "set": {"limit": 100, "filter": "direct"}},
    )
    job = fetch_jobs.get_active_job(job_id)
    assert job is not None

    for ev in _post_events("1", "https://example.com/api/offers", post_data='{"limit": 10, "offset": 0}'):
        cdp.events.put_nowait(ev)

    # Пока повтор выполняется (evaluate_delay 0.08), _has_inflight_bodies() возвращает True
    await asyncio.sleep(0.02)
    assert job._has_inflight_bodies() is True
    assert job.is_network_quiet() is False

    # Дожидаемся завершения исходного запроса и повтора
    await asyncio.sleep(0.12)
    assert job._has_inflight_bodies() is False

    # Проверяем, что в fetch ушло тело с перезаписанными полями
    assert len(cdp.evaluate_calls) == 1
    expr = cdp.evaluate_calls[0]["expression"]
    assert '"limit": 100' in expr
    assert '"filter": "direct"' in expr
    assert '"offset": 0' in expr

    # Проверяем responses: начальный i=0 и повторный i=1 с replayed=True
    assert len(job.responses) == 2
    r0 = job.responses[0]
    r1 = job.responses[1]
    assert r0["i"] == 0
    assert r0["url"] == "https://example.com/api/offers"
    assert "replayed" not in r0

    assert r1["i"] == 1
    assert r1["url"] == "https://example.com/api/offers"
    assert r1["replayed"] is True
    assert r1["body"] == '{"offers": [1, 2, 3]}'
    assert r1["t"] >= r0["t"]
    assert job.t_first_match == r0["t"]

    # Проверяем стриминг через get_slice: since=0 отдаёт оба, since=next отдаёт пусто
    items0, next0 = job.get_slice(since=0)
    assert len(items0) == 2
    assert next0 == 2

    items1, next1 = job.get_slice(since=next0)
    assert len(items1) == 0
    assert next1 == 2

    # Завершаем: unserved пуст, так как всё выдано
    fin = await job.finish()
    assert fin["json_responses"] == []
    assert fin["t_first_match"] == r0["t"]


async def test_fetch_jobs_replay_post_finish_unserved(manager: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """Если finish вызван без предварительного get_slice, replayed: True возвращается в json_responses."""
    cdp = ReplayFetchJobCdp()
    fake_dev = FakeDevToolsContext(cdp)
    monkeypatch.setattr(fetch_jobs, "_DevTools", lambda serial: fake_dev)
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))

    job_id = await fetch_jobs.create_fetch_job(
        "realme",
        "https://example.com/search",
        capture_json=["/api/offers"],
        replay_post={"url_contains": "/api/offers", "set": {"limit": 100}},
    )
    job = fetch_jobs.get_active_job(job_id)
    assert job is not None

    for ev in _post_events("1", "https://example.com/api/offers", post_data='{"limit": 10}'):
        cdp.events.put_nowait(ev)
    await asyncio.sleep(0.08)

    fin = await job.finish()
    assert len(fin["json_responses"]) == 2
    replayed = [r for r in fin["json_responses"] if r.get("replayed") is True]
    assert len(replayed) == 1
    assert replayed[0]["body"] == '{"offers": [1, 2, 3]}'


async def test_fetch_jobs_replay_post_not_intercepted_silently_skipped(manager: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """Если подходящий POST-запрос не пришёл, повтор пропускается без ошибки."""
    cdp = ReplayFetchJobCdp()
    fake_dev = FakeDevToolsContext(cdp)
    monkeypatch.setattr(fetch_jobs, "_DevTools", lambda serial: fake_dev)
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))

    job_id = await fetch_jobs.create_fetch_job(
        "realme",
        "https://example.com/",
        replay_post={"url_contains": "/api/offers", "set": {"limit": 100}},
    )
    job = fetch_jobs.get_active_job(job_id)
    assert job is not None

    for ev in _request_events("1", "https://example.com/api/other"):
        cdp.events.put_nowait(ev)
    await asyncio.sleep(0.05)

    assert len(cdp.evaluate_calls) == 0
    assert len(job.responses) == 1
    assert "replayed" not in job.responses[0]


async def test_fetch_jobs_replay_post_body_not_json_dict_silently_skipped(manager: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """Если тело перехваченного запроса не является JSON-словарём, повтор пропускается."""
    cdp = ReplayFetchJobCdp()
    fake_dev = FakeDevToolsContext(cdp)
    monkeypatch.setattr(fetch_jobs, "_DevTools", lambda serial: fake_dev)
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))

    job_id = await fetch_jobs.create_fetch_job(
        "realme",
        "https://example.com/",
        replay_post={"url_contains": "/api/offers", "set": {"limit": 100}},
    )
    job = fetch_jobs.get_active_job(job_id)
    assert job is not None

    for ev in _post_events("1", "https://example.com/api/offers", post_data="not json string"):
        cdp.events.put_nowait(ev)
    await asyncio.sleep(0.05)

    assert len(cdp.evaluate_calls) == 0
    assert len(job.responses) == 1


async def test_fetch_jobs_replay_post_fetch_error_silently_skipped(manager: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """Если fetch возвращает ошибку, повтор пропускается."""
    cdp = ReplayFetchJobCdp(evaluate_response={"result": {"value": {"ok": False, "status": 500, "body": "error"}}})
    fake_dev = FakeDevToolsContext(cdp)
    monkeypatch.setattr(fetch_jobs, "_DevTools", lambda serial: fake_dev)
    monkeypatch.setattr(actions, "ensure_awake", AsyncMock(return_value=False))

    job_id = await fetch_jobs.create_fetch_job(
        "realme",
        "https://example.com/",
        replay_post={"url_contains": "/api/offers", "set": {"limit": 100}},
    )
    job = fetch_jobs.get_active_job(job_id)
    assert job is not None

    for ev in _post_events("1", "https://example.com/api/offers", post_data='{"limit": 10}'):
        cdp.events.put_nowait(ev)
    await asyncio.sleep(0.05)

    assert len(job.responses) == 1
    assert "replayed" not in job.responses[0]


async def test_fetch_jobs_replay_post_validation(manager: DeviceManager) -> None:
    """Валидация replay_post в create_fetch_job: url_contains непустая строка, set словарь."""
    with pytest.raises(ValueError, match="url_contains"):
        await fetch_jobs.create_fetch_job("realme", "https://example.com", replay_post={"url_contains": "", "set": {}})

    with pytest.raises(ValueError, match="set"):
        await fetch_jobs.create_fetch_job("realme", "https://example.com", replay_post={"url_contains": "/api", "set": []})

    with pytest.raises(ValueError, match="словарём"):
        await fetch_jobs.create_fetch_job("realme", "https://example.com", replay_post=123)
