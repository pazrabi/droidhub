from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from desktop.windows_adb import read_local_transport_types


def test_reads_first_typed_adb_snapshot(monkeypatch):
    monkeypatch.setattr("desktop.windows_adb.os.environ", {})
    snapshot = (
        b'device {\n  serial: "USB_A"\n  connection_type: USB\n  transport_id: 5\n}\n'
        b'device {\n  serial: "REMOTE"\n  connection_type: SOCKET\n  transport_id: 7\n}\n'
    )
    frame = b"OKAY" + f"{len(snapshot):04x}".encode() + snapshot
    with patch("desktop.windows_adb.socket.create_connection") as connect:
        conn = connect.return_value.__enter__.return_value
        conn.makefile.return_value = BytesIO(frame + b"0000")
        assert read_local_transport_types() == {("USB_A", "5"): "USB", ("REMOTE", "7"): "SOCKET"}
    conn.sendall.assert_called_once_with(b"001dhost:track-devices-proto-text")


@pytest.mark.parametrize("frame", [b"", b"FAIL0004", b"OKAYffffshort", b"OKAY-001"])
def test_does_not_use_malformed_or_truncated_snapshot(frame, monkeypatch):
    monkeypatch.setattr("desktop.windows_adb.os.environ", {})
    conn = MagicMock()
    conn.makefile.return_value = BytesIO(frame)
    with patch("desktop.windows_adb.socket.create_connection") as connect:
        connect.return_value.__enter__.return_value = conn
        assert not read_local_transport_types()


def test_does_not_cross_check_an_alternate_adb_server(monkeypatch):
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:localhost:5038")
    with patch("desktop.windows_adb.socket.create_connection") as connect:
        assert not read_local_transport_types()
        connect.assert_not_called()
