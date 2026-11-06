"""Чтение страниц в Chrome телефона через DevTools (CDP): текст, JSON-ответы сайта, вкладки.

Хаб пробрасывает сокет chrome_devtools_remote через adb forward на локальный порт сервера,
открывает отдельную вкладку, читает данные и закрывает её. Только чтение: произвольный JS наружу не отдаём.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx
import websockets

from hub.devices import actions
from hub.devices.adb import AdbError, adb_shell, run_adb

logger = logging.getLogger(__name__)

CHROME_PACKAGE = "com.android.chrome"
CHROME_ACTIVITY = f"{CHROME_PACKAGE}/com.google.android.apps.chrome.Main"
DEVTOOLS_SOCKET = "localabstract:chrome_devtools_remote"

MAX_WAIT_SECONDS = 60.0
QUIET_SECONDS = 1.5  # столько сети должно молчать после загрузки, чтобы считать выдачу догруженной
DEFAULT_MAX_CHARS = 200_000
MAX_JSON_RESPONSES = 200
MAX_JSON_BODY = 2_000_000
MAX_JSON_TOTAL = 30_000_000
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
        try:
            msg = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise BrowserError(f"Chrome на телефоне не ответил на {method} за {timeout:.0f} с")
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


def _wanted_response(info: dict, patterns: list[str]) -> bool:
    if info.get("failed") or info["type"] in _SKIP_TYPES:
        return False
    if patterns:
        return any(p in info["url"] for p in patterns)
    return "json" in info["mime"].lower()


async def _fetch_body(cdp: _Cdp, request_id: str, info: dict) -> dict | None:
    try:
        res = await cdp.call("Network.getResponseBody", {"requestId": request_id}, timeout=15.0)
    except (BrowserError, asyncio.TimeoutError):
        return None
    body = res.get("body", "")
    if res.get("base64Encoded") or len(body) > MAX_JSON_BODY:
        return None
    return {"url": info["url"], "status": info["status"], "body": body}


_SERVICE_HEADERS = {
    "host",
    "content-length",
    "cookie",
    "cookie2",
    "connection",
    "keep-alive",
    "origin",
    "referer",
    "user-agent",
    "accept-encoding",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "sec-fetch-user",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "te",
    "trailer",
    "upgrade",
    "via",
}


def _validate_replay_post(replay_post: Any) -> None:
    if replay_post is None:
        return
    if not isinstance(replay_post, dict):
        raise ValueError("replay_post должен быть словарём")
    url_contains = replay_post.get("url_contains")
    if not isinstance(url_contains, str) or not url_contains:
        raise ValueError("replay_post.url_contains должен быть непустой строкой")
    set_dict = replay_post.get("set")
    if not isinstance(set_dict, dict):
        raise ValueError("replay_post.set должен быть словарём")


async def _do_replay(
    cdp: Any,
    rid: str,
    req: dict[str, Any],
    replay_post: dict[str, Any],
    started: float,
) -> dict[str, Any] | None:
    post_data = req.get("postData")
    if post_data is None:
        try:
            res = await cdp.call("Network.getRequestPostData", {"requestId": rid}, timeout=15.0)
            post_data = res.get("postData")
        except Exception as exc:
            logger.warning("Повтор POST пропущен: не удалось получить postData (%s)", type(exc).__name__)
            return None

    if post_data is None:
        logger.warning("Повтор POST пропущен: отсутствует postData")
        return None

    try:
        orig_body = json.loads(post_data)
    except Exception:
        logger.warning("Повтор POST пропущен: тело запроса не является валидным JSON")
        return None

    if not isinstance(orig_body, dict):
        logger.warning("Повтор POST пропущен: тело запроса не является JSON-словарём")
        return None

    new_body = dict(orig_body)
    new_body.update(replay_post["set"])

    orig_url = req.get("url", "")
    orig_headers = req.get("headers", {})

    headers = {
        k: v for k, v in orig_headers.items()
        if not k.startswith(":") and k.lower() not in _SERVICE_HEADERS
    }

    js_code = (
        "fetch(" + json.dumps(orig_url) + ", {"
        "method: 'POST', "
        "headers: " + json.dumps(headers) + ", "
        "credentials: 'include', "
        "body: JSON.stringify(" + json.dumps(new_body) + ")"
        "}).then(async (r) => ({"
        "ok: r.ok, "
        "status: r.status, "
        "body: await r.text()"
        "})).catch((e) => ({"
        "ok: false, "
        "error: String(e)"
        "}))"
    )

    try:
        res = await cdp.call(
            "Runtime.evaluate",
            {"expression": js_code, "awaitPromise": True, "returnByValue": True},
            timeout=15.0,
        )
    except Exception as exc:
        logger.warning("Повтор POST пропущен: ошибка Runtime.evaluate (%s)", type(exc).__name__)
        return None

    val = res.get("result", {}).get("value") if isinstance(res.get("result"), dict) else res.get("value")
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except Exception:
            pass

    if not isinstance(val, dict):
        logger.warning("Повтор POST пропущен: некорректный ответ от Runtime.evaluate")
        return None

    if not val.get("ok"):
        logger.warning("Повтор POST пропущен: fetch завершился ошибкой (%s)", val.get("error", "not ok"))
        return None

    status = val.get("status", 200)
    if not (isinstance(status, int) and 200 <= status < 300):
        logger.warning("Повтор POST пропущен: статус ответа %s не 2xx", status)
        return None

    body = val.get("body", "")
    if not isinstance(body, str):
        body = str(body)

    if len(body) > MAX_JSON_BODY:
        body = body[:MAX_JSON_BODY]

    finish_time = time.monotonic()
    t = round(max(0.0, finish_time - started), 2)

    return {
        "url": orig_url,
        "status": status,
        "body": body,
        "t": t,
        "replayed": True,
    }


async def _wait_for_page(
    cdp: _Cdp, wait_seconds: float, captured: dict[str, dict], patterns: list[str] | None = None,
    bodies: dict[str, asyncio.Task] | None = None,
    wait_for: str | None = None,
    wait_full: bool = False,
    started: float | None = None,
    finish_times: dict[str, float] | None = None,
    replay_post: dict | None = None,
    replayed_items: list[dict] | None = None,
    replay_tasks: list[asyncio.Task] | None = None,
) -> bool:
    """Ждёт загрузки и затишья сети; True если дождались, False если вышло время.

    Тела нужных JSON-ответов забираются сразу после их загрузки (иначе Chrome вытесняет их из кеша).
    Если заданы patterns, то ждём затишья только по подходящим запросам: сайты вроде Авиасейлс
    опрашивают сервер бесконечно, и общее затишье не наступает.
    """
    patterns = patterns or []
    bodies = bodies if bodies is not None else {}
    found = False  # подстрока wait_for уже встретилась в теле одного из ответов
    deadline = time.monotonic() + wait_seconds
    inflight: set[str] = set()
    matching: set[str] = set()  # запросы, чей адрес подходит под patterns (по requestWillBeSent)
    matched_done = 0
    load_fired = False
    last_activity = last_matched = time.monotonic()
    replay_attempted = False
    replay_task: asyncio.Task | None = None
    replay_handled = False
    while True:
        now = time.monotonic()
        if wait_for and not found:
            found = any(
                t.done() and not t.cancelled() and (r := t.result()) is not None and wait_for in r["body"]
                for t in bodies.values()
            )
            if not found and replay_task and replay_task.done() and not replay_task.cancelled():
                try:
                    r = replay_task.result()
                    if r is not None and wait_for in r.get("body", ""):
                        found = True
                except Exception:
                    pass

        if replay_task is not None and replay_task.done() and not replay_handled:
            replay_handled = True
            last_activity = time.monotonic()
            last_matched = time.monotonic()
            if replayed_items is not None and not replay_task.cancelled():
                try:
                    r = replay_task.result()
                    if r is not None:
                        replayed_items.append(r)
                        if not patterns or any(p in r.get("url", "") for p in patterns):
                            matched_done += 1
                except Exception:
                    pass

        replay_in_flight = replay_task is not None and not replay_task.done()

        if wait_full:
            if now >= deadline:
                if replay_post is not None and not replay_attempted:
                    logger.warning("Повтор POST пропущен: подходящий POST-запрос не был перехвачен")
                return True
        elif wait_for:
            if found and not (matching & inflight) and not replay_in_flight and now - last_matched >= QUIET_SECONDS:
                if replay_post is not None and not replay_attempted:
                    logger.warning("Повтор POST пропущен: подходящий POST-запрос не был перехвачен")
                return True
        elif patterns:
            if matched_done and not (matching & inflight) and not replay_in_flight and now - last_matched >= QUIET_SECONDS:
                if replay_post is not None and not replay_attempted:
                    logger.warning("Повтор POST пропущен: подходящий POST-запрос не был перехвачен")
                return True
        elif load_fired and not inflight and not replay_in_flight and now - last_activity >= QUIET_SECONDS:
            if replay_post is not None and not replay_attempted:
                logger.warning("Повтор POST пропущен: подходящий POST-запрос не был перехвачен")
            return True
        if now >= deadline:
            if replay_task is not None and replay_task.done() and not replay_handled:
                replay_handled = True
                if replayed_items is not None and not replay_task.cancelled():
                    try:
                        r = replay_task.result()
                        if r is not None:
                            replayed_items.append(r)
                    except Exception:
                        pass
            if replay_post is not None and not replay_attempted:
                logger.warning("Повтор POST пропущен: подходящий POST-запрос не был перехвачен")
            return wait_full
        try:
            ev = await asyncio.wait_for(cdp.events.get(), timeout=min(0.25, max(0.01, deadline - now)))
        except asyncio.TimeoutError:
            continue
        method, params = ev.get("method"), ev.get("params", {})
        rid = params.get("requestId")
        if method == "Page.loadEventFired":
            load_fired = True
            last_activity = time.monotonic()
        elif method == "Network.requestWillBeSent":
            inflight.add(rid)
            last_activity = time.monotonic()
            req = params.get("request", {})
            req_url = req.get("url", "")
            req_method = req.get("method", "")
            if any(p in req_url for p in patterns):
                matching.add(rid)
                last_matched = time.monotonic()
            if (
                replay_post is not None
                and not replay_attempted
                and req_method.upper() == "POST"
                and replay_post.get("url_contains", "") in req_url
            ):
                replay_attempted = True
                replay_task = asyncio.create_task(
                    _do_replay(cdp, rid, req, replay_post, started=started or time.monotonic())
                )
                if replay_tasks is not None:
                    replay_tasks.append(replay_task)
        elif method in ("Network.loadingFinished", "Network.loadingFailed"):
            finish_time = time.monotonic()
            if method == "Network.loadingFinished" and finish_times is not None and rid is not None:
                if started is not None:
                    finish_times[rid] = round(max(0.0, finish_time - started), 2)
                else:
                    finish_times[rid] = finish_time
            inflight.discard(rid)
            last_activity = time.monotonic()
            info = captured.get(rid)
            if rid in matching:
                last_matched = time.monotonic()
            if method == "Network.loadingFailed":
                if info is not None:
                    info["failed"] = True
            elif info is not None and _wanted_response(info, patterns) and len(bodies) < MAX_JSON_RESPONSES:
                bodies[rid] = asyncio.create_task(_fetch_body(cdp, rid, info))
                if rid in matching:
                    matched_done += 1
        elif method == "Network.responseReceived":
            resp = params["response"]
            captured[rid] = {
                "url": resp.get("url", ""),
                "status": resp.get("status"),
                "mime": resp.get("mimeType", ""),
                "type": params.get("type", ""),
            }


async def _collect_json(
    bodies: dict[str, asyncio.Task],
    finish_times: dict[str, float] | None = None,
    started: float | None = None,
) -> list[dict]:
    out: list[dict] = []
    total = 0
    for rid, task in bodies.items():
        item = await task
        if item is None:
            continue
        total += len(item["body"])
        if total > MAX_JSON_TOTAL:
            break
        if finish_times is not None:
            if rid in finish_times:
                val = finish_times[rid]
                if val is not None:
                    if started is not None and val >= started:
                        item["t"] = round(max(0.0, val - started), 2)
                    else:
                        item["t"] = round(val, 2)
                else:
                    item["t"] = None
            else:
                item["t"] = None
        out.append(item)
    return out


def _guarded(fn: Any) -> Any:
    """Сетевые сбои на пути к DevTools (обрыв websocket, порт, HTTP) превращает в понятную ошибку."""

    async def wrapper() -> Any:
        try:
            return await fn()
        except (websockets.WebSocketException, httpx.HTTPError, OSError, asyncio.TimeoutError) as exc:
            raise BrowserUnavailable(
                f"Связь с Chrome на телефоне оборвалась ({type(exc).__name__}): повтори запрос"
            ) from exc

    return wrapper


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
    wait_for: str | None = None,
    wait_full: bool = False,
    replay_post: dict | None = None,
    *,
    actor: str = "system",
) -> dict[str, Any]:
    """Открывает url в новой вкладке Chrome, ждёт догрузки и возвращает текст страницы и JSON-ответы сайта.

    capture_json: подстроки адресов, ответы которых нужно вернуть (пусто: все JSON-ответы XHR/fetch);
    возвращаются все подходящие ответы по порядку завершения, по одному на каждый запрос.
    wait_for: подстрока, которая должна появиться в теле одного из ответов (выдача закончена);
    wait_full: ждать wait_seconds целиком (finished=true, если время вышло без ошибок).
    """
    if not re.match(r"^https?://", url, re.IGNORECASE):
        raise ValueError("Нужен адрес, начинающийся с http:// или https://")
    _validate_replay_post(replay_post)
    wait_seconds = max(1.0, min(float(wait_seconds), MAX_WAIT_SECONDS))
    max_chars = max(1000, min(int(max_chars), 1_000_000))
    patterns = [p for p in (capture_json or []) if p]
    params = {
        "url": url, "wait_seconds": wait_seconds, "capture_json": patterns,
        "wait_for": wait_for, "wait_full": wait_full,
    }
    if replay_post is not None:
        params["replay_post"] = {"url_contains": replay_post.get("url_contains")}

    async def _run() -> dict[str, Any]:
        woke = await actions.ensure_awake(device_id, actor=actor)
        serial = actions._get_serial(device_id)
        started = time.monotonic()
        async with _DevTools(serial) as dev:
            tab = await dev.new_tab("about:blank")
            cdp = None
            nav: asyncio.Task | None = None
            bodies: dict[str, asyncio.Task] = {}
            replayed_items: list[dict] = []
            replay_tasks: list[asyncio.Task] = []
            try:
                cdp = await dev.connect(tab)
                await cdp.call("Page.enable")
                await cdp.call("Network.enable", {"maxTotalBufferSize": 100_000_000, "maxResourceBufferSize": 20_000_000})
                # Ответ на navigate у тяжёлых сайтов приходит поздно: не ждём его, а следим за сетью
                nav_started = time.monotonic()
                nav = asyncio.create_task(cdp.call("Page.navigate", {"url": url}, timeout=wait_seconds + 15.0))
                captured: dict[str, dict] = {}
                finish_times: dict[str, float] = {}
                finished = await _wait_for_page(
                    cdp, wait_seconds, captured, patterns, bodies, wait_for=wait_for, wait_full=wait_full,
                    started=nav_started, finish_times=finish_times,
                    replay_post=replay_post, replayed_items=replayed_items, replay_tasks=replay_tasks,
                )
                if nav.done() and not nav.cancelled() and nav.exception() is None:
                    error_text = nav.result().get("errorText")
                    if error_text:
                        raise BrowserError(f"Страница не открылась на телефоне ({error_text}): проверь адрес и интернет")
                result = await _read_page(cdp, max_chars)
                if result["url"] in ("about:blank", ""):
                    raise BrowserError(
                        "Страница не начала загружаться на телефоне за отведённое время: "
                        "сайт не отвечает или телефон медленно грузит его"
                    )
                if result["url"].startswith("chrome-error://"):
                    code = re.search(r"\b[A-Z][A-Z0-9_]{6,}\b", result["text"])
                    raise BrowserError(
                        f"Страница не открылась на телефоне ({code.group(0) if code else 'ошибка сети'}): "
                        "проверь интернет на телефоне и адрес"
                    )
                result["json_responses"] = await _collect_json(bodies, finish_times=finish_times)
                for item in replayed_items:
                    total_json = sum(len(r["body"]) for r in result["json_responses"])
                    if total_json + len(item["body"]) <= MAX_JSON_TOTAL:
                        result["json_responses"].append(item)
                    else:
                        logger.warning("Превышен суммарный лимит JSON при повторе POST")
                result["finished"] = finished
                result["seconds"] = round(time.monotonic() - started, 1)
                result["woke"] = bool(woke)

                matching_ts = [
                    item["t"] for item in result["json_responses"]
                    if item.get("t") is not None and (not patterns or any(p in item.get("url", "") for p in patterns))
                ]
                result["t_first_match"] = min(matching_ts) if matching_ts else None

                return result
            finally:
                if nav is not None:
                    nav.cancel()
                for task in bodies.values():
                    task.cancel()
                for task in replay_tasks:
                    if not task.done():
                        task.cancel()
                if cdp is not None:
                    await cdp.close()
                await dev.close_tab(tab["id"])

    return await actions._execute_action(
        device_id, "fetch_page", params, lambda: _locked(device_id, _guarded(_run)), actor=actor
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
        device_id, "browser_tabs", {}, lambda: _locked(device_id, _guarded(_run)), actor=actor
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
        device_id, "page_text", {"tab_index": tab_index}, lambda: _locked(device_id, _guarded(_run)), actor=actor
    )
