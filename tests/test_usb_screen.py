"""
Тесты для серверной обработки PNG скриншотов от USB-моста DroidHub (hub/devices/usb_screen.py).
"""

from __future__ import annotations

import struct
import zlib
from io import BytesIO
from typing import Any

import pytest
from PIL import Image, PngImagePlugin

from hub.devices.usb_screen import (
    MAX_INPUT_BYTES,
    MAX_INPUT_PIXELS,
    USBScreenshotError,
    format_usb_screenshot,
)


def _make_png_bytes(
    width: int = 1080,
    height: int = 2400,
    mode: str = "RGB",
    color: Any = "blue",
    pnginfo: PngImagePlugin.PngInfo | None = None,
    exif: bytes | None = None,
) -> bytes:
    """Генерирует реальные байты PNG заданного размера и режима."""
    img = Image.new(mode, (width, height), color=color)
    buf = BytesIO()
    save_kwargs: dict[str, Any] = {"format": "PNG"}
    if pnginfo is not None:
        save_kwargs["pnginfo"] = pnginfo
    if exif is not None:
        save_kwargs["exif"] = exif
    img.save(buf, **save_kwargs)
    return buf.getvalue()


def _make_raw_png_header(width: int, height: int, chunk_type: bytes = b"IHDR", ihdr_len: int = 13) -> bytes:
    """Создаёт заголовок PNG (сигнатура + IHDR-чанк) с произвольными размерами без реальных пикселей."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    if ihdr_len != 13:
        ihdr_data = ihdr_data[:ihdr_len]
    crc = struct.pack(">I", zlib.crc32(chunk_type + ihdr_data))
    chunk = struct.pack(">I", len(ihdr_data)) + chunk_type + ihdr_data + crc
    return sig + chunk


class TestUSBScreenshotSuccess:
    """Тесты успешного форматирования реального PNG."""

    def test_real_png_formatting(self):
        """Проверка успешной обработки реального PNG: JPEG на выходе, пропорции и исходные размеры."""
        orig_w, orig_h = 1080, 2400
        png_data = _make_png_bytes(width=orig_w, height=orig_h)

        jpeg_bytes, w, h = format_usb_screenshot(png_data, max_width=720)

        assert w == orig_w
        assert h == orig_h

        # Проверка полученного JPEG
        with Image.open(BytesIO(jpeg_bytes)) as out_img:
            assert out_img.format == "JPEG"
            assert out_img.width == 720
            assert out_img.height == 1600

    def test_aspect_ratio_preservation(self):
        """Проверка корректного сохранения пропорций для разных разрешений."""
        cases = [
            (1080, 1920, 540, 540, 960),      # 16:9 портрет
            (1920, 1080, 960, 960, 540),      # 16:9 ландшафт
            (1440, 3120, 720, 720, 1560),     # 19.5:9
            (750, 1334, 375, 375, 667),       # нечётная высота с округлением
            (1081, 2345, 720, 720, 1562),     # произвольные нечётные размеры
        ]
        for orig_w, orig_h, max_w, exp_w, exp_h in cases:
            png_data = _make_png_bytes(width=orig_w, height=orig_h)
            jpeg_bytes, ret_w, ret_h = format_usb_screenshot(png_data, max_width=max_w)
            assert (ret_w, ret_h) == (orig_w, orig_h)

            with Image.open(BytesIO(jpeg_bytes)) as out_img:
                assert out_img.format == "JPEG"
                assert out_img.width == exp_w
                assert out_img.height == exp_h

    def test_no_upscale_when_smaller_than_max_width(self):
        """Проверка отсутствия upscale, если исходная ширина меньше max_width."""
        orig_w, orig_h = 480, 800
        png_data = _make_png_bytes(width=orig_w, height=orig_h)

        jpeg_bytes, ret_w, ret_h = format_usb_screenshot(png_data, max_width=720)

        assert ret_w == orig_w
        assert ret_h == orig_h

        with Image.open(BytesIO(jpeg_bytes)) as out_img:
            assert out_img.format == "JPEG"
            assert out_img.width == orig_w
            assert out_img.height == orig_h

    def test_no_upscale_when_equal_to_max_width(self):
        """Проверка отсутствия масштабирования, если исходная ширина равна max_width."""
        orig_w, orig_h = 720, 1280
        png_data = _make_png_bytes(width=orig_w, height=orig_h)

        jpeg_bytes, ret_w, ret_h = format_usb_screenshot(png_data, max_width=720)

        assert (ret_w, ret_h) == (orig_w, orig_h)
        with Image.open(BytesIO(jpeg_bytes)) as out_img:
            assert out_img.format == "JPEG"
            assert out_img.width == 720
            assert out_img.height == 1280

    def test_max_width_zero_or_negative_keeps_original_size(self):
        """max_width <= 0 оставляет исходный размер изображения без изменений."""
        orig_w, orig_h = 600, 900
        png_data = _make_png_bytes(width=orig_w, height=orig_h)

        for mw in (0, -1, -100):
            jpeg_bytes, ret_w, ret_h = format_usb_screenshot(png_data, max_width=mw)
            assert (ret_w, ret_h) == (orig_w, orig_h)
            with Image.open(BytesIO(jpeg_bytes)) as out_img:
                assert out_img.format == "JPEG"
                assert out_img.width == orig_w
                assert out_img.height == orig_h

    def test_bytearray_and_memoryview_supported(self):
        """Поддержка bytearray и memoryview во входных данных."""
        png_data = _make_png_bytes(width=200, height=200)

        jpeg1, w1, h1 = format_usb_screenshot(bytearray(png_data), max_width=100)
        assert (w1, h1) == (200, 200)

        jpeg2, w2, h2 = format_usb_screenshot(memoryview(png_data), max_width=100)
        assert (w2, h2) == (200, 200)
        assert jpeg1 == jpeg2


class TestMetadataStripping:
    """Тесты удаления исходных метаданных из результирующего JPEG."""

    def test_metadata_and_exif_stripped(self):
        """Оригинальные текстовые метаданные и EXIF не попадают в результирующий JPEG."""
        meta = PngImagePlugin.PngInfo()
        meta.add_text("DeviceModel", "Pixel8Pro")
        meta.add_text("SecretToken", "SuperSecret12345")
        meta.add_text("Comment", "DroidHub internal capture")

        dummy_exif = b"Exif\x00\x00II*\x00\x08\x00\x00\x00\x00\x00\x00\x00\x00\x00"

        png_data = _make_png_bytes(
            width=300,
            height=400,
            pnginfo=meta,
            exif=dummy_exif,
        )

        jpeg_bytes, _, _ = format_usb_screenshot(png_data, max_width=200)

        # Проверяем, что в байтах JPEG нет секретных строк
        assert b"SuperSecret12345" not in jpeg_bytes
        assert b"Pixel8Pro" not in jpeg_bytes
        assert b"DroidHub internal capture" not in jpeg_bytes

        # Проверяем структуру метаданных через Pillow
        with Image.open(BytesIO(jpeg_bytes)) as out_img:
            assert "DeviceModel" not in out_img.info
            assert "SecretToken" not in out_img.info
            assert "Comment" not in out_img.info
            exif_obj = out_img.getexif()
            assert len(exif_obj) == 0


class TestColorModes:
    """Тесты корректной конвертации различных режимов PNG в RGB JPEG."""

    def test_rgba_png_conversion(self):
        """RGBA с альфа-каналом корректно конвертируется в RGB JPEG."""
        png_data = _make_png_bytes(width=100, height=100, mode="RGBA", color=(255, 0, 0, 128))
        jpeg_bytes, w, h = format_usb_screenshot(png_data, max_width=100)
        assert (w, h) == (100, 100)
        with Image.open(BytesIO(jpeg_bytes)) as out_img:
            assert out_img.format == "JPEG"
            assert out_img.mode == "RGB"

    def test_palette_p_png_conversion(self):
        """PNG с палитрой (P) корректно конвертируется в RGB JPEG."""
        img = Image.new("P", (120, 120))
        img.putpalette([i % 256 for i in range(768)])
        buf = BytesIO()
        img.save(buf, format="PNG")
        png_data = buf.getvalue()

        jpeg_bytes, w, h = format_usb_screenshot(png_data, max_width=60)
        assert (w, h) == (120, 120)
        with Image.open(BytesIO(jpeg_bytes)) as out_img:
            assert out_img.format == "JPEG"
            assert out_img.mode == "RGB"
            assert out_img.width == 60

    def test_grayscale_l_png_conversion(self):
        """Градации серого (L) корректно конвертируются в RGB JPEG."""
        png_data = _make_png_bytes(width=150, height=150, mode="L", color=128)
        jpeg_bytes, w, h = format_usb_screenshot(png_data, max_width=100)
        assert (w, h) == (150, 150)
        with Image.open(BytesIO(jpeg_bytes)) as out_img:
            assert out_img.format == "JPEG"
            assert out_img.mode == "RGB"


class TestCorruptedAndInvalidInputs:
    """Тесты обработки некорректных, повреждённых и не-PNG данных."""

    def test_empty_bytes_raises(self):
        """Пустой вход вызывает USBScreenshotError."""
        with pytest.raises(USBScreenshotError, match="Пустые данные"):
            format_usb_screenshot(b"", max_width=720)

    def test_non_png_random_bytes_raises(self):
        """Произвольные не-PNG данные вызывают USBScreenshotError."""
        with pytest.raises(USBScreenshotError, match="Неверная сигнатура PNG"):
            format_usb_screenshot(b"This is definitely not a PNG file header", max_width=720)

    def test_truncated_signature_raises(self):
        """Неполный заголовок (< 16 байт) вызывает USBScreenshotError."""
        with pytest.raises(USBScreenshotError, match="размер меньше минимального заголовка"):
            format_usb_screenshot(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4, max_width=720)

    def test_wrong_first_chunk_raises(self):
        """Первый чанк не IHDR вызывает USBScreenshotError."""
        bad_header = _make_raw_png_header(100, 100, chunk_type=b"IDAT")
        with pytest.raises(USBScreenshotError, match="ожидается IHDR"):
            format_usb_screenshot(bad_header, max_width=720)

    def test_invalid_ihdr_length_raises(self):
        """Длина чанка IHDR != 13 вызывает USBScreenshotError."""
        bad_header = _make_raw_png_header(100, 100, ihdr_len=10)
        with pytest.raises(USBScreenshotError, match="Неверная длина чанка IHDR"):
            format_usb_screenshot(bad_header, max_width=720)

    def test_zero_dimensions_in_header_raises(self):
        """Ширина или высота 0 в заголовке вызывает USBScreenshotError."""
        bad_w = _make_raw_png_header(0, 100)
        with pytest.raises(USBScreenshotError, match="Недопустимые размеры"):
            format_usb_screenshot(bad_w, max_width=720)

        bad_h = _make_raw_png_header(100, 0)
        with pytest.raises(USBScreenshotError, match="Недопустимые размеры"):
            format_usb_screenshot(bad_h, max_width=720)

    def test_truncated_png_after_header_raises(self):
        """PNG с валидным заголовком, но без чанков IDAT вызывает USBScreenshotError."""
        valid_png = _make_png_bytes(width=100, height=100)
        # Отрезаем всё после заголовка IHDR (33 байта)
        truncated = valid_png[:33]
        with pytest.raises(USBScreenshotError):
            format_usb_screenshot(truncated, max_width=720)

    def test_corrupted_idat_stream_raises(self):
        """Повреждённые данные сжатия IDAT вызывают USBScreenshotError."""
        valid_png = bytearray(_make_png_bytes(width=50, height=50))
        # Повреждаем тело чанка IDAT
        valid_png[45:60] = b"\xff" * 15
        with pytest.raises(USBScreenshotError):
            format_usb_screenshot(bytes(valid_png), max_width=720)

    def test_strictly_png_rejects_jpeg(self):
        """Валидный JPEG отвергается, так как требуется строго PNG."""
        img = Image.new("RGB", (100, 100), "red")
        buf = BytesIO()
        img.save(buf, format="JPEG")
        jpeg_data = buf.getvalue()

        with pytest.raises(USBScreenshotError, match="Неверная сигнатура PNG"):
            format_usb_screenshot(jpeg_data, max_width=720)

    def test_strictly_png_rejects_gif(self):
        """Валидный GIF отвергается."""
        img = Image.new("RGB", (100, 100), "green")
        buf = BytesIO()
        img.save(buf, format="GIF")
        gif_data = buf.getvalue()

        with pytest.raises(USBScreenshotError, match="Неверная сигнатура PNG"):
            format_usb_screenshot(gif_data, max_width=720)

    def test_strictly_png_rejects_webp(self):
        """Валидный WebP отвергается."""
        img = Image.new("RGB", (100, 100), "blue")
        buf = BytesIO()
        img.save(buf, format="WEBP")
        webp_data = buf.getvalue()

        with pytest.raises(USBScreenshotError, match="Неверная сигнатура PNG"):
            format_usb_screenshot(webp_data, max_width=720)


class TestLimitsAndResourceProtection:
    """Тесты лимитов на размер входа (до 8 MiB) и разрешение (до 20 Мп до декодирования)."""

    def test_payload_exceeding_8_mib_raises(self):
        """Вход размером более 8 MiB отвергается до декодирования."""
        oversized_len = MAX_INPUT_BYTES + 1
        fake_payload = b"\x00" * oversized_len
        with pytest.raises(USBScreenshotError, match="превышает лимит 8 MiB"):
            format_usb_screenshot(fake_payload, max_width=720)

    def test_payload_within_8_mib_size_check_passes(self):
        """Вход в пределах 8 MiB проходит проверку размера (падает далее на сигнатуре, если не PNG)."""
        exact_8_mib = b"\x00" * MAX_INPUT_BYTES
        with pytest.raises(USBScreenshotError, match="Неверная сигнатура PNG"):
            format_usb_screenshot(exact_8_mib, max_width=720)

    def test_pixel_limit_exceeded_before_decoding(self):
        """Разрешение более 20 мегапикселей в заголовке PNG отвергается до декодирования."""
        # 5000 x 4001 = 20_005_000 пикселей (> 20 Мп)
        header_over_20mp = _make_raw_png_header(5000, 4001)

        with pytest.raises(USBScreenshotError, match="превышает лимит в 20 мегапикселей"):
            format_usb_screenshot(header_over_20mp, max_width=720)

    def test_pixel_limit_exact_20_mp_allowed_in_header(self):
        """Разрешение ровно 20 мегапикселей проходит валидацию заголовка (не выбрасывает ошибку 20 Мп)."""
        # 4000 x 5000 = 20_000_000 пикселей (ровно 20 Мп)
        header_20mp = _make_raw_png_header(4000, 5000)

        # Упадёт на этапе декодирования из-за отсутствия IDAT, а НЕ на лимите 20 Мп
        with pytest.raises(USBScreenshotError) as exc_info:
            format_usb_screenshot(header_20mp, max_width=720)
        assert "превышает лимит" not in str(exc_info.value)

    def test_pillow_globals_not_modified(self):
        """Проверка, что глобальные параметры Pillow не изменяются."""
        initial_max_pixels = Image.MAX_IMAGE_PIXELS
        png_data = _make_png_bytes(width=100, height=100)

        format_usb_screenshot(png_data, max_width=50)

        assert Image.MAX_IMAGE_PIXELS == initial_max_pixels


class TestTypeValidationAndPILFailureHandling:
    """Тесты валидации типов параметров и перехвата внутренних сбоев Pillow."""

    def test_invalid_png_type_raises(self):
        """Некорректный тип png вызывает USBScreenshotError."""
        for invalid in (None, 12345, "string_png", [1, 2, 3], {"data": b""}):
            with pytest.raises(USBScreenshotError, match="должны быть bytes"):
                format_usb_screenshot(invalid, max_width=720)  # type: ignore

    def test_invalid_max_width_type_raises(self):
        """Некорректный тип max_width вызывает USBScreenshotError."""
        png_data = _make_png_bytes(width=100, height=100)
        for invalid in ("720", 720.5, True, False, None, [720]):
            with pytest.raises(USBScreenshotError, match="должен быть целым числом"):
                format_usb_screenshot(png_data, max_width=invalid)  # type: ignore

    def test_pil_internal_failure_wrapped_in_usb_screenshot_error(self, monkeypatch: pytest.MonkeyPatch):
        """Любой непредвиденный сбой PIL оборачивается в USBScreenshotError."""
        png_data = _make_png_bytes(width=200, height=200)

        def mock_resize(*args, **kwargs):
            raise RuntimeError("PIL simulated internal failure")

        monkeypatch.setattr(Image.Image, "resize", mock_resize)

        with pytest.raises(USBScreenshotError, match="Сбой обработки изображения через Pillow"):
            format_usb_screenshot(png_data, max_width=100)
