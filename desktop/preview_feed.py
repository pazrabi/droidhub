"""Переключает предпросмотр USB-устройств между видео и снимками."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from importlib.util import find_spec

from desktop.adb_client import DeviceInfo
from desktop.link_service import is_valid_usb_device
from desktop.scrcpy_video import find_preview_server, iter_h264_packets
from desktop.usb_preview import MAX_CONCURRENT_DEVICES, USBPreviewWorker, _device_connection_key
from desktop.video_preview import VideoPreviewWorker


class PreviewFeed:
    def __init__(
        self,
        adb_path: str,
        scrcpy_path: str | None,
        scrcpy_version: str | None,
    ) -> None:
        self._snapshots = USBPreviewWorker(adb_path)
        self._video: VideoPreviewWorker | None = None
        server_path = find_preview_server(scrcpy_path, scrcpy_version)
        if server_path and find_spec("av") is not None:
            self._video = VideoPreviewWorker(
                lambda device, stop: iter_h264_packets(adb_path, device, server_path, stop)
            )
        self._devices: dict[str, DeviceInfo] = {}
        self._fallback: set[str] = set()
        self._scrcpy_windows: set[str] = set()

    def update_devices(self, devices: Sequence[DeviceInfo]) -> set[str]:
        serial_counts = Counter(device.serial for device in devices)
        eligible = [
            device for device in devices
            if serial_counts[device.serial] == 1 and is_valid_usb_device(device)
        ][:MAX_CONCURRENT_DEVICES]
        previous = self._devices
        self._devices = {device.serial: device for device in eligible}
        same_connection = {
            serial for serial, old in previous.items()
            if (new := self._devices.get(serial)) is not None
            and _device_connection_key(new) == _device_connection_key(old)
        }
        self._fallback.intersection_update(same_connection)
        self._update_sources()
        return previous.keys() - same_connection

    def set_scrcpy_windows(self, serials: set[str]) -> None:
        if serials != self._scrcpy_windows:
            self._scrcpy_windows = serials
            self._fallback.difference_update(serials)
            self._update_sources()

    def has_video_for(self, serials: set[str]) -> bool:
        return self._video is not None and self._video.has_streams_for(serials)

    def _update_sources(self) -> None:
        if self._video is not None:
            self._video.update_devices([
                device for serial, device in self._devices.items()
                if serial not in self._scrcpy_windows
            ])
        self._update_snapshots()

    def _update_snapshots(self) -> None:
        self._snapshots.update_devices([
            device for serial, device in self._devices.items()
            if self._video is None or serial in self._fallback or serial in self._scrcpy_windows
        ])

    def drain(self) -> dict[str, bytes | None]:
        video_frames: dict[str, bytes] = {}
        if self._video is not None:
            fallback_before = self._fallback.copy()
            for serial, png in self._video.drain().items():
                if serial not in self._devices:
                    continue
                if png is None:
                    self._fallback.add(serial)
                else:
                    self._fallback.discard(serial)
                    video_frames[serial] = png
            if self._fallback != fallback_before:
                self._update_snapshots()
        return self._snapshots.drain() | video_frames

    def close(self) -> None:
        self._snapshots.close()
        if self._video is not None:
            self._video.close()
