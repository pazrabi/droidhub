"""WebSocket живого видео экрана: /api/devices/{id}/stream (H.264 Annex B кадрами)."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from hub.api.routes import get_screen_size
from hub.auth import require_api
from hub.devices import actions
from hub.devices.stream import StreamInfo, streams

logger = logging.getLogger(__name__)

stream_router = APIRouter()

MIN_WIDTH, MAX_WIDTH, DEFAULT_WIDTH = 240, 1080, 540
ALLOWED_ORIGIN_HOSTS = {"molokohub.site"}


def origin_allowed(websocket: WebSocket) -> bool:
    """Браузерный WebSocket должен идти со страницы самого хаба или молокохаба (защита от чужих сайтов)."""
    origin = websocket.headers.get("origin")
    if not origin:
        return True  # не браузер (Bearer-клиенты)
    host = urlsplit(origin).netloc
    return host == websocket.headers.get("host") or host in ALLOWED_ORIGIN_HOSTS


def stream_size(width: int, real_width: int, real_height: int) -> tuple[int, int]:
    """Размер кодирования: заданная ширина и высота по пропорциям экрана, оба значения чётные."""
    width = max(MIN_WIDTH, min(MAX_WIDTH, width)) // 2 * 2
    height = round(width * real_height / real_width) // 2 * 2
    return width, height


def config_message(info: StreamInfo, real_width: int, real_height: int) -> str:
    return json.dumps({
        "type": "config", "codec": info.codec, "width": info.width, "height": info.height,
        "sps": base64.b64encode(info.sps).decode(), "pps": base64.b64encode(info.pps).decode(),
        "real_width": real_width, "real_height": real_height,
    })


@stream_router.websocket("/api/devices/{device_id}/stream")
async def device_stream(websocket: WebSocket, device_id: str, width: int = DEFAULT_WIDTH) -> None:
    """Видео экрана: сначала JSON «config», затем бинарные кадры: 1 байт (1 = ключевой) + данные H.264."""
    if not origin_allowed(websocket):
        await websocket.close(code=1008)
        return
    try:
        actor = require_api(websocket)  # type: ignore[arg-type]  (HTTPConnection-совместимо: сессия, Bearer, SSO)
    except HTTPException:
        await websocket.close(code=1008)
        return
    manager = actions.get_manager()
    try:
        serial = manager.serial(device_id)
    except KeyError:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    try:
        await actions.ensure_awake(device_id, actor=actor)
        real_width, real_height = await get_screen_size(device_id)
    except Exception as exc:
        await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
        await websocket.close(code=1011)
        return

    enc_width, enc_height = stream_size(width, real_width, real_height)
    session = streams.session(device_id, serial, enc_width, enc_height)
    queue = session.subscribe()
    watcher = asyncio.create_task(_drain_client(websocket))
    try:
        while not watcher.done():
            getter = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({getter, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if getter not in done:
                getter.cancel()
                break
            kind, item = getter.result()
            if kind == "config":
                await websocket.send_text(config_message(item, real_width, real_height))
            else:
                await websocket.send_bytes((b"\x01" if item.is_key else b"\x00") + item.data)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        watcher.cancel()
        session.unsubscribe(queue)
        with contextlib.suppress(Exception):
            await websocket.close()


async def _drain_client(websocket: WebSocket) -> None:
    """Читает входящие сообщения клиента (их нет), чтобы заметить разрыв соединения."""
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        while (await websocket.receive())["type"] != "websocket.disconnect":
            pass
