"""Контроллер настольного приложения DroidHub: координация ADB, scrcpy и фоновых задач."""

from __future__ import annotations

import logging
import threading
from typing import Callable

from desktop.adb_client import AdbClient, AdbClientError, DeviceInfo
from desktop.binaries import BinaryInfo, find_adb, find_scrcpy, get_installation_guide
from desktop.scrcpy_manager import ScrcpyManager


logger = logging.getLogger(__name__)


class AppController:
    """Главный контроллер логики настольного контроллера DroidHub.

    Обеспечивает:
    - Неблокирующее фоновое обновление списка устройств
    - Защиту от автоматического запуска сетевых ADB-устройств
    - Проверку готовности устройства перед запуском scrcpy
    - Синхронизацию статусов и корректное завершение
    """

    def __init__(
        self,
        custom_adb_path: str | None = None,
        custom_scrcpy_path: str | None = None,
    ) -> None:
        self.adb_info: BinaryInfo = find_adb(custom_adb_path)
        self.scrcpy_info: BinaryInfo = find_scrcpy(custom_scrcpy_path)

        adb_bin = self.adb_info.path if self.adb_info.found else "adb"
        scrcpy_bin = self.scrcpy_info.path if self.scrcpy_info.found else "scrcpy"

        self.adb_client = AdbClient(adb_bin)
        self.scrcpy_manager = ScrcpyManager(scrcpy_bin)

        self._devices: list[DeviceInfo] = []
        self._devices_lock = threading.Lock()
        self._is_refreshing = False
        self._refresh_lock = threading.Lock()

    @property
    def devices(self) -> list[DeviceInfo]:
        """Возвращает текущую копию списка обнаруженных устройств."""
        with self._devices_lock:
            return list(self._devices)

    def get_device(self, serial: str) -> DeviceInfo | None:
        """Находит устройство по серийному номеру."""
        with self._devices_lock:
            for dev in self._devices:
                if dev.serial == serial:
                    return dev
        return None

    def refresh_binaries(
        self,
        custom_adb: str | None = None,
        custom_scrcpy: str | None = None,
    ) -> None:
        """Повторно ищет исполняемые файлы adb и scrcpy."""
        self.adb_info = find_adb(custom_adb)
        self.scrcpy_info = find_scrcpy(custom_scrcpy)

        if self.adb_info.found and self.adb_info.path:
            self.adb_client.adb_path = self.adb_info.path
        if self.scrcpy_info.found and self.scrcpy_info.path:
            self.scrcpy_manager.set_scrcpy_path(self.scrcpy_info.path)

    def refresh_devices_sync(self) -> tuple[list[DeviceInfo], str | None]:
        """Синхронно обновляет список устройств.

        Возвращает: (список_устройств, сообщение_об_ошибке | None)
        """
        # Сначала проверяем наличие ADB
        if not self.adb_info.found:
            return [], "Утилита ADB не найдена. Проверьте установку или укажите путь."

        try:
            new_devices = self.adb_client.list_devices()
            current_serials = {dev.serial for dev in new_devices}

            # Обрабатываем физически отключенные устройства
            self.scrcpy_manager.handle_device_disconnection(current_serials)
            # Обновляем состояния завершившихся процессов
            self.scrcpy_manager.poll_sessions()

            with self._devices_lock:
                self._devices = new_devices

            return new_devices, None
        except AdbClientError as err:
            logger.warning("Ошибка при получении списка устройств: %s", err)
            return [], str(err)
        except Exception as exc:
            logger.exception("Непредвиденная ошибка при обновлении устройств")
            return [], f"Ошибка обновления: {exc}"

    def refresh_devices_async(
        self,
        on_success: Callable[[list[DeviceInfo]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> bool:
        """Запускает неблокирующее обновление устройств в фоновом потоке.

        Возвращает True, если поток запущен, или False, если обновление уже выполняется.
        """
        with self._refresh_lock:
            if self._is_refreshing:
                return False
            self._is_refreshing = True

        def _worker() -> None:
            try:
                devices, err = self.refresh_devices_sync()
                if err:
                    if on_error:
                        on_error(err)
                else:
                    if on_success:
                        on_success(devices)
            finally:
                with self._refresh_lock:
                    self._is_refreshing = False

        thread = threading.Thread(target=_worker, name="AdbRefreshWorker", daemon=True)
        thread.start()
        return True

    def start_device_scrcpy(self, serial: str) -> tuple[bool, str]:
        """Запускает окно scrcpy для указанного устройства с проверками готовности."""
        if not self.scrcpy_info.found:
            return False, (
                "Утилита scrcpy не найдена на компьютере. "
                "Установите scrcpy (см. раздел 'Справка')."
            )

        dev = self.get_device(serial)
        if not dev:
            return False, f"Устройство '{serial}' не найдено среди подключенных."

        if not dev.is_ready:
            if dev.state.lower() == "unauthorized":
                return False, (
                    f"Устройство {dev.display_name} не авторизовано!\n"
                    "Разблокируйте экран телефона и подтвердите запрос "
                    "«Разрешить отладку по USB с этого компьютера»."
                )
            if dev.state.lower() == "offline":
                return False, (
                    f"Устройство {dev.display_name} находится в офлайне.\n"
                    "Проверьте USB-кабель или переподключите устройство."
                )
            return False, (
                f"Невозможно запустить scrcpy: устройство в состоянии '{dev.state_display}'."
            )

        ok, err = self.scrcpy_manager.start(serial, title=dev.model_display)
        if ok:
            return True, f"Окно scrcpy успешно открыто для {dev.display_name}."
        return False, err or "Неизвестная ошибка при запуске scrcpy."

    def stop_device_scrcpy(self, serial: str) -> tuple[bool, str]:
        """Закрывает окно scrcpy для указанного устройства."""
        ok, err = self.scrcpy_manager.stop(serial)
        if ok:
            return True, f"Окно scrcpy для '{serial}' закрыто."
        return False, err or "Не удалось закрыть окно scrcpy."

    def start_selected_usb_devices(self, serials: list[str]) -> tuple[int, list[str]]:
        """Запускает scrcpy для выбранных USB-устройств.

        Строгое правило: сетевые ADB-устройства никогда не открываются автоматически!
        Возвращает: (количество_успешно_запущенных, список_сообщений_и_предупреждений)
        """
        messages: list[str] = []
        started_count = 0

        if not serials:
            messages.append("Не выбрано ни одного устройства.")
            return 0, messages

        for serial in serials:
            dev = self.get_device(serial)
            if not dev:
                messages.append(f"Устройство '{serial}' не найдено.")
                continue

            # Защита: сетевые ADB-устройства не открывать автоматически!
            if dev.is_network:
                messages.append(
                    f"Пропуск {dev.display_name}: сетевые ADB-устройства не открываются автоматически."
                )
                continue

            # Проверка готовности
            if not dev.is_ready:
                messages.append(
                    f"Пропуск {dev.display_name}: статус '{dev.state_display}' (не готов)."
                )
                continue

            # Пропуск уже запущенных
            if self.scrcpy_manager.is_running(serial):
                messages.append(f"{dev.display_name}: окно scrcpy уже запущено.")
                continue

            ok, err = self.start_device_scrcpy(serial)
            if ok:
                started_count += 1
                messages.append(f"Запущено окно для {dev.display_name}.")
            else:
                messages.append(f"Ошибка для {dev.display_name}: {err}")

        return started_count, messages

    def stop_all_scrcpy(self) -> int:
        """Останавливает все запущенные окна scrcpy."""
        return self.scrcpy_manager.stop_all()

    def get_instructions(self) -> str:
        """Возвращает справочный текст с инструкциями."""
        return get_installation_guide()

    def shutdown(self) -> None:
        """Корректно останавливает все процессы при закрытии приложения."""
        self.stop_all_scrcpy()
