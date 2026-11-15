"""Публикация присутствия USB из сохранённых привязок DroidHub."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import os
from threading import Event

from desktop.adb_client import AdbClient, AdbClientError
from desktop.binaries import find_adb
from desktop.device_links import DeviceLinkError, LinkRegistry
from desktop.hub_client import HubClient, HubClientError
from desktop.link_storage import get_storage_path
from desktop.usb_battery import USBBatteryError, read_usb_battery
from desktop.usb_presence import ready_linked_hub_ids
from desktop.usb_presence_client import USBPresenceClient, USBPresenceClientError, USBPresenceConflictError
from desktop.usb_screen import USBScreenError, capture_usb_screen, tap_usb_screen


def publish_presence(
    client: USBPresenceClient,
    adb: AdbClient,
    registry: LinkRegistry,
    stop: Event,
    *,
    interval: float = 3.0,
    notify: Callable[[str], None] = print,
    read_battery: bool = False,
    control_screen: bool = False,
) -> None:
    """Обновляет полный снимок. При сбое чтения освобождает записи сессии."""
    session_id = None
    status = None
    try:
        while not stop.is_set():
            try:
                registry.load()
                device_ids = ready_linked_hub_ids(registry, adb.list_devices())
            except (AdbClientError, DeviceLinkError, OSError, UnicodeError):
                if session_id is not None:
                    _close_session(client, session_id)
                    session_id = None
                next_status = "Не удалось прочитать список USB или реестр привязок"
            else:
                try:
                    if stop.is_set():
                        break
                    if session_id is None:
                        session_id = client.open_session()
                    capabilities = {}
                    if control_screen:
                        capabilities["control_screen"] = True
                    if read_battery:
                        capabilities["read_battery"] = True
                    client.publish_snapshot(session_id, device_ids, **capabilities)
                    if read_battery and not stop.is_set():
                        _serve_battery(client, session_id, adb, registry)
                    if control_screen and not stop.is_set():
                        _serve_screen(client, session_id, adb, registry, stop)
                except USBPresenceClientError:
                    if session_id is not None:
                        _close_session(client, session_id)
                        session_id = None
                    next_status = "Не удалось передать USB-статус. Проверьте сервер и токен моста"
                else:
                    next_status = f"USB-устройств передано: {len(device_ids)}"
            if status != next_status:
                notify(next_status)
                status = next_status
            stop.wait(interval)
    finally:
        if session_id is not None:
            _close_session(client, session_id)


def _close_session(client: USBPresenceClient, session_id: str) -> None:
    try:
        client.close_session(session_id)
    except USBPresenceClientError:
        pass  # При потере связи сессия истечёт на сервере.


def _serve_battery(client: USBPresenceClient, session_id: str, adb: AdbClient, registry: LinkRegistry) -> None:
    job = client.poll_battery(session_id)
    if job is None:
        return
    request_id, device_id = job
    try:
        output = read_usb_battery(adb, registry, device_id)
    except USBBatteryError:
        output = None
    try:
        client.complete_battery(session_id, request_id, output)
    except USBPresenceConflictError:
        pass  # Запрос мог истечь, пока выполнялся локальный ADB.


def main() -> int:
    parser = argparse.ArgumentParser(description="Публикация статусов USB в DroidHub")
    parser.add_argument("url", help="HTTPS URL сервера DroidHub")
    parser.add_argument("--read-battery", action="store_true", help="Разрешить чтение заряда через USB")
    parser.add_argument("--control-screen", action="store_true", help="Разрешить снимки экрана и нажатия через USB")
    args = parser.parse_args()
    token = os.environ.pop("DROIDHUB_USB_BRIDGE_TOKEN", "")
    if not token:
        parser.error("Задайте DROIDHUB_USB_BRIDGE_TOKEN")

    adb = find_adb()
    if not adb.path:
        parser.error("ADB не найден. Задайте DROIDHUB_ADB_PATH")

    try:
        client = USBPresenceClient(args.url, token, timeout=3.0)
        registry = LinkRegistry(get_storage_path(HubClient.devices_url_for(args.url)))
    except (DeviceLinkError, HubClientError, USBPresenceClientError, ValueError):
        parser.error("Проверьте HTTPS URL и токен USB-моста")

    try:
        publish_presence(
            client, AdbClient(adb.path), registry, Event(),
            read_battery=args.read_battery, control_screen=args.control_screen,
        )
    except KeyboardInterrupt:
        pass
    return 0


def _serve_screen(
    client: USBPresenceClient, session_id: str, adb: AdbClient, registry: LinkRegistry, stop: Event,
) -> None:
    job = client.poll_screen(session_id)
    if job is None or stop.is_set():
        return
    try:
        if job["operation"] == "screenshot":
            output = capture_usb_screen(adb, registry, job["device_id"])
        else:
            tap_usb_screen(adb, registry, job["device_id"], job["x"], job["y"])
            output = b""
    except USBScreenError:
        output = None
    try:
        client.complete_screen(session_id, job["request_id"], output)
    except USBPresenceConflictError:
        pass  # Просроченное нажатие не переигрывается.


if __name__ == "__main__":
    raise SystemExit(main())
