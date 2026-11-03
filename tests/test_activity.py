from __future__ import annotations

import asyncio

from hub.devices import activity


async def test_free_by_default() -> None:
    assert activity.current("nobody") is None


async def test_busy_during_action_and_free_after() -> None:
    async with activity.track("dev1", "fetch_page", "pool"):
        cur = activity.current("dev1")
        assert cur["label"] == "читает сайт"
        assert cur["actor"] == "pool"
    assert activity.current("dev1") is None


async def test_busy_cleared_on_error() -> None:
    try:
        async with activity.track("dev2", "tap", "mcp"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert activity.current("dev2") is None


async def test_oldest_operation_is_reported_for_parallel_actions() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def first() -> None:
        async with activity.track("dev3", "fetch_page", "bot"):
            started.set()
            await release.wait()

    t = asyncio.create_task(first())
    await started.wait()
    async with activity.track("dev3", "tap", "mcp"):
        assert activity.current("dev3")["action"] == "fetch_page"
    assert activity.current("dev3")["action"] == "fetch_page"
    release.set()
    await t
    assert activity.current("dev3") is None
