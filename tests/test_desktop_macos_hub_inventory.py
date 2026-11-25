"""Unit tests for desktop/macos_hub_inventory.py using fakes."""

from __future__ import annotations

import pathlib
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from desktop.hub_client import HubClientError, HubDevice
from desktop.macos_hub_inventory import (
    CANONICAL_KEY,
    CANONICAL_URL,
    MacOSHubInventory,
    bundled_inventory,
)
from desktop.macos_hub_transport import (
    ErrorKind,
    MacOSHubTransport,
    MacOSHubTransportError,
)

VALID_SHA1 = "0123456789abcdef0123456789abcdef01234567"


class FakeTransport:
    """Fake transport implementing list_devices_payload."""

    def __init__(self, payload: bytes | None = None, exc: Exception | None = None) -> None:
        self.payload = payload if payload is not None else b"[]"
        self.exc = exc
        self.call_count = 0

    def list_devices_payload(self) -> bytes:
        self.call_count += 1
        if self.exc:
            raise self.exc
        return self.payload


# ===========================================================================
# Canonical URL key
# ===========================================================================


class TestCanonicalKey:
    def test_canonical_key_constant(self) -> None:
        assert CANONICAL_KEY == "https://droidhub.molokohub.site:443/api/devices"
        assert CANONICAL_URL == "https://droidhub.molokohub.site:443/api/devices"

    def test_inventory_canonical_attributes(self) -> None:
        inv = MacOSHubInventory(transport=FakeTransport())
        assert inv.canonical_key == "https://droidhub.molokohub.site:443/api/devices"
        assert inv.devices_url == "https://droidhub.molokohub.site:443/api/devices"


# ===========================================================================
# bundled_inventory environment selection
# ===========================================================================


class TestBundledInventorySelection:
    def test_returns_none_on_linux(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        assert bundled_inventory() is None

    def test_returns_none_on_windows(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        assert bundled_inventory() is None

    def test_returns_none_when_not_frozen(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        if hasattr(sys, "frozen"):
            monkeypatch.delattr(sys, "frozen")
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        assert bundled_inventory() is None

    def test_returns_none_when_frozen_false(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        assert bundled_inventory() is None

    def test_returns_none_when_meipass_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        if hasattr(sys, "_MEIPASS"):
            monkeypatch.delattr(sys, "_MEIPASS")
        assert bundled_inventory() is None

    def test_returns_none_when_meipass_relative(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", "relative/path/to/bundle", raising=False)
        assert bundled_inventory() is None


# ===========================================================================
# bundled_inventory asset checking & errors
# ===========================================================================


class TestBundledInventoryAssets:
    def _setup_env(self, monkeypatch: pytest.MonkeyPatch, base_dir: pathlib.Path) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(base_dir), raising=False)

    def test_returns_none_if_neither_asset_exists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        self._setup_env(monkeypatch, tmp_path)
        assert bundled_inventory() is None

    def test_raises_hub_client_error_when_only_cert_exists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        self._setup_env(monkeypatch, tmp_path)
        hub_dir = tmp_path / "hub"
        hub_dir.mkdir(parents=True)
        (hub_dir / "signing-cert.sha1").write_text(VALID_SHA1, encoding="ascii")

        with pytest.raises(HubClientError) as exc_info:
            bundled_inventory()
        assert exc_info.value.retryable is False
        assert "macos_hub_xpc_client.dylib" in str(exc_info.value)

    def test_raises_hub_client_error_when_only_dylib_exists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        self._setup_env(monkeypatch, tmp_path)
        hub_dir = tmp_path / "hub"
        hub_dir.mkdir(parents=True)
        (hub_dir / "macos_hub_xpc_client.dylib").write_bytes(b"\x00" * 100)

        with pytest.raises(HubClientError) as exc_info:
            bundled_inventory()
        assert exc_info.value.retryable is False
        assert "signing-cert.sha1" in str(exc_info.value)

    def test_raises_hub_client_error_when_cert_not_ascii(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        self._setup_env(monkeypatch, tmp_path)
        hub_dir = tmp_path / "hub"
        hub_dir.mkdir(parents=True)
        (hub_dir / "macos_hub_xpc_client.dylib").write_bytes(b"\x00" * 100)
        (hub_dir / "signing-cert.sha1").write_bytes(b"\xff\xfe\x00\x00")

        with pytest.raises(HubClientError) as exc_info:
            bundled_inventory()
        assert exc_info.value.retryable is False

    def test_raises_hub_client_error_when_sha1_invalid(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        self._setup_env(monkeypatch, tmp_path)
        hub_dir = tmp_path / "hub"
        hub_dir.mkdir(parents=True)
        (hub_dir / "macos_hub_xpc_client.dylib").write_bytes(b"\x00" * 100)
        (hub_dir / "signing-cert.sha1").write_text("not_a_valid_40_hex_sha1\n", encoding="ascii")

        with pytest.raises(HubClientError) as exc_info:
            bundled_inventory()
        assert exc_info.value.retryable is False

    def test_raises_hub_client_error_when_asset_is_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        self._setup_env(monkeypatch, tmp_path)
        hub_dir = tmp_path / "hub"
        hub_dir.mkdir(parents=True)
        (hub_dir / "macos_hub_xpc_client.dylib").mkdir()
        (hub_dir / "signing-cert.sha1").write_text(VALID_SHA1, encoding="ascii")

        with pytest.raises(HubClientError) as exc_info:
            bundled_inventory()
        assert exc_info.value.retryable is False

    def test_succeeds_when_both_assets_valid(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        self._setup_env(monkeypatch, tmp_path)
        hub_dir = tmp_path / "hub"
        hub_dir.mkdir(parents=True)
        (hub_dir / "macos_hub_xpc_client.dylib").write_bytes(b"\x00" * 100)
        (hub_dir / "signing-cert.sha1").write_text(f"  {VALID_SHA1}\n", encoding="ascii")

        inv = bundled_inventory()
        assert isinstance(inv, MacOSHubInventory)
        assert inv.canonical_key == CANONICAL_KEY
        assert inv.transport._library_path == (hub_dir / "macos_hub_xpc_client.dylib")
        assert inv.transport._sha1 == VALID_SHA1


# ===========================================================================
# list_devices payload parsing & shape acceptance
# ===========================================================================


class TestListDevicesSuccess:
    def test_accepts_json_list(self) -> None:
        raw = b'[{"id": "device-1", "name": "Pixel 7", "online": true, "battery": 85}]'
        fake = FakeTransport(payload=raw)
        inv = MacOSHubInventory(transport=fake)

        devices = inv.list_devices()
        assert len(devices) == 1
        dev = devices[0]
        assert isinstance(dev, HubDevice)
        assert dev.id == "device-1"
        assert dev.name == "Pixel 7"
        assert dev.online is True
        assert dev.battery == 85

    def test_accepts_json_object_with_devices_list(self) -> None:
        raw = b'{"devices": [{"id": "device-2", "name": "Galaxy S23", "online": false}]}'
        fake = FakeTransport(payload=raw)
        inv = MacOSHubInventory(transport=fake)

        devices = inv.list_devices()
        assert len(devices) == 1
        assert devices[0].id == "device-2"
        assert devices[0].name == "Galaxy S23"
        assert devices[0].online is False

    def test_accepts_empty_list(self) -> None:
        fake = FakeTransport(payload=b"[]")
        inv = MacOSHubInventory(transport=fake)
        assert inv.list_devices() == []

    def test_accepts_object_with_empty_devices(self) -> None:
        fake = FakeTransport(payload=b'{"devices": []}')
        inv = MacOSHubInventory(transport=fake)
        assert inv.list_devices() == []

    def test_reuses_hubdevice_from_dict_and_preserves_extra(self) -> None:
        raw = b'[{"id": "d1", "custom_flag": "custom_val"}]'
        fake = FakeTransport(payload=raw)
        inv = MacOSHubInventory(transport=fake)

        devices = inv.list_devices()
        assert len(devices) == 1
        assert devices[0].id == "d1"
        assert devices[0].extra.get("custom_flag") == "custom_val"


# ===========================================================================
# list_devices shape rejection & payload suppression
# ===========================================================================


class TestListDevicesShapeRejection:
    def test_rejects_non_utf8(self) -> None:
        fake = FakeTransport(payload=b"\xff\xfe\x00\x00SECRET_TOKEN")
        inv = MacOSHubInventory(transport=fake)

        with pytest.raises(HubClientError) as exc_info:
            inv.list_devices()
        assert exc_info.value.retryable is False
        assert "SECRET_TOKEN" not in str(exc_info.value)
        assert "UTF-8" in str(exc_info.value)

    def test_rejects_invalid_json(self) -> None:
        fake = FakeTransport(payload=b"{malformed json SECRET_KEY")
        inv = MacOSHubInventory(transport=fake)

        with pytest.raises(HubClientError) as exc_info:
            inv.list_devices()
        assert exc_info.value.retryable is False
        assert "SECRET_KEY" not in str(exc_info.value)
        assert "JSON" in str(exc_info.value)

    def test_rejects_object_without_devices_key(self) -> None:
        fake = FakeTransport(payload=b'{"error": "none", "secret": "HIDDEN"}')
        inv = MacOSHubInventory(transport=fake)

        with pytest.raises(HubClientError) as exc_info:
            inv.list_devices()
        assert exc_info.value.retryable is False
        assert "HIDDEN" not in str(exc_info.value)

    def test_rejects_object_with_non_list_devices(self) -> None:
        fake = FakeTransport(payload=b'{"devices": "not_a_list"}')
        inv = MacOSHubInventory(transport=fake)

        with pytest.raises(HubClientError) as exc_info:
            inv.list_devices()
        assert exc_info.value.retryable is False

    def test_rejects_primitive_json(self) -> None:
        for val in (b"123", b'"string"', b"true", b"null"):
            inv = MacOSHubInventory(transport=FakeTransport(payload=val))
            with pytest.raises(HubClientError) as exc_info:
                inv.list_devices()
            assert exc_info.value.retryable is False

    def test_rejects_item_not_dict(self) -> None:
        fake = FakeTransport(payload=b'["just_a_string"]')
        inv = MacOSHubInventory(transport=fake)

        with pytest.raises(HubClientError) as exc_info:
            inv.list_devices()
        assert exc_info.value.retryable is False

    def test_rejects_missing_id(self) -> None:
        fake = FakeTransport(payload=b'[{"name": "No ID", "secret": "LEAK"}]')
        inv = MacOSHubInventory(transport=fake)

        with pytest.raises(HubClientError) as exc_info:
            inv.list_devices()
        assert exc_info.value.retryable is False
        assert "LEAK" not in str(exc_info.value)
        assert "id" in str(exc_info.value)

    def test_rejects_empty_id(self) -> None:
        fake = FakeTransport(payload=b'[{"id": ""}]')
        inv = MacOSHubInventory(transport=fake)

        with pytest.raises(HubClientError) as exc_info:
            inv.list_devices()
        assert exc_info.value.retryable is False

    def test_rejects_null_id(self) -> None:
        fake = FakeTransport(payload=b'[{"id": null}]')
        inv = MacOSHubInventory(transport=fake)

        with pytest.raises(HubClientError) as exc_info:
            inv.list_devices()
        assert exc_info.value.retryable is False


# ===========================================================================
# Transport error mapping & retryable flag
# ===========================================================================


class TestTransportErrorMapping:
    def test_maps_unavailable_to_retryable_russian_error(self) -> None:
        exc = MacOSHubTransportError(ErrorKind.unavailable, "Service unavailable")
        inv = MacOSHubInventory(transport=FakeTransport(exc=exc))

        with pytest.raises(HubClientError) as exc_info:
            inv.list_devices()
        err = exc_info.value
        assert err.retryable is True
        assert "недоступна" in str(err).lower()
        assert "Service unavailable" not in str(err)

    def test_maps_temporary_to_retryable_russian_error(self) -> None:
        exc = MacOSHubTransportError(ErrorKind.temporary, "Transient socket fail")
        inv = MacOSHubInventory(transport=FakeTransport(exc=exc))

        with pytest.raises(HubClientError) as exc_info:
            inv.list_devices()
        err = exc_info.value
        assert err.retryable is True
        assert "временная" in str(err).lower()

    def test_maps_authorization_to_non_retryable_russian_error(self) -> None:
        exc = MacOSHubTransportError(ErrorKind.authorization, "Token rejected")
        inv = MacOSHubInventory(transport=FakeTransport(exc=exc))

        with pytest.raises(HubClientError) as exc_info:
            inv.list_devices()
        err = exc_info.value
        assert err.retryable is False
        assert "авторизац" in str(err).lower()

    def test_maps_trust_to_non_retryable_russian_error(self) -> None:
        exc = MacOSHubTransportError(ErrorKind.trust, "Bad cert")
        inv = MacOSHubInventory(transport=FakeTransport(exc=exc))

        with pytest.raises(HubClientError) as exc_info:
            inv.list_devices()
        err = exc_info.value
        assert err.retryable is False
        assert "довери" in str(err).lower() or "сертификат" in str(err).lower()

    def test_maps_invalid_to_non_retryable_russian_error(self) -> None:
        exc = MacOSHubTransportError(ErrorKind.invalid, "Bad request")
        inv = MacOSHubInventory(transport=FakeTransport(exc=exc))

        with pytest.raises(HubClientError) as exc_info:
            inv.list_devices()
        err = exc_info.value
        assert err.retryable is False
        assert "некорректн" in str(err).lower()


# ===========================================================================
# Context manager & cleanup
# ===========================================================================


class TestContextManager:
    def test_context_manager_and_close(self) -> None:
        fake = FakeTransport(payload=b"[]")
        inv = MacOSHubInventory(transport=fake)
        with inv as client:
            assert client is inv
            assert client.list_devices() == []
        inv.close()  # idempotent
