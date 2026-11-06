from __future__ import annotations

import asyncio
import json
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
