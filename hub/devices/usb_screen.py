"""
Серверная обработка скриншотов PNG, полученных от авторизованного USB-моста DroidHub.

Проверяет входные данные, формат PNG, лимиты размера и разрешения, конвертирует
в чистый JPEG с сохранением пропорций без метаданных и возвращает кортеж:
(jpeg_bytes, orig_width, orig_height).
"""

from __future__ import annotations

import struct
from io import BytesIO

from PIL import Image

__all__ = [
    "MAX_INPUT_BYTES",
    "MAX_INPUT_PIXELS",
    "USBScreenshotError",
    "format_usb_screenshot",
]

MAX_INPUT_BYTES: int = 8 * 1024 * 1024  # 8 MiB
MAX_INPUT_PIXELS: int = 20_000_000  # 20 мегапикселей
PNG_SIGNATURE: bytes = b"\x89PNG\r\n\x1a\n"
JPEG_QUALITY: int = 80


class USBScreenshotError(Exception):
    """Ошибка валидации или обработки PNG скриншота от USB-моста."""


def _validate_png_header(png: bytes) -> tuple[int, int]:
    """
    Проверяет структуру заголовка PNG и возвращает (width, height) до декодирования.
    Выбрасывает USBScreenshotError при любых отклонениях от формата PNG.
    """
    if len(png) < 16:
        raise USBScreenshotError("Повреждённые данные PNG: размер меньше минимального заголовка")

    if not png.startswith(PNG_SIGNATURE):
        raise USBScreenshotError("Неверная сигнатура PNG: ожидается формат PNG")

    chunk_len, chunk_type = struct.unpack(">I4s", png[8:16])
    if chunk_type != b"IHDR":
        raise USBScreenshotError(f"Неверный заголовок PNG: первый чанк {chunk_type!r}, ожидается IHDR")

    if chunk_len != 13:
        raise USBScreenshotError(f"Неверная длина чанка IHDR: {chunk_len} (ожидается 13)")

    if len(png) < 33:
        raise USBScreenshotError("Повреждённые данные PNG: неполный чанк IHDR")

    width, height = struct.unpack(">II", png[16:24])
    if width <= 0 or height <= 0:
        raise USBScreenshotError(f"Недопустимые размеры изображения в заголовке PNG: {width}x{height}")

    if width * height > MAX_INPUT_PIXELS:
        raise USBScreenshotError(
            f"Разрешение изображения {width}x{height} ({width * height} пикселей) "
            f"превышает лимит в 20 мегапикселей ({MAX_INPUT_PIXELS})"
        )

    return width, height


def format_usb_screenshot(png: bytes, max_width: int) -> tuple[bytes, int, int]:
    """
    Обрабатывает входящий PNG от USB-моста:
    - проверяет лимит 8 MiB;
    - строго проверяет сигнатуру и заголовок PNG;
    - проверяет лимит разрешения до 20 мегапикселей до декодирования;
    - масштабирует до max_width без upscale с сохранением пропорций;
    - конвертирует в JPEG качества 80 без исходных метаданных;
    - возвращает (jpeg_bytes, orig_width, orig_height).
    """
    if not isinstance(png, (bytes, bytearray, memoryview)):
        raise USBScreenshotError("Входные данные скриншота должны быть bytes")

    if isinstance(png, (bytearray, memoryview)):
        png = bytes(png)

    if len(png) == 0:
        raise USBScreenshotError("Пустые данные скриншота")

    if len(png) > MAX_INPUT_BYTES:
        raise USBScreenshotError(
            f"Размер входящего скриншота ({len(png)} байт) превышает лимит 8 MiB ({MAX_INPUT_BYTES} байт)"
        )

    if not isinstance(max_width, int) or isinstance(max_width, bool):
        raise USBScreenshotError("Параметр max_width должен быть целым числом")

    # Валидация заголовка и проверка лимита пикселей до декодирования
    hdr_width, hdr_height = _validate_png_header(png)

    try:
        with Image.open(BytesIO(png)) as raw_img:
            if raw_img.format != "PNG":
                raise USBScreenshotError(f"Неверный формат изображения PIL: {raw_img.format}, ожидается PNG")

            orig_width, orig_height = raw_img.size
            if orig_width <= 0 or orig_height <= 0:
                raise USBScreenshotError(f"Недопустимые размеры изображения: {orig_width}x{orig_height}")

            if orig_width * orig_height > MAX_INPUT_PIXELS:
                raise USBScreenshotError(
                    f"Разрешение {orig_width}x{orig_height} ({orig_width * orig_height} пикселей) "
                    f"превышает лимит 20 Мп"
                )

            # Декодируем пиксели для проверки целостности сжатых данных (IDAT)
            raw_img.load()

            img: Image.Image = raw_img

            # Масштабирование без upscale (сохраняя пропорции)
            if max_width > 0 and orig_width > max_width:
                ratio = max_width / float(orig_width)
                new_height = int(round(orig_height * ratio))
                img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)

            # Конвертация в RGB (например, из RGBA/P/L)
            if img.mode != "RGB":
                img = img.convert("RGB")

            # Очистка исходных метаданных (комментарии, EXIF, ICC-профиль и т.д.)
            img.info.clear()

            out_buf = BytesIO()
            img.save(out_buf, format="JPEG", quality=JPEG_QUALITY)
            jpeg_bytes = out_buf.getvalue()

            return jpeg_bytes, orig_width, orig_height
    except USBScreenshotError:
        raise
    except Exception as exc:
        raise USBScreenshotError(f"Сбой обработки изображения через Pillow: {exc}") from exc
