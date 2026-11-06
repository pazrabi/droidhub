"""Задания на загрузку страниц в Chrome (fetch_jobs) для потокового получения ответов (стриминг JSON).

Позволяет запустить навигацию в фоновом режиме, опрашивать появляющиеся ответы через GET /api/fetch_jobs/{job_id}?since=N,
и завершить чтением текста страницы через POST /api/fetch_jobs/{job_id}/finish.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import sys
import time
from typing import Any

import httpx
import websockets

from hub.devices import actions, activity, browser
from hub.devices.browser import (
    DEFAULT_MAX_CHARS,
    MAX_JSON_BODY,
    MAX_JSON_RESPONSES,
    MAX_JSON_TOTAL,
    MAX_WAIT_SECONDS,
    BrowserBusy,
    BrowserError,
    BrowserUnavailable,
    detect_captcha,
)

MIN_MAX_SECONDS: float = 1.0
MAX_MAX_SECONDS: float = 90.0
DEFAULT_MAX_SECONDS: float = 45.0
CLIENT_SILENCE_SECONDS: float = 15.0
QUIET_SECONDS: float = 1.5
FINISH_BODY_WAIT_SECONDS: float = 15.0

logger = logging.getLogger(__name__)

_DevTools: Any = None
ensure_awake: Any = None
_read_page: Any = None

_LIGHT_PAGE_JS = (
    "JSON.stringify({title: document.title, url: location.href, "
    "text: document.body ? document.body.innerText.slice(0, 3000) : ''})"
)


def _get_quiet_seconds() -> float:
    mod_val = getattr(sys.modules[__name__], "QUIET_SECONDS", 1.5)
    if mod_val != 1.5:
        return float(mod_val)
    b_val = getattr(browser, "QUIET_SECONDS", 1.5)
    if b_val != 1.5:
        return float(b_val)
    return float(mod_val)


def _get_finish_body_wait_seconds() -> float:
    return float(getattr(sys.modules[__name__], "FINISH_BODY_WAIT_SECONDS", 15.0))


def _get_client_silence_seconds() -> float:
    return float(getattr(sys.modules[__name__], "CLIENT_SILENCE_SECONDS", 15.0))


def _get_min_max_seconds() -> float:
    return float(getattr(sys.modules[__name__], "MIN_MAX_SECONDS", 1.0))


def _get_max_max_seconds() -> float:
    return float(getattr(sys.modules[__name__], "MAX_MAX_SECONDS", 90.0))


def _create_devtools(serial: str) -> Any:
    dt_cls = getattr(sys.modules[__name__], "_DevTools", None)
    if dt_cls is not None:
        return dt_cls(serial)
    return browser._DevTools(serial)


def _get_ensure_awake_fn() -> Any:
    fn = getattr(sys.modules[__name__], "ensure_awake", None)
    if fn is not None:
        return fn
    return actions.ensure_awake


def _get_read_page_fn() -> Any:
    fn = getattr(sys.modules[__name__], "_read_page", None)
    if fn is not None:
        return fn
    return browser._read_page


class FetchJob:
    """Управление жизненным циклом одного задания на загрузку страницы."""

    def __init__(
        self,
        job_id: str,
        device_id: str,
        url: str,
        wait_seconds: float,
        capture_json: list[str],
        max_chars: int,
        wait_for: str | None,
        wait_full: bool,
        max_seconds: float,
        actor: str,
        started: float,
        nav_started: float,
        woke: bool,
        dev: Any,
        cdp: Any,
        tab_id: str,
        nav_task: asyncio.Task,
        activity_token: int,
        lock_held: bool = True,
        replay_post: dict | None = None,
    ) -> None:
        self.job_id = job_id
        self.device_id = device_id
        self.url = url
        self.wait_seconds = wait_seconds
        self.capture_json = capture_json
        self.max_chars = max_chars
        self.wait_for = wait_for
        self.wait_full = wait_full
        self.max_seconds = max_seconds
        self.actor = actor
        self.started = started
        self.nav_started = nav_started
        self.woke = woke
        self.dev = dev
        self.cdp = cdp
        self.tab_id: str | None = tab_id
        self._nav_task: asyncio.Task | None = nav_task
        self._activity_token: int | None = activity_token
        self._lock_held: bool = lock_held
        self.replay_post: dict | None = replay_post
        self._replay_task: asyncio.Task | None = None
        self._replay_attempted: bool = False

        self.state: str = "running"
        self.error: str | None = None
        self.finished: bool = False
        self.wait_for_seen: bool = False
        self.t_first_match: float | None = None
        self.captcha: bool = False
        self.responses: list[dict[str, Any]] = []

        self._total_json_bytes: int = 0
        self._last_client_activity: float = time.monotonic()
        self._is_closed: bool = False

        self._new_item_event: asyncio.Event = asyncio.Event()
        self._collector_task: asyncio.Task | None = None
        self._captcha_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None

        self._matching_reqs: set[str] = set()
        self._inflight_reqs: set[str] = set()
        self._captured: dict[str, dict[str, Any]] = {}
        self._bodies_tasks: list[asyncio.Task] = []
        self._matched_done: int = 0
        self._last_matched_time: float = started
        self._last_activity_time: float = started
        self._load_event_fired: bool = False
        self._max_served_next: int = 0

    def touch(self) -> None:
        """Сбрасывает таймер автоотмены при активности клиента."""
        self._last_client_activity = time.monotonic()

    def _notify(self) -> None:
        """Пробуждает клиентов, ожидающих на длинном опросе."""
        old_ev = self._new_item_event
        self._new_item_event = asyncio.Event()
        old_ev.set()

    def _has_inflight_bodies(self) -> bool:
        """Проверяет, есть ли ещё выполняющиеся фоновые задачи загрузки тел ответов или повтора POST."""
        if self._replay_task is not None and not self._replay_task.done():
            return True
        return any(not t.done() for t in self._bodies_tasks)

    def is_network_quiet(self) -> bool:
        """Проверяет затишье сети по подходящим запросам."""
        if self._matched_done < 1:
            return False
        if self._matching_reqs & self._inflight_reqs:
            return False
        if self._has_inflight_bodies():
            return False
        return (time.monotonic() - self._last_matched_time) >= _get_quiet_seconds()

    def get_slice(self, since: int) -> tuple[list[dict[str, Any]], int]:
        """Возвращает порцию ответов с i >= since и следующий индекс next."""
        items = [r for r in self.responses if r["i"] >= since]
        next_i = len(self.responses)
        if items:
            self._max_served_next = max(self._max_served_next, next_i)
        return items, next_i

    async def wait_for_data(self, since: int, timeout: float = 1.0) -> None:
        """Длинный опрос: ждёт появления нового ответа или смены состояния до 1 с."""
        if self.state != "running" or any(r["i"] >= since for r in self.responses):
            return
        ev = self._new_item_event
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    def start_tasks(self) -> None:
        """Запускает фоновые задачи сбора ответов, проверки капчи и сторожевого таймера."""
        self._collector_task = asyncio.create_task(self._collector_loop())
        self._captcha_task = asyncio.create_task(self._captcha_loop())
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def _fetch_body_and_store(self, rid: str, info: dict[str, Any], t: float) -> None:
        url = info.get("url", "")
        try:
            item = await browser._fetch_body(self.cdp, rid, info)
            if item is None:
                logger.warning(
                    "Не удалось получить тело ответа (job_id=%s, url=%s)",
                    self.job_id,
                    url,
                )
                return
            body = item.get("body", "")
            if self._total_json_bytes + len(body) > MAX_JSON_TOTAL:
                logger.warning(
                    "Превышен суммарный лимит JSON (job_id=%s, url=%s)",
                    self.job_id,
                    url,
                )
                return
            self._total_json_bytes += len(body)
            resp_obj = {
                "i": len(self.responses),
                "t": t,
                "url": item["url"],
                "status": item["status"],
                "body": body,
            }
            self.responses.append(resp_obj)
            if self.wait_for and self.wait_for in body:
                self.wait_for_seen = True
            if not self.capture_json or any(p in item["url"] for p in self.capture_json):
                if self.t_first_match is None or t < self.t_first_match:
                    self.t_first_match = t
            self._notify()
        except Exception as exc:
            logger.warning(
                "Ошибка при получении тела ответа (job_id=%s, url=%s): %s",
                self.job_id,
                url,
                exc,
            )
        finally:
            self._last_matched_time = time.monotonic()
            self._last_activity_time = time.monotonic()

    async def _execute_replay(self, rid: str, req: dict[str, Any]) -> None:
        try:
            item = await browser._do_replay(
                self.cdp, rid, req, self.replay_post, started=self.nav_started
            )
            if item is None:
                return
            body = item.get("body", "")
            if self._total_json_bytes + len(body) > MAX_JSON_TOTAL:
                logger.warning(
                    "Превышен суммарный лимит JSON при повторе POST (job_id=%s, url=%s)",
                    self.job_id,
                    item.get("url", ""),
                )
                return
            self._total_json_bytes += len(body)
            resp_obj = {
                "i": len(self.responses),
                "t": item["t"],
                "url": item["url"],
                "status": item["status"],
                "body": body,
                "replayed": True,
            }
            self.responses.append(resp_obj)
            if self.wait_for and self.wait_for in body:
                self.wait_for_seen = True
            if not self.capture_json or any(p in item["url"] for p in self.capture_json):
                if self.t_first_match is None or item["t"] < self.t_first_match:
                    self.t_first_match = item["t"]
                self._matched_done += 1
            self._notify()
        except Exception as exc:
            logger.warning("Ошибка при повторе POST (job_id=%s): %s", self.job_id, type(exc).__name__)
        finally:
            self._last_matched_time = time.monotonic()
            self._last_activity_time = time.monotonic()

    async def _collector_loop(self) -> None:
        """Основной цикл обработки CDP-событий сети и навигации."""
        try:
            while not self._is_closed and self.state == "running":
                now = time.monotonic()
                quiet_sec = _get_quiet_seconds()

                # Проверка условий готовности
                if self.wait_full:
                    if now - self.started >= self.wait_seconds:
                        self.finished = True
                        self.state = "done"
                        self._notify()
                        break
                elif self.wait_for:
                    if self.wait_for_seen and self.is_network_quiet():
                        self.finished = True
                        self.state = "done"
                        self._notify()
                        break
                elif self.capture_json:
                    if self.is_network_quiet():
                        self.finished = True
                        self.state = "done"
                        self._notify()
                        break
                elif (
                    self._load_event_fired
                    and not self._inflight_reqs
                    and not self._has_inflight_bodies()
                    and (now - self._last_activity_time >= quiet_sec)
                ):
                    self.finished = True
                    self.state = "done"
                    self._notify()
                    break

                # Проверка предельного времени max_seconds
                if now - self.started >= self.max_seconds:
                    self.finished = (self.wait_full and (now - self.started >= self.wait_seconds))
                    self.state = "done"
                    self._notify()
                    break

                # Проверка ошибки навигации
                if self._nav_task and self._nav_task.done() and not self._nav_task.cancelled():
                    if self._nav_task.exception() is not None:
                        raise self._nav_task.exception()
                    error_text = self._nav_task.result().get("errorText")
                    if error_text:
                        raise BrowserError(
                            f"Страница не открылась на телефоне ({error_text}): проверь адрес и интернет"
                        )

                # Получение очередного события
                wait_time = min(0.05, max(0.01, (self.started + self.max_seconds) - now))
                try:
                    ev = await asyncio.wait_for(self.cdp.events.get(), timeout=wait_time)
                except asyncio.TimeoutError:
                    continue

                method = ev.get("method")
                params = ev.get("params", {})
                rid = params.get("requestId")

                if method == "Page.loadEventFired":
                    self._load_event_fired = True
                    self._last_activity_time = time.monotonic()
                elif method == "Network.requestWillBeSent":
                    req = params.get("request", {})
                    req_url = req.get("url", "")
                    req_method = req.get("method", "")
                    self._inflight_reqs.add(rid)
                    self._last_activity_time = time.monotonic()
                    if self.capture_json:
                        if any(p in req_url for p in self.capture_json):
                            self._matching_reqs.add(rid)
                            self._last_matched_time = time.monotonic()
                    else:
                        self._matching_reqs.add(rid)
                        self._last_matched_time = time.monotonic()

                    if (
                        self.replay_post is not None
                        and not self._replay_attempted
                        and req_method.upper() == "POST"
                        and self.replay_post.get("url_contains", "") in req_url
                    ):
                        self._replay_attempted = True
                        self._replay_task = asyncio.create_task(
                            self._execute_replay(rid, req)
                        )
                elif method == "Network.responseReceived":
                    resp = params.get("response", {})
                    self._captured[rid] = {
                        "url": resp.get("url", ""),
                        "status": resp.get("status"),
                        "mime": resp.get("mimeType", ""),
                        "type": params.get("type", ""),
                    }
                elif method in ("Network.loadingFinished", "Network.loadingFailed"):
                    finish_time = time.monotonic()
                    self._inflight_reqs.discard(rid)
                    self._last_activity_time = time.monotonic()
                    if rid in self._matching_reqs:
                        self._last_matched_time = time.monotonic()
                    info = self._captured.get(rid)
                    if method == "Network.loadingFailed":
                        if info is not None:
                            info["failed"] = True
                    elif info is not None and browser._wanted_response(info, self.capture_json) and len(self._bodies_tasks) < MAX_JSON_RESPONSES:
                        t = round(max(0.0, finish_time - self.nav_started), 2)
                        if not self.capture_json or rid in self._matching_reqs:
                            self._matched_done += 1
                        task = asyncio.create_task(self._fetch_body_and_store(rid, info, t))
                        self._bodies_tasks.append(task)
            if self.replay_post is not None and not self._replay_attempted:
                logger.warning(
                    "Повтор POST пропущен: подходящий POST-запрос не был перехвачен (job_id=%s)",
                    self.job_id,
                )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.state = "failed"
            self.error = str(exc) or "Связь с Chrome на телефоне оборвалась"
            self._notify()
            await self._release_resources()
            try:
                await actions._record_audit(
                    self.device_id,
                    "fetch_job",
                    {"job_id": self.job_id, "event": "failed", "error": self.error},
                    ok=False,
                    error=self.error,
                    actor=self.actor,
                )
            except Exception:
                pass
            asyncio.create_task(self._delayed_remove(60.0))

    async def _captcha_loop(self) -> None:
        """Периодическая проверка наличия капчи на странице (раз в ~2 с)."""
        try:
            while not self._is_closed and self.state == "running":
                await asyncio.sleep(2.0)
                if self._is_closed or self.state != "running":
                    break
                if self.cdp is None:
                    break
                try:
                    res = await self.cdp.call(
                        "Runtime.evaluate",
                        {"expression": _LIGHT_PAGE_JS, "returnByValue": True},
                        timeout=2.0,
                    )
                    val = res.get("result", {}).get("value")
                    if val:
                        data = json.loads(val)
                        if detect_captcha(data.get("url", ""), data.get("title", ""), data.get("text", "")):
                            self.captcha = True
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    async def _watchdog_loop(self) -> None:
        """Сторожевой таймер: отменяет задание при молчании клиента дольше CLIENT_SILENCE_SECONDS."""
        try:
            while not self._is_closed:
                silence_limit = _get_client_silence_seconds()
                elapsed = time.monotonic() - self._last_client_activity
                remaining = silence_limit - elapsed
                if remaining <= 0:
                    await self.auto_cancel()
                    break
                await asyncio.sleep(min(remaining, 0.5))
        except asyncio.CancelledError:
            pass

    async def _release_resources(self) -> None:
        """Освобождает вкладку, закрывает порт и снимает замок (гарантированно один раз)."""
        current = asyncio.current_task()
        tasks = [self._collector_task, self._captcha_task, self._watchdog_task, self._nav_task]
        for t in tasks:
            if t is not None and t is not current and not t.done():
                t.cancel()

        for t in self._bodies_tasks:
            if t is not None and t is not current and not t.done():
                t.cancel()

        if self._replay_task is not None and self._replay_task is not current and not self._replay_task.done():
            self._replay_task.cancel()

        try:
            # Сначала закрываем вкладку: Chrome на загружающейся странице не отвечает на закрытие сокета
            # (websockets ждёт до 10 с), а закрытая вкладка рвёт сокет сама.
            if self.dev is not None and self.tab_id:
                tab_id = self.tab_id
                self.tab_id = None
                try:
                    await asyncio.wait_for(self.dev.close_tab(tab_id), timeout=3.0)
                except BaseException:
                    pass

            if self.cdp is not None:
                cdp = self.cdp
                self.cdp = None
                try:
                    await asyncio.wait_for(cdp.close(), timeout=1.0)
                except BaseException:
                    pass

            if self.dev is not None:
                dev = self.dev
                self.dev = None
                try:
                    await dev.__aexit__(None, None, None)
                except BaseException:
                    pass
        finally:
            if self._activity_token is not None:
                token = self._activity_token
                self._activity_token = None
                try:
                    activity.end(self.device_id, token)
                except BaseException:
                    pass

            if self._lock_held:
                self._lock_held = False
                try:
                    browser._get_lock(self.device_id).release()
                except RuntimeError:
                    pass

    async def _delayed_remove(self, delay: float = 60.0) -> None:
        """Удаляет завершённое задание из реестра памяти через delay секунд."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            pass
        finally:
            _jobs.pop(self.job_id, None)

    async def auto_cancel(self) -> None:
        """Автоотмена при отсутствии запросов клиента."""
        if self._is_closed:
            return
        self._is_closed = True
        if self.state == "running":
            self.state = "done"
        await self._release_resources()
        try:
            await actions._record_audit(
                self.device_id,
                "fetch_job",
                {"job_id": self.job_id, "event": "auto_cancel", "reason": "таймаут активности клиента"},
                ok=True,
                actor=self.actor,
            )
        except Exception:
            pass
        self._notify()
        asyncio.create_task(self._delayed_remove(60.0))

    async def cancel(self) -> bool:
        """Отмена задания: закрыть вкладку, снять замок, отпустить телефон."""
        if self._is_closed:
            return False
        self._is_closed = True
        if self.state == "running":
            self.state = "done"
        await self._release_resources()
        try:
            await actions._record_audit(
                self.device_id,
                "fetch_job",
                {"job_id": self.job_id, "event": "cancel"},
                ok=True,
                actor=self.actor,
            )
        except Exception:
            pass
        self._notify()
        asyncio.create_task(self._delayed_remove(60.0))
        return True

    async def finish(self) -> dict[str, Any]:
        """Завершает задание: читает страницу, закрывает вкладку и снимает замок."""
        self.touch()
        if self._is_closed:
            raise KeyError("Задание не найдено или уже закрыто")

        if self.state == "failed":
            self._is_closed = True
            await self._release_resources()
            asyncio.create_task(self._delayed_remove(60.0))
            raise BrowserError(self.error or "Задание завершилось с ошибкой")

        if self._collector_task and not self._collector_task.done():
            self._collector_task.cancel()
        if self._captcha_task and not self._captcha_task.done():
            self._captcha_task.cancel()
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()

        try:
            pending = [t for t in self._bodies_tasks if not t.done()]
            if self._replay_task is not None and not self._replay_task.done():
                pending.append(self._replay_task)
            if pending:
                done, not_done = await asyncio.wait(
                    pending, timeout=_get_finish_body_wait_seconds()
                )
                for t in not_done:
                    t.cancel()

            if self._nav_task and self._nav_task.done() and not self._nav_task.cancelled():
                if self._nav_task.exception() is not None:
                    raise self._nav_task.exception()
                error_text = self._nav_task.result().get("errorText")
                if error_text:
                    raise BrowserError(
                        f"Страница не открылась на телефоне ({error_text}): проверь адрес и интернет"
                    )

            read_page_fn = _get_read_page_fn()
            result = await read_page_fn(self.cdp, self.max_chars)
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

            unserved = [
                {
                    "url": r["url"],
                    "status": r["status"],
                    "body": r["body"],
                    "t": r["t"],
                    **({"replayed": r["replayed"]} if "replayed" in r else {}),
                }
                for r in self.responses
                if r["i"] >= self._max_served_next
            ]
            self._max_served_next = max(self._max_served_next, len(self.responses))

            seconds = round(time.monotonic() - self.started, 1)
            res = {
                "url": result["url"],
                "title": result["title"],
                "text": result["text"],
                "truncated": result["truncated"],
                "captcha": result["captcha"] or self.captcha,
                "finished": self.finished,
                "seconds": seconds,
                "woke": self.woke,
                "t_first_match": self.t_first_match,
                "json_responses": unserved,
            }

            try:
                await actions._record_audit(
                    self.device_id,
                    "fetch_job",
                    {"job_id": self.job_id, "event": "finish", "seconds": seconds, "finished": self.finished},
                    ok=True,
                    actor=self.actor,
                )
            except Exception:
                pass

            return res
        finally:
            self._is_closed = True
            await self._release_resources()
            asyncio.create_task(self._delayed_remove(60.0))


_jobs: dict[str, FetchJob] = {}


def get_active_job(job_id: str) -> FetchJob | None:
    """Возвращает активное задание по job_id или None."""
    job = _jobs.get(job_id)
    if job is None or job._is_closed:
        return None
    return job


async def cancel_job(job_id: str) -> bool:
    """Отменяет задание по job_id. Возвращает True если отменено, False если уже закрыто или не найдено."""
    job = _jobs.get(job_id)
    if job is None:
        return False
    return await job.cancel()


async def cancel_all() -> None:
    """Отменяет все активные задания (вызывается при остановке приложения в lifespan)."""
    jobs = list(_jobs.values())
    for job in jobs:
        try:
            await job.cancel()
        except Exception:
            pass
    _jobs.clear()


async def create_fetch_job(
    device_id: str,
    url: str,
    wait_seconds: float = 15.0,
    capture_json: list[str] | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    wait_for: str | None = None,
    wait_full: bool = False,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    replay_post: dict | None = None,
    *,
    actor: str = "system",
) -> str:
    """Создаёт и запускает задание на загрузку страницы."""
    if not re.match(r"^https?://", url, re.IGNORECASE):
        raise ValueError("Нужен адрес, начинающийся с http:// или https://")
    browser._validate_replay_post(replay_post)

    min_s = _get_min_max_seconds()
    max_s = _get_max_max_seconds()
    if not (min_s <= max_seconds <= max_s):
        raise ValueError(f"Параметр max_seconds должен быть от {min_s} до {max_s} с")

    wait_seconds = max(1.0, min(float(wait_seconds), MAX_WAIT_SECONDS))
    max_chars = max(1000, min(int(max_chars), 1_000_000))
    patterns = [p for p in (capture_json or []) if p]

    lock = browser._get_lock(device_id)
    if lock.locked():
        raise BrowserBusy("Телефон занят: с ним сейчас работает другой запрос к браузеру, повтори позже")
    try:
        await asyncio.wait_for(lock.acquire(), timeout=1.0)
    except asyncio.TimeoutError:
        raise BrowserBusy("Телефон занят: с ним сейчас работает другой запрос к браузеру, повтори позже")

    activity_token = None
    dev = None
    cdp = None
    tab_id = None
    lock_held = True
    try:
        activity_token = activity.begin(device_id, "fetch_job", actor)
        ensure_awake_fn = _get_ensure_awake_fn()
        woke = await ensure_awake_fn(device_id, actor=actor)

        serial = actions._get_serial(device_id)
        dev = _create_devtools(serial)
        try:
            await dev.__aenter__()
        except (websockets.WebSocketException, httpx.HTTPError, OSError, asyncio.TimeoutError) as exc:
            raise BrowserUnavailable(f"Связь с Chrome на телефоне оборвалась ({type(exc).__name__}): повтори запрос") from exc

        try:
            tab = await dev.new_tab("about:blank")
            tab_id = tab["id"]
            cdp = await dev.connect(tab)
            await cdp.call("Page.enable")
            await cdp.call("Network.enable", {"maxTotalBufferSize": 100_000_000, "maxResourceBufferSize": 20_000_000})

            nav_started = time.monotonic()
            nav_task = asyncio.create_task(cdp.call("Page.navigate", {"url": url}, timeout=max_seconds + 15.0))
        except (websockets.WebSocketException, httpx.HTTPError, OSError, asyncio.TimeoutError) as exc:
            raise BrowserUnavailable(f"Связь с Chrome на телефоне оборвалась ({type(exc).__name__}): повтори запрос") from exc

        job_id = secrets.token_urlsafe(16)
        job = FetchJob(
            job_id=job_id,
            device_id=device_id,
            url=url,
            wait_seconds=wait_seconds,
            capture_json=patterns,
            max_chars=max_chars,
            wait_for=wait_for,
            wait_full=wait_full,
            max_seconds=max_seconds,
            actor=actor,
            started=time.monotonic(),
            nav_started=nav_started,
            woke=bool(woke),
            dev=dev,
            cdp=cdp,
            tab_id=tab_id,
            nav_task=nav_task,
            activity_token=activity_token,
            lock_held=True,
            replay_post=replay_post,
        )

        job.start_tasks()
        _jobs[job_id] = job
        lock_held = False

        try:
            await actions._record_audit(
                device_id,
                "fetch_job",
                {
                    "job_id": job_id,
                    "url": url,
                    "event": "start",
                    "wait_seconds": wait_seconds,
                    "max_seconds": max_seconds,
                    "capture_json": patterns,
                    "wait_for": wait_for,
                    "wait_full": wait_full,
                    **({"replay_post": {"url_contains": replay_post["url_contains"]}} if replay_post else {}),
                },
                ok=True,
                actor=actor,
            )
        except Exception:
            pass

        return job_id
    finally:
        if lock_held:
            if cdp is not None:
                try:
                    await cdp.close()
                except BaseException:
                    pass
            if dev is not None:
                if tab_id:
                    try:
                        await dev.close_tab(tab_id)
                    except BaseException:
                        pass
                try:
                    await dev.__aexit__(None, None, None)
                except BaseException:
                    pass
            if activity_token is not None:
                try:
                    activity.end(device_id, activity_token)
                except BaseException:
                    pass
            try:
                lock.release()
            except RuntimeError:
                pass
