"""
Tests for desktop.device_aliases – DeviceAliasStore.

Covers:
- persist / reopen
- clear / missing serial
- invalid / corrupt file
- concurrent writes preserving all changes
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

# Import directly from the module file (no desktop package __init__)
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "device_aliases",
    Path(__file__).parent.parent / "desktop" / "device_aliases.py",
)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

DeviceAliasStore = _mod.DeviceAliasStore
DeviceAliasError = _mod.DeviceAliasError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_store(tmp_path: Path, filename: str = "aliases.json") -> DeviceAliasStore:
    return DeviceAliasStore(tmp_path / filename)


# ---------------------------------------------------------------------------
# Persist / reopen
# ---------------------------------------------------------------------------

class TestPersistReopen:
    def test_set_and_list(self, tmp_path):
        store = make_store(tmp_path)
        store.set_alias("ABC123", "My Phone")
        assert store.list_aliases() == {"ABC123": "My Phone"}

    def test_persists_across_instances(self, tmp_path):
        store1 = make_store(tmp_path)
        store1.set_alias("SN001", "Tablet A")
        store1.set_alias("SN002", "Phone B")

        store2 = make_store(tmp_path)
        assert store2.list_aliases() == {"SN001": "Tablet A", "SN002": "Phone B"}

    def test_overwrite_alias(self, tmp_path):
        store = make_store(tmp_path)
        store.set_alias("SN001", "Old Name")
        store.set_alias("SN001", "New Name")
        assert store.list_aliases()["SN001"] == "New Name"

    def test_multiple_serials_independent(self, tmp_path):
        store = make_store(tmp_path)
        store.set_alias("AAAA", "Alpha")
        store.set_alias("BBBB", "Beta")
        store.set_alias("CCCC", "Gamma")
        result = store.list_aliases()
        assert result["AAAA"] == "Alpha"
        assert result["BBBB"] == "Beta"
        assert result["CCCC"] == "Gamma"

    def test_duplicate_display_alias_allowed(self, tmp_path):
        """Two different serials may share the same display alias."""
        store = make_store(tmp_path)
        store.set_alias("SERIAL1", "My Device")
        store.set_alias("SERIAL2", "My Device")
        result = store.list_aliases()
        assert result["SERIAL1"] == "My Device"
        assert result["SERIAL2"] == "My Device"

    def test_no_file_written_until_set(self, tmp_path):
        store_path = tmp_path / "aliases.json"
        store = DeviceAliasStore(store_path)
        store.list_aliases()  # should not create file
        assert not store_path.exists()

    def test_alias_is_trimmed(self, tmp_path):
        store = make_store(tmp_path)
        store.set_alias("SN001", "  Hello World  ")
        assert store.list_aliases()["SN001"] == "Hello World"


# ---------------------------------------------------------------------------
# Clear / missing
# ---------------------------------------------------------------------------

class TestClearMissing:
    def test_missing_file_returns_empty(self, tmp_path):
        store = make_store(tmp_path)
        assert store.list_aliases() == {}

    def test_clear_existing_alias(self, tmp_path):
        store = make_store(tmp_path)
        store.set_alias("SN001", "Laptop")
        store.set_alias("SN001", None)
        assert "SN001" not in store.list_aliases()

    def test_clear_missing_serial_no_error(self, tmp_path):
        store = make_store(tmp_path)
        store.set_alias("NONEXISTENT", None)  # must not raise
        assert store.list_aliases() == {}

    def test_clear_one_leaves_others(self, tmp_path):
        store = make_store(tmp_path)
        store.set_alias("SN001", "Alpha")
        store.set_alias("SN002", "Beta")
        store.set_alias("SN001", None)
        result = store.list_aliases()
        assert "SN001" not in result
        assert result.get("SN002") == "Beta"


# ---------------------------------------------------------------------------
# Invalid inputs
# ---------------------------------------------------------------------------

class TestInvalidInputs:
    # --- serial validation ---

    def test_blank_serial_rejected(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(ValueError):
            store.set_alias("", "Name")

    def test_whitespace_serial_rejected(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(ValueError):
            store.set_alias("   ", "Name")

    def test_serial_with_newline_rejected(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(ValueError):
            store.set_alias("SN\n001", "Name")

    def test_serial_host_port_rejected(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(ValueError):
            store.set_alias("192.168.1.5:5555", "Name")

    def test_serial_emulator_rejected(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(ValueError):
            store.set_alias("emulator-5554", "Name")

    # --- alias validation ---

    def test_blank_alias_rejected(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(ValueError):
            store.set_alias("SN001", "")

    def test_whitespace_only_alias_rejected(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(ValueError):
            store.set_alias("SN001", "   ")

    def test_alias_too_long_rejected(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(ValueError):
            store.set_alias("SN001", "A" * 61)

    def test_alias_exactly_60_chars_accepted(self, tmp_path):
        store = make_store(tmp_path)
        store.set_alias("SN001", "A" * 60)
        assert store.list_aliases()["SN001"] == "A" * 60

    def test_alias_control_char_rejected(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(ValueError):
            store.set_alias("SN001", "Bad\x01Alias")

    def test_alias_tab_rejected(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(ValueError):
            store.set_alias("SN001", "Bad\tAlias")

    def test_alias_newline_rejected(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(ValueError):
            store.set_alias("SN001", "Bad\nAlias")


# ---------------------------------------------------------------------------
# Corrupt / damaged / unsupported file
# ---------------------------------------------------------------------------

class TestCorruptFile:
    def test_corrupt_json_raises(self, tmp_path):
        store_path = tmp_path / "aliases.json"
        store_path.write_text("not json at all!!!", encoding="utf-8")
        store = DeviceAliasStore(store_path)
        with pytest.raises(DeviceAliasError):
            store.list_aliases()

    def test_wrong_schema_version_raises(self, tmp_path):
        store_path = tmp_path / "aliases.json"
        store_path.write_text(
            json.dumps({"version": 99, "aliases": {}}), encoding="utf-8"
        )
        store = DeviceAliasStore(store_path)
        with pytest.raises(DeviceAliasError):
            store.list_aliases()

    def test_missing_version_raises(self, tmp_path):
        store_path = tmp_path / "aliases.json"
        store_path.write_text(json.dumps({"aliases": {}}), encoding="utf-8")
        store = DeviceAliasStore(store_path)
        with pytest.raises(DeviceAliasError):
            store.list_aliases()

    def test_top_level_list_raises(self, tmp_path):
        store_path = tmp_path / "aliases.json"
        store_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        store = DeviceAliasStore(store_path)
        with pytest.raises(DeviceAliasError):
            store.list_aliases()

    def test_aliases_not_dict_raises(self, tmp_path):
        store_path = tmp_path / "aliases.json"
        store_path.write_text(
            json.dumps({"version": 1, "aliases": ["a", "b"]}), encoding="utf-8"
        )
        store = DeviceAliasStore(store_path)
        with pytest.raises(DeviceAliasError):
            store.list_aliases()

    def test_corrupt_file_not_overwritten_silently(self, tmp_path):
        """set_alias on a corrupt store must raise, not silently overwrite."""
        store_path = tmp_path / "aliases.json"
        corrupt_content = "this is not json"
        store_path.write_text(corrupt_content, encoding="utf-8")
        store = DeviceAliasStore(store_path)
        with pytest.raises(DeviceAliasError):
            store.set_alias("SN001", "Name")
        # File must still contain the original corrupt content
        assert store_path.read_text(encoding="utf-8") == corrupt_content

    def test_nonstring_values_raise(self, tmp_path):
        store_path = tmp_path / "aliases.json"
        store_path.write_text(
            json.dumps({"version": 1, "aliases": {"SN001": 42}}), encoding="utf-8"
        )
        store = DeviceAliasStore(store_path)
        with pytest.raises(DeviceAliasError):
            store.list_aliases()


# ---------------------------------------------------------------------------
# Concurrent writes
# ---------------------------------------------------------------------------

class TestConcurrentWrites:
    def test_concurrent_threads_preserve_all_changes(self, tmp_path):
        """
        Many threads each writing a unique serial; all entries must survive.
        """
        n = 30
        errors: list[Exception] = []

        def writer(idx: int) -> None:
            try:
                store = DeviceAliasStore(tmp_path / "aliases.json")
                store.set_alias(f"SERIAL{idx:04d}", f"Device {idx}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors during concurrent writes: {errors}"

        final = DeviceAliasStore(tmp_path / "aliases.json").list_aliases()
        for i in range(n):
            key = f"SERIAL{i:04d}"
            assert key in final, f"Missing {key} in final aliases"
            assert final[key] == f"Device {i}"

    def test_concurrent_set_and_clear(self, tmp_path):
        """
        Half threads set, half threads clear a different set of serials.
        Neither group should corrupt the other's data.
        """
        n = 20
        errors: list[Exception] = []
        store_path = tmp_path / "aliases.json"

        # Pre-populate serials that will be cleared
        setup = DeviceAliasStore(store_path)
        for i in range(n):
            setup.set_alias(f"CLEAR{i:04d}", f"To Clear {i}")

        def setter(idx: int) -> None:
            try:
                DeviceAliasStore(store_path).set_alias(f"SET{idx:04d}", f"Set {idx}")
            except Exception as exc:
                errors.append(exc)

        def clearer(idx: int) -> None:
            try:
                DeviceAliasStore(store_path).set_alias(f"CLEAR{idx:04d}", None)
            except Exception as exc:
                errors.append(exc)

        threads = (
            [threading.Thread(target=setter, args=(i,)) for i in range(n)]
            + [threading.Thread(target=clearer, args=(i,)) for i in range(n)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors during concurrent set/clear: {errors}"

        final = DeviceAliasStore(store_path).list_aliases()
        # All SET serials must be present
        for i in range(n):
            assert f"SET{i:04d}" in final, f"Missing SET{i:04d}"
        # All CLEAR serials must be absent
        for i in range(n):
            assert f"CLEAR{i:04d}" not in final, f"Stale CLEAR{i:04d}"

    @pytest.mark.skipif(sys.platform == "win32", reason="fork not available on Windows")
    def test_concurrent_processes_preserve_all_changes(self, tmp_path):
        """
        Multiple OS processes each writing a unique serial; all must survive.
        Uses multiprocessing (fork-based on POSIX).
        """
        import multiprocessing

        store_path = tmp_path / "aliases.json"
        n = 10

        def _worker(path: str, idx: int) -> None:
            # Re-import inside the forked process
            import importlib.util as _ilu
            from pathlib import Path as _P

            _s = _ilu.spec_from_file_location(
                "device_aliases",
                _P(__file__).parent.parent / "desktop" / "device_aliases.py",
            )
            _m = _ilu.module_from_spec(_s)  # type: ignore[arg-type]
            _s.loader.exec_module(_m)  # type: ignore[union-attr]
            _m.DeviceAliasStore(path).set_alias(f"PROC{idx:04d}", f"Process {idx}")

        ctx = multiprocessing.get_context("fork")
        procs = [ctx.Process(target=_worker, args=(str(store_path), i)) for i in range(n)]
        for p in procs:
            p.start()
        for p in procs:
            p.join()

        final = DeviceAliasStore(store_path).list_aliases()
        for i in range(n):
            key = f"PROC{i:04d}"
            assert key in final, f"Missing {key} in final aliases after multi-process writes"
