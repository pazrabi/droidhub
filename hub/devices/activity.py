"""Занятость устройств: какое действие сейчас выполняется на телефоне и кем."""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

# Понятные названия действий для панели; остальные показываем как есть
LABELS: dict[str, str] = {
    "fetch_page": "читает сайт",
    "browser_tabs": "читает вкладки",
    "page_text": "читает страницу",
    "tap": "нажатие",
    "tap_element": "нажатие",
    "swipe": "свайп",
    "long_press": "долгое нажатие",
    "type_text": "ввод текста",
    "key": "кнопка",
    "open_app": "запуск приложения",
    "open_url": "открывает ссылку",
    "ui_dump": "читает экран",
    "list_apps": "список приложений",
    "wait": "ожидание",
    "power": "питание",
    "wake_and_unlock": "разблокировка",
}

# device_id -> активные операции: токен -> (действие, кто, когда началась)
_active: dict[str, dict[int, tuple[str, str, float]]] = {}
_counter = 0


@asynccontextmanager
async def track(device_id: str, action: str, actor: str = "system") -> AsyncIterator[None]:
    """Отмечает устройство занятым на время действия."""
    global _counter
    _counter += 1
    token = _counter
    _active.setdefault(device_id, {})[token] = (action, actor, time.time())
    try:
        yield
    finally:
        ops = _active.get(device_id)
        if ops is not None:
            ops.pop(token, None)
            if not ops:
                _active.pop(device_id, None)


def current(device_id: str) -> dict | None:
    """Самая долгая из идущих операций устройства или None, если свободно."""
    ops = _active.get(device_id)
    if not ops:
        return None
    action, actor, since = min(ops.values(), key=lambda o: o[2])
    return {"action": action, "label": LABELS.get(action, action), "actor": actor, "since": since}
