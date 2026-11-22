"""Вспомогательный модуль: логика выбора экранов из парковки для DroidHub Desktop.

Используется PreviewGrid для расчёта итогового набора устройств к открытию и
формирования краткого локализованного сообщения об исключённых устройствах.

Не содержит I/O, сетевого кода, зависимостей от Tk или ADB.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

# Состояния устройств, которые нельзя открывать ни при каких условиях.
_BLOCKED_STATES: frozenset[str] = frozenset(
    {"hub_only", "offline", "disconnected", "unauthorized"}
)

# Человекочитаемые причины исключения (на русском).
_REASON_HIDDEN = "скрыт фильтром"
_REASON_HUB_ONLY = "доступен только через Hub"
_REASON_UNAUTHORIZED = "подтвердите отладку USB"
_REASON_OFFLINE = "нет связи по USB"
_REASON_USB_NOT_READY = "USB не готов"

# Максимальное количество устройств в тексте сообщения; остальные сворачиваются.
_MAX_DISPLAYED = 2


def _device_label(device_id: str, names_by_id: Mapping[str, str]) -> str:
    """Возвращает отображаемое имя устройства или его ID."""
    name = names_by_id.get(device_id)
    return name if name else device_id


def _exclusion_reason(
    device_id: str,
    *,
    is_visible: bool,
    state: str | None,
    is_ready_usb: bool,
) -> str:
    """Определяет причину исключения устройства из набора к открытию.

    Если устройство скрыто фильтром, причина фильтра имеет приоритет над
    причиной состояния.  Однако проверка состояния выполняется всегда, чтобы
    гарантировать, что заблокированные состояния никогда не преодолевают фильтр.
    """
    # Если устройство скрыто фильтром — сообщаем об этом первым.
    if not is_visible:
        return _REASON_HIDDEN

    # Явно заблокированные состояния (даже если is_ready_usb=True).
    if state == "hub_only":
        return _REASON_HUB_ONLY
    if state == "unauthorized":
        return _REASON_UNAUTHORIZED
    if state in ("offline", "disconnected"):
        return _REASON_OFFLINE

    # Строка состояния отсутствует → устройство не найдено в таблице.
    if state is None:
        return _REASON_OFFLINE

    # Явно не готов по USB (caller считает авторитетным источником).
    return _REASON_USB_NOT_READY


@dataclass(frozen=True)
class ScreenOpenPlan:
    """Результат планирования открытия экранов из парковки.

    Атрибуты:
        serials:  Упорядоченный кортеж серийных номеров устройств к открытию
                  (в порядке отображения, без дубликатов).
        feedback: Локализованное сообщение для пользователя или пустая строка,
                  если все выбранные устройства открываются (или выбор пуст).
    """

    serials: tuple[str, ...]
    feedback: str


def plan_screen_open(
    selected_ids: Iterable[str],
    *,
    visible_ids: Iterable[str],
    ready_usb_ids: Iterable[str],
    state_by_id: Mapping[str, str],
    names_by_id: Mapping[str, str],
) -> ScreenOpenPlan:
    """Вычисляет план открытия выбранных экранов из парковки.

    Args:
        selected_ids:  Идентификаторы устройств, выбранных оператором.
        visible_ids:   Идентификаторы устройств, видимых в текущем фильтре
                       (задают порядок отображения).
        ready_usb_ids: Авторитетный список устройств, готовых по USB.
        state_by_id:   Отображение serial → строка состояния устройства.
        names_by_id:   Отображение serial → отображаемое имя устройства.

    Returns:
        ScreenOpenPlan с упорядоченным кортежем серийников и строкой обратной
        связи на русском языке.
    """
    # Снимаем дубликаты, сохраняя порядок первого вхождения.
    seen: set[str] = set()
    unique_selected: list[str] = []
    for sid in selected_ids:
        if sid not in seen:
            seen.add(sid)
            unique_selected.append(sid)

    if not unique_selected:
        return ScreenOpenPlan(serials=(), feedback="")

    visible_list: list[str] = list(visible_ids)
    visible_set: set[str] = set(visible_list)
    ready_usb_set: set[str] = set(ready_usb_ids)

    # Строим порядок видимых устройств для стабильной сортировки.
    visible_order: dict[str, int] = {}
    for idx, vid in enumerate(visible_list):
        if vid not in visible_order:
            visible_order[vid] = idx

    selected_set: set[str] = set(unique_selected)

    # Разбиваем выбранные устройства на eligible (к открытию) и excluded.
    eligible: list[str] = []
    excluded: list[str] = []  # те, что не пройдут

    for sid in unique_selected:
        state = state_by_id.get(sid)  # None если нет записи
        is_visible = sid in visible_set
        is_ready_usb = sid in ready_usb_set

        # Устройство открывается, только если оно:
        # 1. видимо в текущем фильтре,
        # 2. готово по USB (по авторитетному вводу caller-а),
        # 3. не имеет заблокированного состояния.
        blocked_state = state in _BLOCKED_STATES if state is not None else True

        if is_visible and is_ready_usb and not blocked_state:
            eligible.append(sid)
        else:
            excluded.append(sid)

    # Порядок eligible: visible display order (они все видимы).
    eligible_ordered = sorted(
        eligible,
        key=lambda s: visible_order.get(s, len(visible_order)),
    )

    # Нет исключений или нет вообще никаких выбранных → пустой feedback.
    if not excluded:
        return ScreenOpenPlan(serials=tuple(eligible_ordered), feedback="")

    # Если все устройства открываются (excluded пуст), мы уже вернулись выше.
    # Если все исключены → eligible_ordered пуст, но feedback нужен.

    # Стабильный порядок исключённых:
    # сначала видимые (в порядке отображения), затем скрытые/отсутствующие (по ID).
    visible_excluded = [s for s in excluded if s in visible_set]
    hidden_excluded = [s for s in excluded if s not in visible_set]

    visible_excluded_sorted = sorted(
        visible_excluded,
        key=lambda s: visible_order.get(s, len(visible_order)),
    )
    hidden_excluded_sorted = sorted(hidden_excluded)

    ordered_excluded = visible_excluded_sorted + hidden_excluded_sorted

    n_excluded = len(ordered_excluded)

    # Строим список деталей для сообщения.
    detail_items: list[str] = []
    for sid in ordered_excluded[:_MAX_DISPLAYED]:
        label = _device_label(sid, names_by_id)
        reason = _exclusion_reason(
            sid,
            is_visible=sid in visible_set,
            state=state_by_id.get(sid),
            is_ready_usb=sid in ready_usb_set,
        )
        detail_items.append(f"{label} ({reason})")

    rest = n_excluded - len(detail_items)

    details_str = ", ".join(detail_items)
    if rest > 0:
        details_str += f" и ещё {rest}"

    feedback = f"Пропущено {n_excluded}: {details_str}"

    return ScreenOpenPlan(serials=tuple(eligible_ordered), feedback=feedback)
