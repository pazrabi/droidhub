"""Автономный настольный контроллер DroidHub для управления USB Android-устройствами."""

from desktop.adb_client import AdbClient, DeviceInfo, is_network_serial, parse_adb_devices_output
from desktop.binaries import BinaryInfo, find_adb, find_scrcpy, get_installation_guide
from desktop.controller import AppController
from desktop.scrcpy_manager import ScrcpyManager, ScrcpySession

__version__ = "0.1.0"
__all__ = [
    "AdbClient",
    "DeviceInfo",
    "is_network_serial",
    "parse_adb_devices_output",
    "BinaryInfo",
    "find_adb",
    "find_scrcpy",
    "get_installation_guide",
    "AppController",
    "ScrcpyManager",
    "ScrcpySession",
]
