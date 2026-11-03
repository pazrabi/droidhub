"""Живое видео экрана: один процесс `adb exec-out screenrecord` на устройство, раздаётся подписчикам."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from hub.devices.adb import _get_adb_path
from hub.devices.h264 import AnnexBSplitter, codec_string

logger = logging.getLogger(__name__)

TIME_LIMIT_S = 180  # потолок screenrecord; по истечении процесс перезапускается
IDLE_FLUSH_S = 0.03
QUEUE_SIZE = 400
GOP_LIMIT = 300  # кадров от последнего ключевого; больше не держим
RESTART_DELAY_S = 0.3


@dataclass
class Frame:
    is_key: bool
    data: bytes


@dataclass
class StreamInfo:
    width: int
    height: int
    codec: str = ""
    sps: bytes = b""
    pps: bytes = b""


@dataclass
class StreamSession:
    """Процесс записи экрана одного устройства и его подписчики."""

    device_id: str
    serial: str
    width: int
    height: int
    bit_rate: int = 2_500_000
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    info: StreamInfo = field(init=False)
    _gop: list[Frame] = field(default_factory=list)
    _task: asyncio.Task | None = None
    _proc: asyncio.subprocess.Process | None = None

    def __post_init__(self) -> None:
        self.info = StreamInfo(self.width, self.height)

    def subscribe(self) -> asyncio.Queue:
        """Очередь элементов: ("config", StreamInfo) и ("frame", Frame); с последнего ключевого кадра."""
        queue: asyncio.Queue = asyncio.Queue(QUEUE_SIZE)
        if self.info.codec and self._gop:
            queue.put_nowait(("config", self.info))
            for frame in self._gop:
                queue.put_nowait(("frame", frame))
        self.subscribers.add(queue)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"stream-{self.device_id}")
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)
        if not self.subscribers:
            self.stop()

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._gop.clear()
        self._kill()

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

    def _publish(self, kind: str, item: Any) -> None:
        for queue in list(self.subscribers):
            if queue.full():
                # подписчик не успевает: выбрасываем очередь и даём ему всё с последнего ключевого кадра
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(("config", self.info))
                for frame in self._gop:
                    queue.put_nowait(("frame", frame))
            else:
                queue.put_nowait((kind, item))

    def _on_frames(self, units: list[tuple[bool, bytes]], splitter: AnnexBSplitter) -> None:
        for is_key, data in units:
            if is_key:
                sps, pps = splitter.sps or b"", splitter.pps or b""
                if (sps, pps) != (self.info.sps, self.info.pps):
                    self.info.sps, self.info.pps = sps, pps
                    self.info.codec = codec_string(sps) if sps else ""
                    self._publish("config", self.info)
                self._gop = []
            elif not self._gop:
                continue  # дельта-кадр до первого ключевого бесполезен
            frame = Frame(is_key, data)
            self._gop.append(frame)
            if len(self._gop) > GOP_LIMIT:  # длинный отрезок без ключевых кадров: оставляем последний ключевой
                self._gop = [f for f in self._gop if f.is_key][-1:]
            self._publish("frame", frame)

    async def _run(self) -> None:
        while self.subscribers:
            splitter = AnnexBSplitter()
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    _get_adb_path(), "-s", self.serial, "exec-out", "screenrecord",
                    "--output-format=h264", "--size", f"{self.width}x{self.height}",
                    "--bit-rate", str(self.bit_rate), "--time-limit", str(TIME_LIMIT_S), "-",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                assert self._proc.stdout is not None
                while True:
                    try:
                        chunk = await asyncio.wait_for(self._proc.stdout.read(65536), IDLE_FLUSH_S)
                    except asyncio.TimeoutError:
                        self._on_frames(splitter.flush_tail(), splitter)
                        continue
                    if not chunk:
                        break
                    self._on_frames(splitter.feed(chunk), splitter)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("stream %s failed", self.device_id)
            finally:
                self._kill()
            await asyncio.sleep(RESTART_DELAY_S)  # лимит времени вышел или процесс упал: перезапуск


class StreamManager:
    """Сессии потоков по устройствам."""

    def __init__(self) -> None:
        self._sessions: dict[str, StreamSession] = {}

    def session(self, device_id: str, serial: str, width: int, height: int) -> StreamSession:
        existing = self._sessions.get(device_id)
        if existing is not None and existing.serial == serial:
            return existing
        session = StreamSession(device_id, serial, width, height)
        self._sessions[device_id] = session
        return session

    def stop_all(self) -> None:
        for session in self._sessions.values():
            session.stop()
        self._sessions.clear()


streams = StreamManager()
