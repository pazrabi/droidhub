"""Изолированный сервис для явной привязки USB-устройств к DroidHub.

Сервис предоставляет безопасный, полностью детерминированный API для связывания
локальных аппаратных USB Android-устройств (DeviceInfo) с удаленными устройствами
сервера DroidHub (HubDevice) поверх персистентного реестра LinkRegistry.

Ключевые архитектурные и безопасностные гарантии:
- Строго явное сопоставление 1:1 исключительно по выбранным serial и hub_id.
- Запрет автоматического сопоставления по имени, модели или свойствам устройств.
- Строгая валидация локального устройства:
    * обязательно аппаратный USB (usb_info не пусто, is_network == False);
    * состояние строго 'device' (устройство готово и авторизовано, is_ready == True);
    * серийный номер не является сетевым IP:port адресом.
- Строгая проверка удаленного устройства:
    * hub_id обязан реально присутствовать в загруженном списке hub_devices.
- Защита от неоднозначностей (дубликаты serial в local_devices или hub_id в hub_devices).
- Понятные, информативные сообщения обо всех конфликтах 1:1 привязки.
- Только явная отвязка (никаких неявных или автоматических сбросов связей).
- Наблюдение статусов присутствия и готовности без сетевых вызовов и действий.
- Отсутствие сетевых запросов, ADB-команд, создания мостов и хранения учетных данных.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from desktop.adb_client import DeviceInfo, is_network_serial
from desktop.device_links import (
    DeviceLink,
    DeviceLinkConflictError,
    DeviceLinkError,
    DeviceLinkValidationError,
    LinkRegistry,
    validate_identifier,
)
from desktop.hub_client import HubDevice


# ---------------------------------------------------------------------------
# Исключения сервиса привязки
# ---------------------------------------------------------------------------


class LinkServiceError(DeviceLinkError):
    """Базовое исключение для ошибок сервиса привязки устройств."""
    pass


class LinkValidationError(LinkServiceError, DeviceLinkValidationError):
    """Ошибка валидации идентификаторов или переданных устройств."""
    pass


class LinkConflictError(LinkServiceError, DeviceLinkConflictError):
    """Конфликт связывания: серийный номер или ID хаба уже привязаны к другому объекту."""
    pass


class DeviceNotFoundError(LinkValidationError):
    """Выбранное устройство не найдено в переданном списке устройств."""
    pass


class DeviceAmbiguityError(LinkValidationError):
    """Обнаружена неоднозначность: в списке устройств найдено более одного совпадения."""
    pass


class IncompatibleDeviceError(LinkValidationError):
    """Локальное устройство не удовлетворяет строгим требованиям USB-устройства."""
    pass


# ---------------------------------------------------------------------------
# Модель наблюдения статуса привязанной пары
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkedPairStatus:
    """Статус привязанной пары (локальный USB serial <-> DroidHub ID).

    Отражает состояние присутствия и готовности каждой из сторон на основе
    переданных списков устройств, без сетевых запросов и выполнения команд.
    """

    serial: str
    hub_id: str
    local_device: DeviceInfo | None = None
    hub_device: HubDevice | None = None
    local_present: bool = False
    hub_present: bool = False
    local_ready: bool = False
    hub_online: bool = False
    status_summary: str = ""

    @property
    def link(self) -> DeviceLink:
        """Объект связи DeviceLink."""
        return DeviceLink(serial=self.serial, hub_id=self.hub_id)

    @property
    def is_operational(self) -> bool:
        """True, если обе стороны присутствуют и готовы к работе (USB готов, хаб онлайн)."""
        return self.local_ready and self.hub_online

    @property
    def is_connected(self) -> bool:
        """True, если обе стороны физически присутствуют в загруженных списках."""
        return self.local_present and self.hub_present

    def to_dict(self) -> dict[str, Any]:
        """Возвращает словарное представление статуса пары."""
        return {
            "serial": self.serial,
            "hub_id": self.hub_id,
            "local_present": self.local_present,
            "hub_present": self.hub_present,
            "local_ready": self.local_ready,
            "hub_online": self.hub_online,
            "is_operational": self.is_operational,
            "is_connected": self.is_connected,
            "status_summary": self.status_summary,
        }


# ---------------------------------------------------------------------------
# Вспомогательные функции проверки и валидации
# ---------------------------------------------------------------------------


def check_usb_device_eligibility(device: DeviceInfo) -> None:
    """Строго проверяет, что переданное локальное устройство является USB-устройством в состоянии device.

    Критерии:
    - device.is_network обязано быть False.
    - Серийный номер не должен быть сетевым адресом (is_network_serial).
    - device.usb_info обязано быть непустой строкой.
    - device.state обязано быть 'device' (регистронезависимо).
    - device.is_ready обязано быть True.

    Выбрасывает IncompatibleDeviceError при любом несоответствии.
    """
    if device.is_network or is_network_serial(device.serial):
        raise IncompatibleDeviceError(
            f"Устройство '{device.serial}' является сетевым (ADB TCP/IP), а не USB-устройством"
        )

    if not device.usb_info or not isinstance(device.usb_info, str) or not device.usb_info.strip():
        raise IncompatibleDeviceError(
            f"Устройство '{device.serial}' не опознано как USB: отсутствует или пусто свойство 'usb_info'"
        )

    norm_state = (device.state or "").strip().lower()
    if norm_state != "device" or not device.is_ready:
        state_desc = device.state_display or device.state or "неизвестно"
        raise IncompatibleDeviceError(
            f"Устройство '{device.serial}' не готово к работе: состояние '{device.state}' "
            f"({state_desc}), требуется 'device'"
        )


def is_valid_usb_device(device: DeviceInfo) -> bool:
    """Возвращает True, если устройство является подходящим USB-устройством в состоянии device."""
    try:
        check_usb_device_eligibility(device)
        validate_identifier(device.serial, name="serial")
        return True
    except (IncompatibleDeviceError, DeviceLinkValidationError):
        return False


def format_pair_summary(
    local_present: bool,
    hub_present: bool,
    local_ready: bool,
    hub_online: bool,
    local_device: DeviceInfo | None = None,
    hub_device: HubDevice | None = None,
) -> str:
    """Формирует понятное текстовое резюме состояния привязанной пары."""
    if local_present and hub_present:
        if local_ready and hub_online:
            return "Обе стороны подключены и готовы к работе"
        if local_ready and not hub_online:
            return "Локальное USB-устройство готово, но устройство DroidHub не в сети (офлайн)"
        if not local_ready and hub_online:
            state_text = local_device.state_display if local_device else "не готово"
            return f"Устройство DroidHub в сети, но локальное USB-устройство не готово ({state_text})"
        return "Локальное USB-устройство не готово и устройство DroidHub не в сети (офлайн)"
    elif local_present and not hub_present:
        if local_ready:
            return "Локальное USB-устройство готово, но устройство DroidHub отсутствует в списке"
        state_text = local_device.state_display if local_device else "не готово"
        return f"Локальное USB-устройство обнаружено ({state_text}), но устройство DroidHub отсутствует в списке"
    elif not local_present and hub_present:
        if hub_online:
            return "Устройство DroidHub в сети, но локальное USB-устройство отключено"
        return "Устройство DroidHub не в сети (офлайн), локальное USB-устройство отключено"
    else:
        return "Оба устройства отключены (не обнаружены ни локально, ни на хабе)"


def _find_single_local_device(
    serial: str, local_devices: Sequence[DeviceInfo]
) -> DeviceInfo:
    """Ищет ровно одно устройство с указанным serial в списке local_devices.

    Выбрасывает:
        DeviceNotFoundError: если устройство с таким serial не найдено.
        DeviceAmbiguityError: если найдено более одного устройства с таким serial.
    """
    matches = [d for d in local_devices if d.serial == serial]
    if not matches:
        raise DeviceNotFoundError(
            f"Локальное USB-устройство с серийным номером '{serial}' не найдено в списке подключенных устройств"
        )
    if len(matches) > 1:
        raise DeviceAmbiguityError(
            f"Неоднозначность: в списке локальных устройств найдено {len(matches)} "
            f"записей с серийным номером '{serial}'"
        )
    return matches[0]


def _find_single_hub_device(
    hub_id: str, hub_devices: Sequence[HubDevice]
) -> HubDevice:
    """Ищет ровно одно устройство с указанным ID в списке hub_devices.

    Выбрасывает:
        DeviceNotFoundError: если устройство с таким hub_id не найдено.
        DeviceAmbiguityError: если найдено более одного устройства с таким hub_id.
    """
    matches = [h for h in hub_devices if h.id == hub_id]
    if not matches:
        raise DeviceNotFoundError(
            f"Устройство DroidHub с ID '{hub_id}' не найдено в загруженном списке устройств хаба"
        )
    if len(matches) > 1:
        raise DeviceAmbiguityError(
            f"Неоднозначность: в списке устройств хаба найдено {len(matches)} "
            f"записей с ID '{hub_id}'"
        )
    return matches[0]


def _check_registry_conflicts(
    registry: LinkRegistry, serial: str, hub_id: str
) -> None:
    """Проверяет реестр на конфликты 1:1 перед созданием связи.

    Если serial или hub_id уже заняты другим партнером, выбрасывает
    понятное исключение LinkConflictError с указанием участников конфликта
    и требованием явной отвязки.
    """
    existing_hub = registry.get_hub_id(serial)
    existing_serial = registry.get_serial(hub_id)

    # Идемпотентная пара не является конфликтом
    if existing_hub == hub_id and existing_serial == serial:
        return

    if existing_hub is not None and existing_hub != hub_id:
        raise LinkConflictError(
            f"Локальное USB-устройство '{serial}' уже связано с устройством хаба '{existing_hub}'. "
            f"Сначала выполните явную отвязку (unlink) перед привязкой к '{hub_id}'."
        )

    if existing_serial is not None and existing_serial != serial:
        raise LinkConflictError(
            f"Устройство хаба '{hub_id}' уже связано с локальным USB-устройством '{existing_serial}'. "
            f"Сначала выполните явную отвязку (unlink) перед привязкой к '{serial}'."
        )


# ---------------------------------------------------------------------------
# Публичные функциональные API
# ---------------------------------------------------------------------------


def link_devices(
    serial: str,
    hub_id: str,
    local_devices: Sequence[DeviceInfo],
    hub_devices: Sequence[HubDevice],
    registry: LinkRegistry,
) -> DeviceLink:
    """Явно связывает 1:1 локальный USB serial с remote DroidHub id.

    Гарантии:
    - Сопоставление только по явным выбранным serial и hub_id.
    - Имена и модели не используются для автоматического связывания.
    - Локальный элемент строго проверяется:
        * должен присутствовать в local_devices (ровно одно совпадение);
        * не должен быть сетевым (is_network == False, серийник без порта);
        * должен иметь атрибут usb_info;
        * состояние должно быть строго 'device' (is_ready == True).
    - Удаленный элемент строго проверяется:
        * должен присутствовать в hub_devices (ровно одно совпадение).
    - Строгое отношение 1:1: при конфликте реестра выбрасывается LinkConflictError
      с понятным сообщением.
    - Никаких сетевых вызовов, ADB-команд, фоновых действий или мостов.
    """
    clean_serial = validate_identifier(serial, name="serial")
    clean_hub_id = validate_identifier(hub_id, name="hub_id")

    # 1. Поиск и строгая валидация локального USB-устройства
    local_dev = _find_single_local_device(clean_serial, local_devices)
    check_usb_device_eligibility(local_dev)

    # 2. Поиск удаленного устройства хаба
    _find_single_hub_device(clean_hub_id, hub_devices)

    # 3. Проверка конфликтов 1:1
    _check_registry_conflicts(registry, clean_serial, clean_hub_id)

    # 4. Сохранение в реестр
    try:
        return registry.link(clean_serial, clean_hub_id)
    except DeviceLinkConflictError as exc:
        raise LinkConflictError(str(exc)) from exc


link_device = link_devices


def unlink_device(
    registry: LinkRegistry,
    serial: str | None = None,
    hub_id: str | None = None,
) -> bool:
    """Явно удаляет связь 1:1 по serial, hub_id или обоим.

    Гарантирует:
    - Отвязка происходит ТОЛЬКО явно по прямому вызову.
    - Никаких автоматических или неявных отвязок при отключении устройств.

    Параметры:
        registry: экземпляр реестра связей.
        serial: серийный номер локального USB ADB (опционально, если передан hub_id).
        hub_id: идентификатор устройства DroidHub (опционально, если передан serial).

    Возвращает:
        True, если связь найдена и удалена; False, если связь не существовала.

    Исключения:
        LinkValidationError: если не указан ни serial, ни hub_id, или они некорректны.
        LinkConflictError: если переданы оба параметра, но они не связаны друг с другом.
    """
    if serial is None and hub_id is None:
        raise LinkValidationError("Для явной отвязки необходимо указать хотя бы serial или hub_id")

    clean_serial = validate_identifier(serial, name="serial") if serial is not None else None
    clean_hub_id = validate_identifier(hub_id, name="hub_id") if hub_id is not None else None

    try:
        return registry.unlink(serial=clean_serial, hub_id=clean_hub_id)
    except DeviceLinkConflictError as exc:
        raise LinkConflictError(str(exc)) from exc
    except DeviceLinkValidationError as exc:
        raise LinkValidationError(str(exc)) from exc


def inspect_linked_pairs(
    registry: LinkRegistry,
    local_devices: Sequence[DeviceInfo],
    hub_devices: Sequence[HubDevice],
) -> list[LinkedPairStatus]:
    """Возвращает текущие пары реестра и состояние присутствия каждой стороны.

    Не выполняет сетевых запросов, ADB-команд или фоновых действий.
    Состояние определяется исключительно на основе переданных списков устройств.
    """
    links = registry.list()
    results: list[LinkedPairStatus] = []

    local_by_serial: dict[str, list[DeviceInfo]] = {}
    for dev in local_devices:
        local_by_serial.setdefault(dev.serial, []).append(dev)

    hub_by_id: dict[str, list[HubDevice]] = {}
    for hdev in hub_devices:
        hub_by_id.setdefault(hdev.id, []).append(hdev)

    for link in links:
        local_matches = local_by_serial.get(link.serial, [])
        hub_matches = hub_by_id.get(link.hub_id, [])

        local_device: DeviceInfo | None = None
        local_present = False
        local_ready = False
        ambiguous_local = len(local_matches) > 1

        if len(local_matches) >= 1:
            local_device = local_matches[0]
            local_present = True
            local_ready = is_valid_usb_device(local_device) and not ambiguous_local

        hub_device: HubDevice | None = None
        hub_present = False
        hub_online = False
        ambiguous_hub = len(hub_matches) > 1

        if len(hub_matches) >= 1:
            hub_device = hub_matches[0]
            hub_present = True
            hub_online = bool(hub_device.online) and not ambiguous_hub

        summary = format_pair_summary(
            local_present=local_present,
            hub_present=hub_present,
            local_ready=local_ready,
            hub_online=hub_online,
            local_device=local_device,
            hub_device=hub_device,
        )

        if ambiguous_local:
            summary += f" [Внимание: обнаружено {len(local_matches)} локальных устройств с одинаковым serial]"
        if ambiguous_hub:
            summary += f" [Внимание: обнаружено {len(hub_matches)} устройств хаба с одинаковым ID]"

        results.append(
            LinkedPairStatus(
                serial=link.serial,
                hub_id=link.hub_id,
                local_device=local_device,
                hub_device=hub_device,
                local_present=local_present,
                hub_present=hub_present,
                local_ready=local_ready,
                hub_online=hub_online,
                status_summary=summary,
            )
        )

    return results


get_linked_pairs = inspect_linked_pairs


def inspect_pair_status(
    registry: LinkRegistry,
    local_devices: Sequence[DeviceInfo],
    hub_devices: Sequence[HubDevice],
    serial: str | None = None,
    hub_id: str | None = None,
) -> LinkedPairStatus | None:
    """Возвращает статус конкретной привязанной пары по serial или hub_id, либо None."""
    if serial is None and hub_id is None:
        raise LinkValidationError(
            "Для проверки статуса пары необходимо указать хотя бы serial или hub_id"
        )

    all_pairs = inspect_linked_pairs(registry, local_devices, hub_devices)
    for pair in all_pairs:
        if serial is not None and pair.serial != serial:
            continue
        if hub_id is not None and pair.hub_id != hub_id:
            continue
        return pair
    return None


get_pair_status = inspect_pair_status
validate_usb_device = check_usb_device_eligibility


# ---------------------------------------------------------------------------
# Класс сервиса DeviceLinkService
# ---------------------------------------------------------------------------


class DeviceLinkService:
    """Изолированный сервис для явной привязки пользователем USB-устройств к DroidHub.

    Работает поверх LinkRegistry, DeviceInfo и HubDevice без сетевых запросов,
    команд ADB, мостов и хранения учетных данных.

    Привязка осуществляется исключительно по явным serial и hub_id.
    Имена и модели устройств не используются для сопоставления.
    Отвязка производится исключительно по явному запросу.
    """

    def __init__(
        self,
        registry: LinkRegistry | None = None,
        local_devices: Sequence[DeviceInfo] | None = None,
        hub_devices: Sequence[HubDevice] | None = None,
    ) -> None:
        self._registry: LinkRegistry | None = registry
        self._local_devices: list[DeviceInfo] | None = (
            list(local_devices) if local_devices is not None else None
        )
        self._hub_devices: list[HubDevice] | None = (
            list(hub_devices) if hub_devices is not None else None
        )

    @property
    def registry(self) -> LinkRegistry | None:
        """Экземпляр реестра связей."""
        return self._registry

    @registry.setter
    def registry(self, value: LinkRegistry | None) -> None:
        self._registry = value

    @property
    def local_devices(self) -> list[DeviceInfo] | None:
        """Текущий загруженный список локальных устройств."""
        return self._local_devices

    @local_devices.setter
    def local_devices(self, devices: Sequence[DeviceInfo] | None) -> None:
        self._local_devices = list(devices) if devices is not None else None

    @property
    def hub_devices(self) -> list[HubDevice] | None:
        """Текущий загруженный список устройств хаба."""
        return self._hub_devices

    @hub_devices.setter
    def hub_devices(self, devices: Sequence[HubDevice] | None) -> None:
        self._hub_devices = list(devices) if devices is not None else None

    def set_devices(
        self,
        local_devices: Sequence[DeviceInfo],
        hub_devices: Sequence[HubDevice],
    ) -> None:
        """Обновляет списки локальных и удаленных устройств."""
        self._local_devices = list(local_devices)
        self._hub_devices = list(hub_devices)

    def _resolve_registry(self, registry: LinkRegistry | None) -> LinkRegistry:
        reg = registry if registry is not None else self._registry
        if reg is None:
            raise LinkValidationError(
                "Экземпляр LinkRegistry не предоставлен: передайте его в метод или при инициализации сервиса"
            )
        return reg

    def _resolve_devices(
        self,
        local_devices: Sequence[DeviceInfo] | None,
        hub_devices: Sequence[HubDevice] | None,
    ) -> tuple[Sequence[DeviceInfo], Sequence[HubDevice]]:
        local = local_devices if local_devices is not None else self._local_devices
        hub = hub_devices if hub_devices is not None else self._hub_devices
        if local is None:
            raise LinkValidationError(
                "Список локальных устройств local_devices не предоставлен: "
                "передайте его в метод или при инициализации сервиса"
            )
        if hub is None:
            raise LinkValidationError(
                "Список устройств хаба hub_devices не предоставлен: "
                "передайте его в метод или при инициализации сервиса"
            )
        return local, hub

    def link(
        self,
        serial: str,
        hub_id: str,
        local_devices: Sequence[DeviceInfo] | None = None,
        hub_devices: Sequence[HubDevice] | None = None,
        registry: LinkRegistry | None = None,
    ) -> DeviceLink:
        """Явно связывает 1:1 локальный USB serial с ID устройства DroidHub.

        Выполняет строгое сопоставление 1:1 по выбранным serial и hub_id:
        1. Проверяет корректность идентификаторов (без URL, пробелов и спецсимволов).
        2. Ищет serial в local_devices: должно быть ровно одно совпадение.
        3. Строго проверяет, что выбранный локальный элемент именно USB:
           - is_network == False;
           - usb_info присутствует и не пусто;
           - state == 'device' и is_ready == True.
        4. Ищет hub_id в hub_devices: ID обязан присутствовать (ровно одно совпадение).
        5. Проверяет отсутствие конфликтов 1:1 в реестре.
        6. Сохраняет связь в реестр и возвращает DeviceLink.

        Имена и модели устройств НЕ используются для сопоставления.
        """
        reg = self._resolve_registry(registry)
        local_list, hub_list = self._resolve_devices(local_devices, hub_devices)
        return link_devices(
            serial=serial,
            hub_id=hub_id,
            local_devices=local_list,
            hub_devices=hub_list,
            registry=reg,
        )

    def unlink(
        self,
        serial: str | None = None,
        hub_id: str | None = None,
        registry: LinkRegistry | None = None,
    ) -> bool:
        """Явно удаляет связь 1:1 по serial, hub_id или обоим.

        Отвязка происходит ТОЛЬКО явно. Автоматический сброс связей не выполняется.
        """
        reg = self._resolve_registry(registry)
        return unlink_device(registry=reg, serial=serial, hub_id=hub_id)

    def unlink_by_serial(
        self,
        serial: str,
        registry: LinkRegistry | None = None,
    ) -> bool:
        """Удобная явная отвязка по серийному номеру USB-устройства."""
        return self.unlink(serial=serial, registry=registry)

    def unlink_by_hub_id(
        self,
        hub_id: str,
        registry: LinkRegistry | None = None,
    ) -> bool:
        """Удобная явная отвязка по идентификатору устройства DroidHub."""
        return self.unlink(hub_id=hub_id, registry=registry)

    def get_linked_pairs(
        self,
        local_devices: Sequence[DeviceInfo] | None = None,
        hub_devices: Sequence[HubDevice] | None = None,
        registry: LinkRegistry | None = None,
    ) -> list[LinkedPairStatus]:
        """Возвращает список всех существующих связей с актуальным статусом присутствия каждой стороны.

        Не выполняет сетевых запросов и ADB-команд.
        """
        reg = self._resolve_registry(registry)
        local_list, hub_list = self._resolve_devices(local_devices, hub_devices)
        return inspect_linked_pairs(
            registry=reg,
            local_devices=local_list,
            hub_devices=hub_list,
        )

    list_linked_pairs = get_linked_pairs
    get_pairs = get_linked_pairs

    def get_pair_status(
        self,
        serial: str | None = None,
        hub_id: str | None = None,
        local_devices: Sequence[DeviceInfo] | None = None,
        hub_devices: Sequence[HubDevice] | None = None,
        registry: LinkRegistry | None = None,
    ) -> LinkedPairStatus | None:
        """Возвращает статус конкретной привязанной пары по serial или hub_id, либо None."""
        reg = self._resolve_registry(registry)
        local_list, hub_list = self._resolve_devices(local_devices, hub_devices)
        return inspect_pair_status(
            registry=reg,
            local_devices=local_list,
            hub_devices=hub_list,
            serial=serial,
            hub_id=hub_id,
        )

    def validate_usb_device(self, device: DeviceInfo) -> None:
        """Строго проверяет USB-устройство, выбрасывая IncompatibleDeviceError при несоответствии."""
        check_usb_device_eligibility(device)

    def is_valid_usb(self, device: DeviceInfo) -> bool:
        """Проверяет пригодность USB-устройства без исключений (возвращает bool)."""
        return is_valid_usb_device(device)

    def get_available_usb_devices(
        self,
        local_devices: Sequence[DeviceInfo] | None = None,
        registry: LinkRegistry | None = None,
    ) -> list[DeviceInfo]:
        """Возвращает список подходящих готовых USB-устройств, еще не привязанных в реестре."""
        reg = self._resolve_registry(registry)
        local_list = local_devices if local_devices is not None else self._local_devices
        if local_list is None:
            raise LinkValidationError("Список локальных устройств local_devices не предоставлен")

        linked_serials = {link.serial for link in reg.list()}
        return [
            dev for dev in local_list
            if dev.serial not in linked_serials and is_valid_usb_device(dev)
        ]

    def get_available_hub_devices(
        self,
        hub_devices: Sequence[HubDevice] | None = None,
        registry: LinkRegistry | None = None,
    ) -> list[HubDevice]:
        """Возвращает список устройств DroidHub, еще не привязанных в реестре."""
        reg = self._resolve_registry(registry)
        hub_list = hub_devices if hub_devices is not None else self._hub_devices
        if hub_list is None:
            raise LinkValidationError("Список устройств хаба hub_devices не предоставлен")

        linked_hub_ids = {link.hub_id for link in reg.list()}
        return [
            hdev for hdev in hub_list
            if hdev.id not in linked_hub_ids
        ]


__all__ = [
    "DeviceAmbiguityError",
    "DeviceLinkService",
    "DeviceNotFoundError",
    "IncompatibleDeviceError",
    "LinkConflictError",
    "LinkServiceError",
    "LinkValidationError",
    "LinkedPairStatus",
    "check_usb_device_eligibility",
    "format_pair_summary",
    "get_linked_pairs",
    "get_pair_status",
    "inspect_linked_pairs",
    "inspect_pair_status",
    "is_valid_usb_device",
    "link_device",
    "link_devices",
    "unlink_device",
    "validate_usb_device",
]
