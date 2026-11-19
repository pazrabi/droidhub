"""
DroidHub Desktop — автономный блок данных для объединённого Парка и политики старта Hub.

Модуль реализован строго на стандартной библиотеке Python (stdlib).
Не содержит сетевых вызовов, токенов, персистентности, Keychain, GUI или геометрии.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Optional, Tuple


@dataclass(frozen=True)
class ParkDevice:
    """Неизменяемое представление устройства в объединенном парке DroidHub.

    Поля:
    - key: уникальный строковый ключ с префиксом 'usb:' или 'hub:'
    - name: имя устройства (пользовательское или имя из Hub/USB)
    - model: модель устройства (форматированная или исходная)
    - usb_serial: серийный номер USB/ADB (или None для Hub-only)
    - hub_id: идентификатор устройства на сервере Hub (или None для USB-only)
    - usb_ready: флаг готовности устройства со стороны USB/ADB (state == 'device')
    - hub_online: флаг доступности устройства со стороны Hub
    - hub_stale: флаг устаревания снимка Hub при сетевом сбое
    - battery: уровень заряда батареи (0–100 или None)
    """

    key: str
    name: str = ""
    model: str = ""
    usb_serial: Optional[str] = None
    hub_id: Optional[str] = None
    usb_ready: bool = False
    hub_online: bool = False
    hub_stale: bool = False
    battery: Optional[int] = None

    @property
    def routes(self) -> Tuple[str, ...]:
        """Кортеж меток доступных маршрутов ('USB', 'Hub') строго в этом порядке."""
        labels: list[str] = []
        if self.usb_serial is not None and str(self.usb_serial).strip():
            labels.append("USB")
        if self.hub_id is not None and str(self.hub_id).strip():
            labels.append("Hub")
        return tuple(labels)

    @property
    def is_available(self) -> bool:
        """Доступно ли устройство для выполнения операций хотя бы по одному маршруту.

        При сетевом сбое снимок Hub считается устаревшим (hub_stale=True)
        и Hub-only устройство не выдается как доступное для операций.
        При этом отсутствие или устаревание Hub не блокирует USB-маршрут.
        """
        if self.usb_ready:
            return True
        if self.hub_online and not self.hub_stale:
            return True
        return False

    @property
    def is_operable(self) -> bool:
        """Псевдоним для is_available."""
        return self.is_available

    @property
    def available_routes(self) -> Tuple[str, ...]:
        """Кортеж меток маршрутов, готовых к выполнению операций в данный момент."""
        labels: list[str] = []
        if self.usb_ready:
            labels.append("USB")
        if self.hub_online and not self.hub_stale:
            labels.append("Hub")
        return tuple(labels)


def _get_raw_value(item: Any, name: str) -> Any:
    """Извлекает значение по имени из словаря или атрибута объекта без мутации."""
    if item is None:
        return None
    if isinstance(item, Mapping):
        return item.get(name)
    val = getattr(item, name, None)
    if val is not None:
        return val
    if hasattr(item, "__getitem__"):
        try:
            return item[name]
        except (KeyError, IndexError, TypeError):
            pass
    return None


def _extract_str(item: Any, *names: str) -> Optional[str]:
    """Извлекает непустую очищенную строку из первого найденного атрибута/ключа."""
    for name in names:
        val = _get_raw_value(item, name)
        if val is not None:
            s = str(val).strip()
            if s:
                return s
    return None


def _extract_battery(item: Any, *names: str) -> Optional[int]:
    """Извлекает целочисленный процент заряда батареи или None.

    Сохраняет значение 0 как валидный уровень заряда, отсекает booleans (False/True).
    """
    for name in names:
        val = _get_raw_value(item, name)
        if val is None or isinstance(val, bool):
            continue
        if isinstance(val, (int, float)):
            try:
                return int(round(val))
            except (ValueError, OverflowError):
                continue
        if isinstance(val, str):
            cleaned = val.strip().rstrip("%")
            try:
                return int(round(float(cleaned)))
            except (ValueError, OverflowError):
                continue
    return None


def _extract_usb_ready(item: Any) -> bool:
    """Определяет готовность USB-устройства на основе is_ready или state."""
    raw_ready = _get_raw_value(item, "is_ready")
    if raw_ready is not None:
        if isinstance(raw_ready, bool):
            return raw_ready
        if isinstance(raw_ready, (int, float)):
            return bool(raw_ready)
        if isinstance(raw_ready, str):
            lower = raw_ready.strip().lower()
            if lower in ("true", "1", "yes", "device", "ready"):
                return True
            if lower in ("false", "0", "no", "offline", "unauthorized"):
                return False
        return bool(raw_ready)

    raw_state = _extract_str(item, "state")
    if raw_state is not None:
        return raw_state.strip().lower() == "device"

    return False


def _extract_hub_online(item: Any) -> bool:
    """Извлекает статус online для устройства Hub, сохраняя False."""
    raw_online = _get_raw_value(item, "online")
    if raw_online is not None:
        if isinstance(raw_online, bool):
            return raw_online
        if isinstance(raw_online, (int, float)):
            return bool(raw_online)
        if isinstance(raw_online, str):
            lower = raw_online.strip().lower()
            if lower in ("true", "1", "yes", "online"):
                return True
            if lower in ("false", "0", "no", "offline"):
                return False
        return bool(raw_online)
    return False


def _normalize_hub_to_usb(hub_to_usb: Any) -> tuple[dict[str, str], set[str]]:
    """Нормализует привязки Hub device_id -> USB serial и выявляет коллизии.

    Возвращает:
    - clean_map: словарь {hub_id: usb_serial}
    - collided_serials: множество serial, на которые ссылается более одного hub_id,
      либо имеющих неоднозначные связи.
    """
    if not hub_to_usb:
        return {}, set()

    items: list[tuple[str, str]] = []
    if isinstance(hub_to_usb, Mapping):
        for k, v in hub_to_usb.items():
            if k is not None and v is not None:
                h = str(k).strip()
                s = str(v).strip()
                if h and s:
                    items.append((h, s))
    elif isinstance(hub_to_usb, Iterable):
        for entry in hub_to_usb:
            if isinstance(entry, (tuple, list)) and len(entry) == 2:
                k, v = entry
                if k is not None and v is not None:
                    h = str(k).strip()
                    s = str(v).strip()
                    if h and s:
                        items.append((h, s))

    serial_to_hubs: dict[str, list[str]] = {}
    hub_to_serials: dict[str, list[str]] = {}
    cleaned_map: dict[str, str] = {}

    for h, s in items:
        serial_to_hubs.setdefault(s, []).append(h)
        hub_to_serials.setdefault(h, []).append(s)
        cleaned_map[h] = s

    # Коллизия: две или более Hub-цели привязаны к одному serial
    collided_serials = {s for s, hubs in serial_to_hubs.items() if len(hubs) > 1}

    # Также исключаем связь при неоднозначности одного hub_id на несколько serial
    for h, serials in hub_to_serials.items():
        if len(serials) > 1:
            for s in serials:
                collided_serials.add(s)

    return cleaned_map, collided_serials


@dataclass
class _ParsedUsb:
    serial: str
    name: Optional[str]
    model: str
    is_ready: bool
    battery: Optional[int]


@dataclass
class _ParsedHub:
    id: str
    name: str
    model: str
    online: bool
    battery: Optional[int]


def build_park_inventory(
    usb_rows: Optional[Iterable[Any]],
    hub_rows: Optional[Iterable[Any]],
    *,
    hub_to_usb: Optional[Mapping[str, str]] = None,
    hub_stale: bool = False,
) -> list[ParkDevice]:
    """Строит объединенный снимок Парка устройств (USB и Hub).

    Гарантии:
    - stdlib only, без сетевых вызовов, персистентности или сторонних библиотек.
    - входной снимок USB не мутируется.
    - удаляются повторы одного ID (дедупликация с сохранением порядка).
    - детерминированный порядок: сначала USB-устройства (с подтянутыми Hub-данными),
      затем оставшиеся Hub-only устройства.
    - объединение только при явной однозначной связи (1:1 через hub_to_usb).
    - при коллизии (несколько Hub-целей на один serial) объединение не выполняется,
      ни одна произвольная цель не выбирается.
    - значения 0 (заряд) и False (статусы) сохраняются корректно.
    - отсутствие Hub не блокирует USB.
    - Hub-only сохраняет статус, при сетевом сбое (hub_stale=True) отмечается
      устаревшим и не выдается как доступный для операций.
    """
    clean_map, collided_serials = _normalize_hub_to_usb(hub_to_usb)

    # 1. Сбор уникальных USB-устройств с сохранением порядка
    unique_usb: list[_ParsedUsb] = []
    seen_usb_serials: set[str] = set()

    if usb_rows is not None:
        for row in usb_rows:
            if row is None:
                continue
            serial = _extract_str(row, "serial")
            if not serial or serial in seen_usb_serials:
                continue
            seen_usb_serials.add(serial)

            model_display = _extract_str(row, "model_display")
            raw_model = _extract_str(row, "model")
            device_code = _extract_str(row, "device_code", "device", "product")
            model = model_display or raw_model or device_code or ""

            name = _extract_str(row, "name")
            is_ready = _extract_usb_ready(row)
            battery = _extract_battery(row, "battery")

            unique_usb.append(
                _ParsedUsb(
                    serial=serial,
                    name=name,
                    model=model,
                    is_ready=is_ready,
                    battery=battery,
                )
            )

    # 2. Сбор уникальных Hub-устройств с сохранением порядка
    unique_hub: list[_ParsedHub] = []
    seen_hub_ids: set[str] = set()

    if hub_rows is not None:
        for row in hub_rows:
            if row is None:
                continue
            hub_id = _extract_str(row, "id")
            if not hub_id or hub_id in seen_hub_ids:
                continue
            seen_hub_ids.add(hub_id)

            hub_name = _extract_str(row, "name") or hub_id
            hub_model = _extract_str(row, "model", "model_display") or ""
            online = _extract_hub_online(row)
            battery = _extract_battery(row, "battery")

            unique_hub.append(
                _ParsedHub(
                    id=hub_id,
                    name=hub_name,
                    model=hub_model,
                    online=online,
                    battery=battery,
                )
            )

    hub_by_id = {h.id: h for h in unique_hub}
    usb_by_serial = {u.serial: u for u in unique_usb}

    # 3. Вычисление однозначных привязок 1:1
    merged_usb_to_hub: dict[str, str] = {}
    merged_hub_ids: set[str] = set()

    for h_id, u_ser in clean_map.items():
        if u_ser in collided_serials:
            continue
        if u_ser in usb_by_serial and h_id in hub_by_id:
            merged_usb_to_hub[u_ser] = h_id
            merged_hub_ids.add(h_id)

    result: list[ParkDevice] = []

    # 4. Формирование списка устройств: сначала все USB (в порядке обнаружения)
    for u in unique_usb:
        if u.serial in merged_usb_to_hub:
            h = hub_by_id[merged_usb_to_hub[u.serial]]
            merged_name = u.name or h.name or ""
            merged_model = u.model or h.model or ""
            battery = h.battery if h.battery is not None else u.battery
            result.append(
                ParkDevice(
                    key=f"usb:{u.serial}",
                    name=merged_name,
                    model=merged_model,
                    usb_serial=u.serial,
                    hub_id=h.id,
                    usb_ready=u.is_ready,
                    hub_online=h.online,
                    hub_stale=bool(hub_stale),
                    battery=battery,
                )
            )
        else:
            usb_name = u.name or ""
            result.append(
                ParkDevice(
                    key=f"usb:{u.serial}",
                    name=usb_name,
                    model=u.model,
                    usb_serial=u.serial,
                    hub_id=None,
                    usb_ready=u.is_ready,
                    hub_online=False,
                    hub_stale=False,
                    battery=u.battery,
                )
            )

    # 5. Добавление оставшихся Hub-only устройств (в порядке обнаружения)
    for h in unique_hub:
        if h.id in merged_hub_ids:
            continue
        result.append(
            ParkDevice(
                key=f"hub:{h.id}",
                name=h.name,
                model=h.model,
                usb_serial=None,
                hub_id=h.id,
                usb_ready=False,
                hub_online=h.online,
                hub_stale=bool(hub_stale),
                battery=h.battery,
            )
        )

    return result


def resolve_hub_startup_url(
    saved_url: Optional[str],
    *,
    connect_on_startup: bool,
    default_url: Optional[str] = "",
) -> Optional[str]:
    """Определяет URL для подключения к Hub при старте приложения.

    Чистая функция без сетевых запросов или обращений к Keychain.
    - При connect_on_startup=False возвращает None.
    - При connect_on_startup=True использует сохранённый адрес (saved_url)
      или переданный доверенный HTTPS-адрес продукта (default_url).
    - При пустых обоих возвращает None.
    """
    if not connect_on_startup:
        return None

    saved = (saved_url or "").strip()
    if saved:
        return saved

    default = (default_url or "").strip()
    if default:
        return default

    return None


__all__ = [
    "ParkDevice",
    "build_park_inventory",
    "resolve_hub_startup_url",
]
