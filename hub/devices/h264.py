"""Разбор потока H.264 в формате Annex B на кадры (access units) для WebCodecs."""
from __future__ import annotations

START_CODE = b"\x00\x00\x00\x01"
NAL_SLICE, NAL_IDR, NAL_SPS, NAL_PPS = 1, 5, 7, 8


def codec_string(sps: bytes) -> str:
    """Строка кодека для WebCodecs из первых байт SPS: avc1.<profile><constraints><level>."""
    return "avc1." + sps[1:4].hex()


class AnnexBSplitter:
    """
    Режет байтовый поток на кадры. Ключевой кадр = SPS + PPS + IDR (чтобы декодер мог
    начать с него), обычный кадр = один slice. SEI и прочие NAL отбрасываются.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.sps: bytes | None = None
        self.pps: bytes | None = None
        self._flushed = False  # последний NAL выдан по простою, ждём, что следующие данные начнутся со стартового кода
        self.corrupt = False   # выданный по простою кадр оказался оборванным: нужен перезапуск записи

    def _nal_ranges(self) -> list[tuple[int, int]]:
        """Границы NAL (без стартового кода) во внутреннем буфере: [(начало, конец), ...]."""
        buf = self._buf
        starts: list[int] = []
        i = buf.find(b"\x00\x00\x01")
        while i != -1:
            starts.append(i + 3)
            i = buf.find(b"\x00\x00\x01", i + 3)
        ranges = []
        for n, begin in enumerate(starts):
            end = (starts[n + 1] - 3) if n + 1 < len(starts) else len(buf)
            ranges.append((begin, end))
        return ranges

    def _consume(self, nal: bytes) -> list[tuple[bool, bytes]]:
        nal = nal.rstrip(b"\x00") if nal and nal[-1] == 0 else nal
        if not nal:
            return []
        kind = nal[0] & 0x1F
        if kind == NAL_SPS:
            self.sps = nal
        elif kind == NAL_PPS:
            self.pps = nal
        elif kind == NAL_IDR and self.sps and self.pps:
            data = START_CODE + self.sps + START_CODE + self.pps + START_CODE + nal
            return [(True, data)]
        elif kind == NAL_SLICE:
            return [(False, START_CODE + nal)]
        return []

    def feed(self, data: bytes) -> list[tuple[bool, bytes]]:
        """Добавляет данные; возвращает завершённые кадры (последний NAL ждёт следующего стартового кода)."""
        if self._flushed:
            self._flushed = False
            if not (data.startswith(b"\x00\x00\x01") or data.startswith(b"\x00\x00\x00\x01")):
                self.corrupt = True  # продолжение NAL, который мы уже выдали как целый
        self._buf += data
        ranges = self._nal_ranges()
        units: list[tuple[bool, bytes]] = []
        for begin, end in ranges[:-1]:
            units += self._consume(bytes(self._buf[begin:end]))
        if ranges:
            # оставляем в буфере только последний (возможно неполный) NAL вместе со стартовым кодом
            del self._buf[: ranges[-1][0] - 3]
        elif len(self._buf) > 3:
            del self._buf[:-3]  # мусор до первого стартового кода
        return units

    def pending_size(self) -> int:
        """Сколько байт последнего (возможно неполного) NAL уже накоплено."""
        ranges = self._nal_ranges()
        return ranges[-1][1] - ranges[-1][0] if ranges else 0

    def flush_tail(self, allow_key: bool = False) -> list[tuple[bool, bytes]]:
        """
        Выдаёт последний NAL, если поток затих (кадры идут только при изменении экрана,
        следующего стартового кода можно ждать долго). Только обычные slice-кадры: SPS, PPS и
        ключевой кадр без allow_key не трогаем, они могут быть оборваны посередине.
        """
        ranges = self._nal_ranges()
        if not ranges:
            return []
        begin, end = ranges[-1]
        kind = self._buf[begin] & 0x1F
        if kind != NAL_SLICE and not (allow_key and kind == NAL_IDR):
            return []
        units = self._consume(bytes(self._buf[begin:end]))
        self._buf.clear()
        self._flushed = True
        return units
