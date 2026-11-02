from __future__ import annotations

import asyncio
from collections import defaultdict
import dataclasses
import re
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel

import hub.audit as audit_module
from hub.config import get_settings
from hub.devices import actions
from hub.devices.adb import AdbError, AdbTimeout, DeviceOffline, adb_shell
from hub.devices.manager import DeviceStatus

from hub.auth import require_api


# --- Модели запросов ---

class TapRequest(BaseModel):
    x: int
    y: int


class SwipeRequest(BaseModel):
    x1: int
    y1: int
    x2: int
    y2: int
    ms: int = 300


class TextRequest(BaseModel):
    text: str


class KeyRequest(BaseModel):
    name: str


class OpenAppRequest(BaseModel):
    package: str


class PowerRequest(BaseModel):
    action: Literal["wake", "sleep", "reboot", "shutdown"]
    confirm: bool = False


# actions.power разрабатывается параллельно другим разработчиком
if not hasattr(actions, "power"):
    async def _power_placeholder(device_id: str, action: str, *, actor: str = "system") -> None:
        raise NotImplementedError("actions.power разрабатывается параллельно")

    actions.power = _power_placeholder  # type: ignore


# --- Лимитер действий на устройство (скользящее окно в памяти) ---

class DeviceRateLimiter:
    """Ограничитель действий на устройство со скользящим окном в памяти."""

    def __init__(self, window_seconds: float = 60.0) -> None:
        self.window_seconds = window_seconds
        self._history: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def check(self, device_id: str, limit: int | None = None) -> bool:
        if limit is None:
            try:
                settings = get_settings()
                limit = getattr(settings, "rate_limit_per_min", 60)
            except Exception:
                limit = 60

        if limit <= 0:
            return True

        now = time.monotonic()
        cutoff = now - self.window_seconds

        async with self._lock:
            history = [t for t in self._history[device_id] if t > cutoff]
            if len(history) >= limit:
                self._history[device_id] = history
                return False
            history.append(now)
            self._history[device_id] = history
            return True

    def reset(self, device_id: str | None = None) -> None:
        if device_id is not None:
            self._history.pop(device_id, None)
        else:
            self._history.clear()


limiter = DeviceRateLimiter()


def reset_rate_limiter() -> None:
    limiter.reset()


# --- Кеш размеров экранов телефонов (wm size) ---

_screen_size_cache: dict[str, tuple[int, int]] = {}


def parse_wm_size(output: str) -> tuple[int, int] | None:
    """
    Парсит ширину и высоту экрана из вывода wm size.
    Приоритет у Override size, затем Physical size.
    """
    if not output:
        return None

    override_m = re.search(r"Override size:\s*(\d+)x(\d+)", output, re.IGNORECASE)
    if override_m:
        return int(override_m.group(1)), int(override_m.group(2))

    phys_m = re.search(r"Physical size:\s*(\d+)x(\d+)", output, re.IGNORECASE)
    if phys_m:
        return int(phys_m.group(1)), int(phys_m.group(2))

    any_m = re.search(r"(\d+)x(\d+)", output)
    if any_m:
        return int(any_m.group(1)), int(any_m.group(2))

    return None


async def get_screen_size(device_id: str) -> tuple[int, int]:
    """Возвращает реальный размер экрана телефона через wm size с кешированием."""
    if device_id in _screen_size_cache:
        return _screen_size_cache[device_id]

    mgr = actions.get_manager()
    serial = mgr.serial(device_id)

    out = await adb_shell(serial, "wm size")
    parsed = parse_wm_size(out)
    if parsed:
        _screen_size_cache[device_id] = parsed
        return parsed

    default_size = (1080, 2400)
    _screen_size_cache[device_id] = default_size
    return default_size


def clear_screen_size_cache() -> None:
    _screen_size_cache.clear()


# --- Вспомогательные проверки устройств и ошибок ---

def _verify_device_exists(device_id: str) -> None:
    """Проверяет существование устройства в менеджере. 404 если не найдено."""
    mgr = actions.get_manager()
    try:
        mgr.get(device_id)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Устройство '{device_id}' не найдено",
        )


async def _check_device_and_rate_limit(device_id: str) -> None:
    """Проверяет наличие устройства и лимит действий."""
    _verify_device_exists(device_id)
    allowed = await limiter.check(device_id)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Превышен лимит действий на устройство",
        )


# --- Роутеры ---

public_router = APIRouter(prefix="/api", tags=["public"])
api_router = APIRouter(prefix="/api", dependencies=[Depends(require_api)], tags=["api"])


@public_router.get("/health")
async def health() -> dict[str, str]:
    """Публичный эндпоинт проверки работоспособности без авторизации."""
    return {"status": "ok"}


@api_router.get("/devices")
async def get_devices(actor: str = Depends(require_api)) -> list[dict[str, Any]]:
    """Возвращает список устройств и их статусы."""
    mgr = actions.get_manager()
    statuses = mgr.statuses()
    has_charging = hasattr(DeviceStatus, "charging") or "charging" in getattr(DeviceStatus, "__dataclass_fields__", {})
    results: list[dict[str, Any]] = []
    for s in statuses:
        if dataclasses.is_dataclass(s):
            item = dataclasses.asdict(s)
        elif isinstance(s, dict):
            item = dict(s)
        else:
            item = {
                "id": getattr(s, "id", ""),
                "name": getattr(s, "name", ""),
                "online": getattr(s, "online", False),
                "battery": getattr(s, "battery", None),
                "temperature": getattr(s, "temperature", None),
                "screen_on": getattr(s, "screen_on", None),
                "last_seen": getattr(s, "last_seen", None),
                "error": getattr(s, "error", None),
            }
        if has_charging or hasattr(s, "charging") or (isinstance(s, dict) and "charging" in s):
            if isinstance(s, dict):
                item["charging"] = s.get("charging")
            else:
                item["charging"] = getattr(s, "charging", None)
        results.append(item)
    return results


@api_router.get("/devices/{device_id}/screenshot")
async def get_screenshot(
    device_id: str,
    max_width: int = Query(default=720, ge=1),
    actor: str = Depends(require_api),
) -> Response:
    """Возвращает JPEG-скриншот и заголовки X-Screen-Width / X-Screen-Height с реальным размером экрана."""
    await _check_device_and_rate_limit(device_id)
    try:
        jpeg_bytes = await actions.screenshot(device_id, max_width=max_width, actor=actor)
        width, height = await get_screen_size(device_id)
        return Response(
            content=jpeg_bytes,
            media_type="image/jpeg",
            headers={
                "X-Screen-Width": str(width),
                "X-Screen-Height": str(height),
            },
        )
    except (DeviceOffline, AdbTimeout, AdbError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Устройство '{device_id}' не найдено")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@api_router.post("/devices/{device_id}/tap")
async def tap_device(
    device_id: str,
    req: TapRequest,
    actor: str = Depends(require_api),
) -> dict[str, Any]:
    """Нажатие (тап) по координатам."""
    await _check_device_and_rate_limit(device_id)
    try:
        await actions.tap(device_id, req.x, req.y, actor=actor)
        return {"ok": True, "status": "ok"}
    except (DeviceOffline, AdbTimeout, AdbError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Устройство '{device_id}' не найдено")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@api_router.post("/devices/{device_id}/swipe")
async def swipe_device(
    device_id: str,
    req: SwipeRequest,
    actor: str = Depends(require_api),
) -> dict[str, Any]:
    """Свайп от (x1, y1) до (x2, y2)."""
    await _check_device_and_rate_limit(device_id)
    try:
        await actions.swipe(device_id, req.x1, req.y1, req.x2, req.y2, ms=req.ms, actor=actor)
        return {"ok": True, "status": "ok"}
    except (DeviceOffline, AdbTimeout, AdbError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Устройство '{device_id}' не найдено")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@api_router.post("/devices/{device_id}/text")
async def type_text_device(
    device_id: str,
    req: TextRequest,
    actor: str = Depends(require_api),
) -> dict[str, Any]:
    """Ввод текста на устройстве."""
    await _check_device_and_rate_limit(device_id)
    try:
        await actions.type_text(device_id, req.text, actor=actor)
        return {"ok": True, "status": "ok"}
    except (DeviceOffline, AdbTimeout, AdbError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Устройство '{device_id}' не найдено")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@api_router.post("/devices/{device_id}/key")
async def key_device(
    device_id: str,
    req: KeyRequest,
    actor: str = Depends(require_api),
) -> dict[str, Any]:
    """Нажатие аппаратной клавиши."""
    await _check_device_and_rate_limit(device_id)
    try:
        await actions.key(device_id, req.name, actor=actor)
        return {"ok": True, "status": "ok"}
    except (DeviceOffline, AdbTimeout, AdbError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Устройство '{device_id}' не найдено")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@api_router.post("/devices/{device_id}/power")
async def power_device(
    device_id: str,
    req: PowerRequest,
    actor: str = Depends(require_api),
) -> dict[str, Any]:
    """Управление питанием устройства (wake, sleep, reboot, shutdown)."""
    await _check_device_and_rate_limit(device_id)
    if req.action in ("reboot", "shutdown") and not req.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Для действия '{req.action}' требуется подтверждение (confirm=true)",
        )
    try:
        await actions.power(device_id, req.action, actor=actor)
        return {"ok": True, "status": "ok"}
    except (DeviceOffline, AdbTimeout, AdbError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Устройство '{device_id}' не найдено")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@api_router.post("/devices/{device_id}/open_app")
async def open_app_device(
    device_id: str,
    req: OpenAppRequest,
    actor: str = Depends(require_api),
) -> dict[str, Any]:
    """Запуск приложения по пакету."""
    await _check_device_and_rate_limit(device_id)
    try:
        await actions.open_app(device_id, req.package, actor=actor)
        return {"ok": True, "status": "ok"}
    except (DeviceOffline, AdbTimeout, AdbError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Устройство '{device_id}' не найдено")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@api_router.get("/devices/{device_id}/apps")
async def get_apps_device(
    device_id: str,
    actor: str = Depends(require_api),
) -> list[str]:
    """Список установленных приложений."""
    await _check_device_and_rate_limit(device_id)
    try:
        return await actions.list_apps(device_id, actor=actor)
    except (DeviceOffline, AdbTimeout, AdbError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Устройство '{device_id}' не найдено")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@api_router.get("/devices/{device_id}/ui")
async def get_ui_device(
    device_id: str,
    actor: str = Depends(require_api),
) -> list[dict[str, Any]]:
    """Дамп иерархии UI элементов."""
    await _check_device_and_rate_limit(device_id)
    try:
        return await actions.ui_dump(device_id, actor=actor)
    except (DeviceOffline, AdbTimeout, AdbError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Устройство '{device_id}' не найдено")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@api_router.get("/audit")
async def get_audit_log(
    limit: int = Query(default=100, ge=1, le=1000),
    actor: str = Depends(require_api),
) -> list[dict[str, Any]]:
    """Журнал аудита последних действий."""
    try:
        aud = audit_module.get_audit()
        return await aud.recent(limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Ошибка чтения аудита: {exc}")
