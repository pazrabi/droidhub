"""Identify native Windows USB transports when ADB omits the USB bus path."""

from __future__ import annotations

import json
import os
import re
import socket


def read_local_transport_types() -> dict[tuple[str, str], str]:
    # Query the same stock local ADB server as "adb devices -l". Alternative
    # server endpoints may have independent transport IDs, so skip enrichment.
    if any(os.environ.get(key) for key in (
        "ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_ADDRESS", "ANDROID_ADB_SERVER_PORT",
    )):
        return {}

    request = b"host:track-devices-proto-text"
    try:
        with socket.create_connection(("127.0.0.1", 5037), timeout=1.0) as conn:
            conn.sendall(f"{len(request):04x}".encode("ascii") + request)
            with conn.makefile("rb") as reader:
                header = re.fullmatch(rb"OKAY([0-9a-fA-F]{4})", reader.read(8))
                if header is None:
                    return {}
                size = int(header[1], 16)
                payload = reader.read(size)
    except OSError:
        return {}
    if len(payload) != size:
        return {}

    transport_types: dict[tuple[str, str], str] = {}
    snapshot = payload.decode("utf-8", errors="replace")
    for device in re.findall(r"^device \{\n(.*?)^\}", snapshot, re.M | re.S):
        serial = re.search(r'^  serial: (".*")$', device, re.M)
        transport_id = re.search(r"^  transport_id: ([1-9][0-9]*)$", device, re.M)
        transport_type = re.search(r"^  connection_type: (USB|SOCKET)$", device, re.M)
        if serial and transport_id and transport_type:
            try:
                serial_value = json.loads(serial[1])
            except ValueError:
                continue
            transport_types[(serial_value, transport_id[1])] = transport_type[1]
    return transport_types
