"""Живое видео: разбор H.264, размеры, защита WebSocket и сквозной поток через подставной adb."""
from __future__ import annotations

import json
import os
import stat

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hub.api import stream as stream_api
from hub.config import DeviceConfig
from hub.devices import actions
from hub.devices import stream as stream_mod
from hub.devices.h264 import START_CODE, AnnexBSplitter, codec_string
from hub.devices.manager import DeviceManager

SPS = bytes([0x67, 0x42, 0xC0, 0x1F, 0xAA])
PPS = bytes([0x68, 0xCE, 0x3C, 0x80])
IDR = bytes([0x65, 0x88, 0x84, 0x00, 0x10])
SLICE = bytes([0x41, 0x9A, 0x24, 0x6C])
SEI = bytes([0x06, 0x05, 0x01])


def annexb(*nals: bytes) -> bytes:
    return b"".join(START_CODE + n for n in nals)


def test_codec_string_from_sps() -> None:
    assert codec_string(SPS) == "avc1.42c01f"


def test_splitter_groups_key_frame_and_drops_sei() -> None:
    sp = AnnexBSplitter()
    units = sp.feed(annexb(SPS, PPS, IDR, SEI, SLICE, SLICE))
    # последний NAL ждёт следующего стартового кода
    assert units == [(True, annexb(SPS, PPS, IDR)), (False, annexb(SLICE))]
    assert sp.flush_tail() == [(False, annexb(SLICE))]


def test_splitter_handles_chunk_boundaries_and_3byte_start_codes() -> None:
    stream = annexb(SPS, PPS, IDR, SLICE) + b"\x00\x00\x01" + SLICE
    one_shot = AnnexBSplitter().feed(stream)
    sp = AnnexBSplitter()
    pieces = []
    for i in range(0, len(stream), 3):
        pieces += sp.feed(stream[i:i + 3])
    assert pieces == one_shot
    assert [k for k, _ in pieces] == [True, False]


def test_splitter_does_not_flush_unfinished_key_frame() -> None:
    sp = AnnexBSplitter()
    assert sp.feed(annexb(SPS, PPS, IDR)) == []
    assert sp.flush_tail() == []  # ключевой кадр может быть оборван
    assert sp.flush_tail(allow_key=True) == [(True, annexb(SPS, PPS, IDR))]


def test_splitter_never_flushes_unfinished_parameter_sets() -> None:
    """Регресс: SPS, пришедший кусками, нельзя выдавать по простою обрезанным."""
    sp = AnnexBSplitter()
    assert sp.feed(START_CODE + SPS[:2]) == []
    assert sp.flush_tail() == []
    assert sp.flush_tail(allow_key=True) == []
    assert sp.sps is None
    assert sp.feed(SPS[2:] + annexb(PPS, IDR)) == []
    assert sp.sps == SPS and sp.pps == PPS


def test_splitter_detects_truncated_flush() -> None:
    sp = AnnexBSplitter()
    sp.feed(annexb(SPS, PPS, IDR, SLICE))
    sp.flush_tail()  # выдали slice по простою
    sp.feed(b"\x11\x22\x33")  # а это его продолжение, не стартовый код
    assert sp.corrupt


def test_splitter_flush_then_new_start_code_is_fine() -> None:
    sp = AnnexBSplitter()
    sp.feed(annexb(SPS, PPS, IDR, SLICE))
    sp.flush_tail()
    sp.feed(START_CODE + SLICE)
    assert not sp.corrupt


def test_splitter_skips_delta_before_parameter_sets() -> None:
    assert AnnexBSplitter().feed(annexb(IDR, SLICE, SLICE)) == [(False, annexb(SLICE))]


def test_stream_size_keeps_aspect_and_even_values() -> None:
    assert stream_api.stream_size(540, 1080, 2400) == (540, 1200)
    assert stream_api.stream_size(10_000, 1080, 2400)[0] == 1080
    w, h = stream_api.stream_size(541, 720, 1600)
    assert w % 2 == 0 and h % 2 == 0


@pytest.fixture
def stream_app(monkeypatch: pytest.MonkeyPatch, tmp_path) -> FastAPI:
    mgr = DeviceManager([DeviceConfig(id="realme", name="R", host="100.64.0.1", port=5555)])
    actions.set_manager(mgr)

    async def fake_awake(device_id: str, *, actor: str = "system") -> bool:
        return True

    async def fake_size(device_id: str) -> tuple[int, int]:
        return 720, 1600

    monkeypatch.setattr(actions, "ensure_awake", fake_awake)
    monkeypatch.setattr(stream_api, "get_screen_size", fake_size)

    # подставной "adb": печатает готовый поток H.264 и закрывается
    payload = tmp_path / "payload.bin"
    payload.write_bytes(annexb(SPS, PPS, IDR, SLICE, SLICE))
    fake_adb = tmp_path / "adb"
    fake_adb.write_text(f"#!/bin/sh\ncat {payload}\nsleep 5\n")
    fake_adb.chmod(fake_adb.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(stream_mod, "_get_adb_path", lambda: str(fake_adb))

    app = FastAPI()
    app.include_router(stream_api.stream_router)
    yield app
    stream_mod.streams.stop_all()


def _allow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stream_api, "require_api", lambda conn: "tester")


def test_stream_requires_auth(stream_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(conn):
        raise HTTPException(401)

    monkeypatch.setattr(stream_api, "require_api", deny)
    with TestClient(stream_app) as client, pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/devices/realme/stream"):
            pass
    assert exc.value.code == 1008


def test_stream_rejects_foreign_origin(stream_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    _allow(monkeypatch)
    with TestClient(stream_app) as client, pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/devices/realme/stream", headers={"origin": "https://evil.example"}):
            pass
    assert exc.value.code == 1008


def test_stream_unknown_device(stream_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    _allow(monkeypatch)
    with TestClient(stream_app) as client, pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/devices/nope/stream"):
            pass
    assert exc.value.code == 4404


def test_stream_delivers_config_and_frames(stream_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    _allow(monkeypatch)
    with TestClient(stream_app) as client:
        with client.websocket_connect(
            "/api/devices/realme/stream?width=540", headers={"origin": "https://molokohub.site"}
        ) as ws:
            config = json.loads(ws.receive_text())
            assert config["type"] == "config"
            assert config["codec"] == "avc1.42c01f"
            assert config["sps"] and config["pps"]
            assert (config["width"], config["height"]) == (540, 1200)
            assert (config["real_width"], config["real_height"]) == (720, 1600)
            key = ws.receive_bytes()
            assert key[0] == 1 and key[1:] == annexb(SPS, PPS, IDR)
            delta = ws.receive_bytes()
            assert delta[0] == 0 and delta[1:] == annexb(SLICE)
