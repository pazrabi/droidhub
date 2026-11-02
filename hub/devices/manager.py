from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any

from hub.config import DeviceConfig
from hub.devices.adb import (
    AdbError,
    AdbTimeout,
    DeviceOffline,
    adb_shell,
    connect,
)


@dataclass
class DeviceStatus:
    id: str
    name: str
    online: bool = False
    battery: int | None = None
    temperature: float | None = None
    screen_on: bool | None = None
    last_seen: float | None = None
    error: str | None = None


def parse_battery_dumpsys(dumpsys_output: str) -> tuple[int | None, float | None]:
    """
    Парсит уровень заряда (%) и температуру (°C) из вывода dumpsys battery.
    В Android температура возвращается в десятых долях градуса Цельсия (например, 295 -> 29.5°C).
    """
    if not dumpsys_output:
        return None, None

    battery: int | None = None
    temperature: float | None = None

    level_match = re.search(r"\blevel:\s*(\d+)", dumpsys_output, re.IGNORECASE)
    if level_match:
        battery = int(level_match.group(1))

    temp_match = re.search(r"\btemperature:\s*([-\d.]+)", dumpsys_output, re.IGNORECASE)
    if temp_match:
        try:
            if "." in temp_match.group(1):
                temperature = round(float(temp_match.group(1)), 1)
            else:
                temperature = round(float(temp_match.group(1)) / 10.0, 1)
        except ValueError:
            temperature = None

    return battery, temperature


def parse_screen_dumpsys(dumpsys_output: str) -> bool | None:
    """
    Определяет, включен ли экран, по выводу dumpsys power / dumpsys display.
    """
    if not dumpsys_output:
        return None

    wake_match = re.search(r"\bmWakefulness=(\w+)", dumpsys_output, re.IGNORECASE)
    if wake_match:
        val = wake_match.group(1).lower()
        if val == "awake":
            return True
        if val in ("asleep", "dozing", "dreaming"):
            return False

    state_match = re.search(r"Display Power:\s*state[=\s]+(ON|OFF)\b", dumpsys_output, re.IGNORECASE)
    if not state_match:
        state_match = re.search(r"\bstate[=\s]+(ON|OFF)\b", dumpsys_output, re.IGNORECASE)
    if state_match:
        return state_match.group(1).upper() == "ON"

    screen_state_match = re.search(r"screenState=SCREEN_STATE_(ON|OFF)\b", dumpsys_output, re.IGNORECASE)
    if screen_state_match:
        return screen_state_match.group(1).upper() == "ON"

    interactive_match = re.search(r"\bisInteractive=(true|false)\b", dumpsys_output, re.IGNORECASE)
    if interactive_match:
        return interactive_match.group(1).lower() == "true"

    return None


parse_dumpsys_battery = parse_battery_dumpsys
parse_dumpsys_screen = parse_screen_dumpsys


class DeviceManager:
    def __init__(self, devices: list[DeviceConfig] | None = None, interval: float = 30.0):
        if devices is None:
            devices = []
        self._devices: dict[str, DeviceConfig] = {d.id: d for d in devices}
        self._statuses: dict[str, DeviceStatus] = {
            d.id: DeviceStatus(id=d.id, name=d.name, online=False)
            for d in devices
        }
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._running: bool = False

    def get(self, device_id: str) -> DeviceConfig:
        """Возвращает конфигурацию устройства по ID. KeyError если не найдено."""
        if device_id not in self._devices:
            raise KeyError(f"Устройство '{device_id}' не найдено")
        return self._devices[device_id]

    def serial(self, device_id: str) -> str:
        """Возвращает adb serial (host:port) для устройства."""
        cfg = self.get(device_id)
        return f"{cfg.host}:{cfg.port}"

    def statuses(self) -> list[DeviceStatus]:
        """Возвращает закешированные статусы устройств."""
        return list(self._statuses.values())

    async def refresh(self, device_id: str, *, auto_reconnect: bool = False) -> DeviceStatus:
        """
        Опрашивает устройство и обновляет закешированный статус.
        При auto_reconnect=True и ошибке связи делает попытку adb connect.
        """
        cfg = self.get(device_id)
        target = self.serial(device_id)
        prev_status = self._statuses.get(device_id)
        last_seen = prev_status.last_seen if prev_status else None

        try:
            battery_out = await adb_shell(target, "dumpsys battery")
            battery, temp = parse_battery_dumpsys(battery_out)

            screen_on: bool | None = None
            try:
                power_out = await adb_shell(target, "dumpsys power")
                screen_on = parse_screen_dumpsys(power_out)
            except (AdbError, AdbTimeout, Exception):
                screen_on = None

            status = DeviceStatus(
                id=cfg.id,
                name=cfg.name,
                online=True,
                battery=battery,
                temperature=temp,
                screen_on=screen_on,
                last_seen=time.time(),
                error=None,
            )
        except (DeviceOffline, AdbError, AdbTimeout) as exc:
            if auto_reconnect:
                try:
                    if await connect(cfg.host, cfg.port):
                        return await self.refresh(device_id, auto_reconnect=False)
                except Exception:
                    pass

            status = DeviceStatus(
                id=cfg.id,
                name=cfg.name,
                online=False,
                battery=None,
                temperature=None,
                screen_on=None,
                last_seen=last_seen,
                error=str(exc),
            )

        self._statuses[device_id] = status
        return status

    async def check_device(self, device_id: str) -> DeviceStatus:
        """Проверяет устройство с автопереподключением при обрыве связи."""
        return await self.refresh(device_id, auto_reconnect=True)

    async def _poll_loop(self) -> None:
        """Фоновый цикл проверки каждые self._interval секунд."""
        while self._running:
            if self._devices:
                try:
                    await asyncio.gather(
                        *(self.check_device(dev_id) for dev_id in list(self._devices.keys())),
                        return_exceptions=True,
                    )
                except asyncio.CancelledError:
                    break
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break

    async def start(self) -> None:
        """Запускает фоновую периодическую проверку устройств."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Останавливает фоновую периодическую проверку устройств."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


__all__ = [
    "DeviceStatus",
    "DeviceManager",
    "parse_battery_dumpsys",
    "parse_screen_dumpsys",
    "parse_dumpsys_battery",
    "parse_dumpsys_screen",
]
