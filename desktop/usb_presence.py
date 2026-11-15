"""Изолированный клиентский компонент USB-присутствия DroidHub.

Собирает стабильный снимок ID хаба для текущего списка локальных устройств
на основе сохраненных в реестре явных USB-привязок 1:1.
"""

from __future__ import annotations

from collections.abc import Sequence

from desktop.adb_client import DeviceInfo
from desktop.device_links import DeviceLinkCorruptedFileError, LinkRegistry
from desktop.link_service import is_valid_usb_device


def ready_linked_hub_ids(
    registry: LinkRegistry,
    devices: Sequence[DeviceInfo],
) -> tuple[str, ...]:
    """Возвращает отсортированный стабильный снимок ID хаба для готовых локальных USB-устройств.

    Критерии включения ID в снимок:
    1. Устройство явно привязано в переданном реестре выбранного сервера.
    2. Устройство присутствует в текущем локальном списке devices.
    3. Серийный номер устройства не имеет неоднозначности в ADB-снимке
       (ровно одно вхождение в devices).
    4. Устройство является аппаратным USB и готово к работе:
       - is_network == False и серийный номер не сетевой (без IP:порт);
       - свойство usb_info присутствует и не пусто;
       - state == 'device' и is_ready == True (не unauthorized / offline).

    Гарантии:
    - Чистая функция без внешних сетевых запросов, запуска процессов ADB, GUI
      или переключения соединений.
    - Никаких файловых изменений: реестр на диске не модифицируется (пропажа
      устройства из снимка оставляет привязку в реестре неизменной).
    - Ошибка чтения или повреждение реестра выбрасывает исключение, отличаясь
      от валидного пустого снимка ().
    - Результат отсортирован по возрастанию hub_id для воспроизводимости и стабильности.
    """
    if registry is None:
        raise TypeError("registry is required")
    if devices is None:
        raise TypeError("devices is required")

    if getattr(registry, "is_corrupted", False):
        raise DeviceLinkCorruptedFileError(
            f"Файл хранилища '{getattr(registry, 'path', 'unknown')}' поврежден и не может использоваться"
        )

    links = registry.list()

    local_by_serial: dict[str, list[DeviceInfo]] = {}
    for dev in devices:
        if dev is None or not isinstance(getattr(dev, "serial", None), str):
            continue
        local_by_serial.setdefault(dev.serial, []).append(dev)

    ready_hub_ids: list[str] = []
    for link in links:
        matches = local_by_serial.get(link.serial, [])
        # Любая неоднозначность серийного номера в ADB-снимке (дубликаты) отбрасывается
        if len(matches) != 1:
            continue

        dev = matches[0]
        if is_valid_usb_device(dev):
            ready_hub_ids.append(link.hub_id)

    return tuple(sorted(set(ready_hub_ids)))


__all__ = ["ready_linked_hub_ids"]
