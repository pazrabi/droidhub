"""Инструмент look: компактный текстовый список элементов экрана и кеш «номер -> координаты»."""
from __future__ import annotations

import dataclasses
from typing import Any

MAX_ELEMENTS = 120
CACHE_TTL_SECONDS = 120.0

CLASS_MAP: dict[str, str] = {
    "Button": "Кнопка",
    "ImageButton": "Кнопка",
    "TextView": "Текст",
    "EditText": "Поле ввода",
    "ImageView": "Картинка",
    "CheckBox": "Флажок",
    "RadioButton": "Радиокнопка",
    "Switch": "Переключатель",
    "ProgressBar": "Прогресс",
    "SeekBar": "Ползунок",
    "Spinner": "Список",
    "ListView": "Список",
    "RecyclerView": "Список",
    "ScrollView": "Прокрутка",
    "WebView": "WebView",
    "View": "Элемент",
    "ViewGroup": "Группа",
    "FrameLayout": "Контейнер",
    "LinearLayout": "Контейнер",
    "RelativeLayout": "Контейнер",
}


@dataclasses.dataclass
class LookCacheEntry:
    timestamp: float
    indices: dict[int, tuple[int, int]]


# device_id -> последний look (для tap_index)
look_cache: dict[str, LookCacheEntry] = {}


def _is_truthy(val: Any) -> bool:
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1")
    return bool(val)


def _find_package_activity(raw_dump: Any) -> tuple[list[Any], str | None, str | None]:
    """Элементы дампа плюс пакет и активити (из заголовка дампа или из самих элементов)."""
    pkg = act = None
    if isinstance(raw_dump, dict):
        elements = raw_dump.get("elements", [])
        pkg, act = raw_dump.get("package"), raw_dump.get("activity")
    elif isinstance(raw_dump, list):
        elements = raw_dump
    else:
        elements = []

    for el in elements:
        if not isinstance(el, dict):
            continue
        if not pkg and el.get("package"):
            pkg = str(el["package"]).strip() or None
        if not act and el.get("activity"):
            act = str(el["activity"]).strip() or None
        if not pkg and el.get("resource_id") and ":id/" in str(el["resource_id"]):
            prefix = str(el["resource_id"]).split(":id/")[0]
            if "." in prefix and not prefix.startswith("android"):
                pkg = prefix
    return elements, pkg, act


def _candidate(el: dict[str, Any]) -> tuple[int, int, str, str, str] | None:
    """(cy, cx, тип, текст, флаги) для значимого элемента, иначе None."""
    text = (el.get("text") or "").strip()
    desc = (el.get("content_desc") or "").strip()
    clickable = _is_truthy(el.get("clickable"))
    scrollable = _is_truthy(el.get("scrollable"))
    checkable = _is_truthy(el.get("checkable"))
    cls = el.get("class") or ""
    editable = _is_truthy(el.get("editable")) or "EditText" in cls

    if not (text or desc or clickable or scrollable or checkable or editable):
        return None

    center = el.get("center")
    bounds = el.get("bounds")
    if isinstance(center, (list, tuple)) and len(center) >= 2:
        cx, cy = int(center[0]), int(center[1])
    elif bounds and len(bounds) >= 4:
        cx, cy = int((bounds[0] + bounds[2]) // 2), int((bounds[1] + bounds[3]) // 2)
    else:
        cx, cy = 0, 0

    if el.get("type"):
        elem_type = str(el["type"])
    else:
        short_cls = cls.split(".")[-1] if cls else ""
        elem_type = CLASS_MAP.get(short_cls, short_cls or "Элемент")

    if text and desc and text != desc:
        content = f"{text} ({desc})"
    else:
        content = text or desc
    content = " ".join(content.split())[:80]

    flags = [name for name, on in (
        ("кликабельно", clickable), ("прокрутка", scrollable), ("выбор", checkable), ("ввод", editable)
    ) if on]
    flags_str = f" ({', '.join(flags)})" if flags else ""
    return cy, cx, elem_type, content, flags_str


def _dedupe(candidates: list[tuple[int, int, str, str, str]]) -> list[tuple[str, str, str, int, int]]:
    """Убирает повторы: одинаковые элементы и пустые элементы поверх элементов с текстом."""
    seen_keys: set[tuple[str, str, str, int, int]] = set()
    centers_with_text: set[tuple[int, int]] = set()
    centers_empty: set[tuple[int, int]] = set()
    unique: list[tuple[str, str, str, int, int]] = []
    for cy, cx, elem_type, content, flags_str in candidates:
        key = (elem_type, content, flags_str, cx, cy)
        if key in seen_keys:
            continue
        if content:
            centers_with_text.add((cx, cy))
        else:
            if (cx, cy) in centers_with_text or (cx, cy) in centers_empty:
                continue
            centers_empty.add((cx, cy))
        seen_keys.add(key)
        unique.append((elem_type, content, flags_str, cx, cy))
    return unique


def build_look(raw_dump: Any) -> tuple[str, dict[int, tuple[int, int]]]:
    """Текст для модели и соответствие «номер -> центр» по дампу интерфейса."""
    elements, pkg, act = _find_package_activity(raw_dump)

    lines: list[str] = []
    if pkg and act:
        lines.append(f"Экран: {pkg} / {act}")
    elif pkg or act:
        lines.append(f"Экран: {pkg or act}")

    candidates = [c for el in elements if isinstance(el, dict) and (c := _candidate(el))]
    # сверху вниз, затем слева направо; элементы с текстом раньше пустых
    candidates.sort(key=lambda c: (c[0], c[1], 0 if c[3] else 1))
    unique = _dedupe(candidates)
    displayed = unique[:MAX_ELEMENTS]

    indices: dict[int, tuple[int, int]] = {}
    for idx, (elem_type, content, flags_str, cx, cy) in enumerate(displayed, start=1):
        indices[idx] = (cx, cy)
        text_part = f" «{content}»" if content else ""
        lines.append(f"[{idx}] {elem_type}{text_part}{flags_str} центр=({cx},{cy})")
    if len(unique) > MAX_ELEMENTS:
        lines.append(f"(показано {len(displayed)} из {len(unique)})")

    return ("\n".join(lines) if lines else "(нет значимых элементов)"), indices
