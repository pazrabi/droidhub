"""Чтение страниц в Chrome телефона через DevTools (CDP): текст, JSON-ответы сайта, вкладки.

Хаб пробрасывает сокет chrome_devtools_remote через adb forward на локальный порт сервера,
открывает отдельную вкладку, читает данные и закрывает её. Только чтение: произвольный JS наружу не отдаём.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import httpx
import websockets

from hub.devices import actions
from hub.devices.adb import AdbError, adb_shell, run_adb

CHROME_PACKAGE = "com.android.chrome"
CHROME_ACTIVITY = f"{CHROME_PACKAGE}/com.google.android.apps.chrome.Main"
DEVTOOLS_SOCKET = "localabstract:chrome_devtools_remote"

MAX_WAIT_SECONDS = 60.0
QUIET_SECONDS = 1.5  # столько сети должно молчать после загрузки, чтобы считать выдачу догруженной
DEFAULT_MAX_CHARS = 200_000
MAX_JSON_RESPONSES = 50
MAX_JSON_BODY = 2_000_000
MAX_JSON_TOTAL = 8_000_000
LOCK_WAIT_SECONDS = 90.0

_SKIP_TYPES = {"Image", "Font", "Stylesheet", "Media", "Ping", "Other"}

_CAPTCHA_RE = re.compile(
    r"captcha|капча|капчу|я не робот|вы не робот|не робот|are you (a )?robot|verify you are (a )?human"
    r"|unusual traffic|необычный трафик|подозрительн\w+ активност|just a moment|checking your browser",
    re.IGNORECASE,
)


class BrowserError(Exception):
    """Базовая ошибка работы с браузером телефона (текст на русском для пользователя)."""


class BrowserUnavailable(BrowserError):
    """Chrome не отвечает по DevTools."""


class BrowserBusy(BrowserError):
    """С этим телефоном уже работает другой запрос и очередь не дождалась."""


_locks: dict[str, asyncio.Lock] = {}


def _get_lock(device_id: str) -> asyncio.Lock:
    if device_id not in _locks:
        _locks[device_id] = asyncio.Lock()
    return _locks[device_id]


def detect_captcha(url: str, title: str, text: str) -> bool:
    """Похоже ли, что вместо страницы показана капча или проверка на робота."""
    sample = f"{url}\n{title}\n{text[:3000]}"
    return bool(_CAPTCHA_RE.search(sample))


class _Cdp:
    """Минимальный клиент CDP по websocket: запросы по id и события."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
                else:
                    self.events.put_nowait(msg)
        except Exception:
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(BrowserUnavailable("Связь с Chrome на телефоне оборвалась"))

    async def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        self._next_id += 1
        mid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        msg = await asyncio.wait_for(fut, timeout)
        if "error" in msg:
            raise BrowserError(f"Chrome вернул ошибку на {method}: {msg['error'].get('message', msg['error'])}")
        return msg.get("result", {})

    async def close(self) -> None:
        self._reader.cancel()
        try:
            await self._ws.close()
        except Exception:
            pass


class _DevTools:
    """Проброшенный порт DevTools одного телефона: HTTP-список вкладок и websocket к вкладке."""

    def __init__(self, serial: str) -> None:
        self.serial = serial
        self.port = 0
        self._http = httpx.AsyncClient(timeout=10.0)

    async def __aenter__(self) -> "_DevTools":
        out = await run_adb(["forward", "tcp:0", DEVTOOLS_SOCKET], serial=self.serial)
        try:
            self.port = int(str(out).strip())
        except ValueError:
            raise BrowserUnavailable("Не удалось пробросить порт DevTools с телефона")
        await self._wait_ready()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._http.aclose()
        if self.port:
            try:
                await asyncio.shield(run_adb(["forward", "--remove", f"tcp:{self.port}"], serial=self.serial))
            except BaseException:
                pass

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _wait_ready(self) -> None:
        """Ждёт ответа DevTools; если Chrome не запущен, запускает его."""
        launched = False
        for _ in range(12):
            try:
                r = await self._http.get(f"{self.base}/json/version")
                if r.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if not launched:
                launched = True
                try:
                    await adb_shell(self.serial, f"am start -n {CHROME_ACTIVITY}")
                except AdbError:
                    pass
            await asyncio.sleep(0.5)
        raise BrowserUnavailable("Chrome на телефоне не отвечает: открой Chrome на телефоне и повтори")

    async def tabs(self) -> list[dict]:
        r = await self._http.get(f"{self.base}/json/list")
        return [t for t in r.json() if t.get("type") == "page"]

    async def new_tab(self, url: str = "about:blank") -> dict:
        """Новая вкладка через браузерный сокет (HTTP /json/new на Android Chrome не работает)."""
        ws = await websockets.connect(f"ws://127.0.0.1:{self.port}/devtools/browser", max_size=1_000_000)
        cdp = _Cdp(ws)
        try:
            res = await cdp.call("Target.createTarget", {"url": url})
        finally:
            await cdp.close()
        return {"id": res["targetId"]}

    async def close_tab(self, tab_id: str) -> None:
        try:
            await self._http.get(f"{self.base}/json/close/{tab_id}")
        except httpx.HTTPError:
            pass

    async def connect(self, tab: dict) -> _Cdp:
        path = f"page/{tab['id']}"
        ws = await websockets.connect(f"ws://127.0.0.1:{self.port}/devtools/{path}", max_size=64_000_000)
        return _Cdp(ws)


_PAGE_JS = "JSON.stringify({title: document.title, url: location.href, text: document.body ? document.body.innerText : ''})"


async def _read_page(cdp: _Cdp, max_chars: int) -> dict[str, Any]:
    res = await cdp.call("Runtime.evaluate", {"expression": _PAGE_JS, "returnByValue": True})
    data = json.loads(res["result"]["value"])
    text = data["text"]
    truncated = len(text) > max_chars
    return {
        "url": data["url"],
        "title": data["title"],
        "text": text[:max_chars],
        "truncated": truncated,
        "captcha": detect_captcha(data["url"], data["title"], text),
    }


async def _wait_for_page(cdp: _Cdp, wait_seconds: float, captured: dict[str, dict]) -> bool:
    """Ждёт загрузки и затишья сети; True если дождались, False если вышло время."""
    deadline = time.monotonic() + wait_seconds
    inflight: set[str] = set()
    load_fired = False
    last_activity = time.monotonic()
    while True:
        now = time.monotonic()
        if load_fired and not inflight and now - last_activity >= QUIET_SECONDS:
            return True
        if now >= deadline:
            return False
        try:
            ev = await asyncio.wait_for(cdp.events.get(), timeout=min(0.25, max(0.01, deadline - now)))
        except asyncio.TimeoutError:
            continue
        method, params = ev.get("method"), ev.get("params", {})
        if method == "Page.loadEventFired":
            load_fired = True
            last_activity = time.monotonic()
        elif method == "Network.requestWillBeSent":
            inflight.add(params["requestId"])
            last_activity = time.monotonic()
        elif method in ("Network.loadingFinished", "Network.loadingFailed"):
            inflight.discard(params["requestId"])
            last_activity = time.monotonic()
            if method == "Network.loadingFailed" and params["requestId"] in captured:
                captured[params["requestId"]]["failed"] = True
        elif method == "Network.responseReceived":
            resp = params["response"]
            captured[params["requestId"]] = {
                "url": resp.get("url", ""),
                "status": resp.get("status"),
                "mime": resp.get("mimeType", ""),
                "type": params.get("type", ""),
            }


def _wanted_response(info: dict, patterns: list[str]) -> bool:
    if info.get("failed") or info["type"] in _SKIP_TYPES:
        return False
    if patterns:
        return any(p in info["url"] for p in patterns)
    return "json" in info["mime"].lower()


async def _collect_json(cdp: _Cdp, captured: dict[str, dict], patterns: list[str]) -> list[dict]:
    out: list[dict] = []
    total = 0
    for request_id, info in captured.items():
        if len(out) >= MAX_JSON_RESPONSES or total >= MAX_JSON_TOTAL:
            break
        if not _wanted_response(info, patterns):
            continue
        try:
            res = await cdp.call("Network.getResponseBody", {"requestId": request_id}, timeout=10.0)
        except (BrowserError, asyncio.TimeoutError):
            continue
        body = res.get("body", "")
        if res.get("base64Encoded") or len(body) > MAX_JSON_BODY:
            continue
        total += len(body)
        out.append({"url": info["url"], "status": info["status"], "body": body})
    return out


async def _locked(device_id: str, fn: Any) -> Any:
    lock = _get_lock(device_id)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=LOCK_WAIT_SECONDS)
    except asyncio.TimeoutError:
        raise BrowserBusy("С этим телефоном сейчас работает другой запрос к браузеру, повтори позже")
    try:
        return await fn()
    finally:
        lock.release()


async def fetch_page(
    device_id: str,
    url: str,
    wait_seconds: float = 15.0,
    capture_json: list[str] | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    *,
    actor: str = "system",
) -> dict[str, Any]:
    """Открывает url в новой вкладке Chrome, ждёт догрузки и возвращает текст страницы и JSON-ответы сайта.

    capture_json: подстроки адресов, ответы которых нужно вернуть (пусто: все JSON-ответы XHR/fetch).
    """
    if not re.match(r"^https?://", url, re.IGNORECASE):
        raise ValueError("Нужен адрес, начинающийся с http:// или https://")
    wait_seconds = max(1.0, min(float(wait_seconds), MAX_WAIT_SECONDS))
    max_chars = max(1000, min(int(max_chars), 1_000_000))
    patterns = [p for p in (capture_json or []) if p]
    params = {"url": url, "wait_seconds": wait_seconds, "capture_json": patterns}

    async def _run() -> dict[str, Any]:
        await actions.ensure_awake(device_id, actor=actor)
        serial = actions._get_serial(device_id)
        started = time.monotonic()
        async with _DevTools(serial) as dev:
            tab = await dev.new_tab("about:blank")
            cdp = None
            try:
                cdp = await dev.connect(tab)
                await cdp.call("Page.enable")
                await cdp.call("Network.enable")
                await cdp.call("Page.navigate", {"url": url}, timeout=30.0)
                captured: dict[str, dict] = {}
                finished = await _wait_for_page(cdp, wait_seconds, captured)
                result = await _read_page(cdp, max_chars)
                if result["url"].startswith("chrome-error://"):
                    code = re.search(r"\b[A-Z][A-Z0-9_]{6,}\b", result["text"])
                    raise BrowserError(
                        f"Страница не открылась на телефоне ({code.group(0) if code else 'ошибка сети'}): "
                        "проверь интернет на телефоне и адрес"
                    )
                result["json_responses"] = await _collect_json(cdp, captured, patterns)
                result["finished"] = finished
                result["seconds"] = round(time.monotonic() - started, 1)
                return result
            finally:
                if cdp is not None:
                    await cdp.close()
                await dev.close_tab(tab["id"])

    return await actions._execute_action(
        device_id, "fetch_page", params, lambda: _locked(device_id, _run), actor=actor
    )


async def browser_tabs(device_id: str, *, actor: str = "system") -> list[dict[str, Any]]:
    """Список открытых вкладок Chrome: индекс, заголовок, адрес."""

    async def _run() -> list[dict[str, Any]]:
        serial = actions._get_serial(device_id)
        async with _DevTools(serial) as dev:
            return [
                {"index": i, "title": t.get("title", ""), "url": t.get("url", "")}
                for i, t in enumerate(await dev.tabs())
            ]

    return await actions._execute_action(
        device_id, "browser_tabs", {}, lambda: _locked(device_id, _run), actor=actor
    )


async def page_text(
    device_id: str, tab_index: int = 0, max_chars: int = DEFAULT_MAX_CHARS, *, actor: str = "system"
) -> dict[str, Any]:
    """Текст всей страницы уже открытой вкладки Chrome (по индексу из browser_tabs)."""
    max_chars = max(1000, min(int(max_chars), 1_000_000))

    async def _run() -> dict[str, Any]:
        serial = actions._get_serial(device_id)
        async with _DevTools(serial) as dev:
            tabs = await dev.tabs()
            if not 0 <= tab_index < len(tabs):
                raise ValueError(f"Вкладки с номером {tab_index} нет, открыто вкладок: {len(tabs)}")
            cdp = await dev.connect(tabs[tab_index])
            try:
                return await _read_page(cdp, max_chars)
            finally:
                await cdp.close()

    return await actions._execute_action(
        device_id, "page_text", {"tab_index": tab_index}, lambda: _locked(device_id, _run), actor=actor
    )
