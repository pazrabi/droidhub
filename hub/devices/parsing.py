"""Pure parsing helpers for device actions: screen state, UI tree, text chunks, JPEG."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from io import BytesIO

from PIL import Image


def split_text_for_adb_b64(text: str, max_b64_len: int = 800) -> list[str]:
    """
    Разбивает текст на части так, чтобы base64-представление каждой части
    было не больше max_b64_len символов и не разрывало многобайтовые UTF-8 символы.
    """
    if not text:
        return []
    max_bytes = (max_b64_len // 4) * 3
    chunks: list[str] = []
    current_chars: list[str] = []
    current_bytes = 0

    for ch in text:
        ch_bytes = len(ch.encode("utf-8"))
        if current_bytes + ch_bytes > max_bytes and current_chars:
            chunks.append("".join(current_chars))
            current_chars = []
            current_bytes = 0
        current_chars.append(ch)
        current_bytes += ch_bytes

    if current_chars:
        chunks.append("".join(current_chars))

    return chunks


def process_image_to_jpeg(raw_bytes: bytes, max_width: int) -> bytes:
    """Уменьшает изображение Pillow до max_width и сохраняет как JPEG."""
    img = Image.open(BytesIO(raw_bytes))
    if max_width > 0 and img.width > max_width:
        ratio = max_width / float(img.width)
        new_height = int(round(img.height * ratio))
        img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)

    if img.mode != "RGB":
        img = img.convert("RGB")

    out_buf = BytesIO()
    img.save(out_buf, format="JPEG", quality=80)
    return out_buf.getvalue()


def parse_screen_off(dumpsys_output: str) -> bool:
    """
    Определяет, выключен ли экран, по выводу
    dumpsys power | grep -E "mWakefulness|Display Power"
    с учетом формата Android 11 и 13.
    """
    if not dumpsys_output:
        return False

    # 1. Проверяем Display Power: state=OFF / ON / DOZE / etc.
    disp_match = re.search(
        r"Display Power:\s*state[=\s]+([A-Za-z0-9_]+)", dumpsys_output, re.IGNORECASE
    )
    if disp_match:
        disp_state = disp_match.group(1).upper()
        if disp_state in ("OFF", "DOZE", "DOZE_SUSPEND"):
            return True

    # 2. Проверяем mWakefulness: Awake / Asleep / Dozing / Dreaming / Sleeping
    wake_match = re.search(r"\bmWakefulness[=\s]+([A-Za-z0-9_]+)", dumpsys_output, re.IGNORECASE)
    if wake_match:
        wake_state = wake_match.group(1).upper()
        if wake_state != "AWAKE":
            return True

    # Явные подтверждения включенного экрана
    if disp_match and disp_match.group(1).upper() == "ON":
        return False
    if wake_match and wake_match.group(1).upper() == "AWAKE":
        return False

    return False


def extract_hierarchy_xml(xml_text: str) -> str:
    """Вырезает <hierarchy>…</hierarchy> из вывода adb и чистит невалидные управляющие символы."""
    start = xml_text.find("<hierarchy")
    end = xml_text.rfind("</hierarchy>")
    xml_text = xml_text[start:end + len("</hierarchy>")] if end != -1 else xml_text[start:]
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml_text)


def parse_ui_elements(xml_text: str) -> list[dict]:
    """Сжатый список элементов из XML uiautomator (пустые неинтерактивные контейнеры пропускаются)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as pe:
        raise RuntimeError(f"Не удалось получить дамп UI: ошибка разбора XML ({pe})") from pe

    elements: list[dict] = []
    for node in root.iter("node"):
        text = node.attrib.get("text", "")
        resource_id = node.attrib.get("resource-id", "") or node.attrib.get("resource_id", "")
        content_desc = node.attrib.get("content-desc", "") or node.attrib.get("content_desc", "")
        clickable = node.attrib.get("clickable", "false").lower() == "true"
        if not (text or resource_id or content_desc or clickable):
            continue

        coords = [int(v) for v in re.findall(r"-?\d+", node.attrib.get("bounds", ""))]
        if len(coords) >= 4:
            x1, y1, x2, y2 = coords[:4]
            bounds, center = [x1, y1, x2, y2], [(x1 + x2) // 2, (y1 + y2) // 2]
        else:
            bounds, center = [0, 0, 0, 0], [0, 0]

        elements.append({
            "text": text,
            "resource_id": resource_id,
            "class": node.attrib.get("class", ""),
            "content_desc": content_desc,
            "clickable": clickable,
            "bounds": bounds,
            "center": center,
        })
    return elements


def _first_visible(matches: list[dict]) -> dict | None:
    """Первый элемент с ненулевым центром, иначе первый из найденных."""
    visible = [e for e in matches if e.get("center") != [0, 0]]
    return visible[0] if visible else (matches[0] if matches else None)


def find_element(elements: list[dict], text: str | None, resource_id: str | None) -> dict | None:
    """Поиск по text/resource_id: сначала точный, потом по подстроке без учёта регистра."""
    exact = [
        e for e in elements
        if (text is None or e["text"] == text or e["content_desc"] == text)
        and (resource_id is None or e["resource_id"] == resource_id or e["resource_id"].endswith(f":id/{resource_id}"))
    ]
    found = _first_visible(exact)
    if found is not None:
        return found

    t_low = text.lower() if text else None
    r_low = resource_id.lower() if resource_id else None
    fuzzy = [
        e for e in elements
        if (t_low is None or t_low in e["text"].lower() or t_low in e["content_desc"].lower())
        and (r_low is None or r_low in e["resource_id"].lower())
    ]
    return _first_visible(fuzzy)
