from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

import hub.audit as audit_module
from hub.audit import Audit, get_audit, init_audit


@pytest.mark.asyncio
async def test_audit_log_and_recent(tmp_path: Path) -> None:
    db_file = tmp_path / "test_hub.db"
    audit = Audit(db_file)

    before_ts = time.time()
    await audit.log(
        actor="admin",
        device_id="realme",
        action="tap",
        params={"x": 100, "y": 200, "comment": "тестовый тап"},
        ok=True,
    )
    after_ts = time.time()

    entries = await audit.recent(limit=10)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["id"] == 1
    assert before_ts <= entry["timestamp"] <= after_ts + 0.1
    assert entry["actor"] == "admin"
    assert entry["device_id"] == "realme"
    assert entry["action"] == "tap"
    assert entry["params"] == {"x": 100, "y": 200, "comment": "тестовый тап"}
    assert entry["ok"] is True
    assert entry["error"] is None

    # Добавляем вторую запись с ошибкой и без device_id
    await audit.log(
        actor="mcp",
        device_id=None,
        action="connect",
        params={"host": "100.64.0.1", "port": 5555},
        ok=False,
        error="Устройство не в сети",
    )

    all_entries = await audit.recent(limit=10)
    assert len(all_entries) == 2
    # recent возвращает последние записи (новые сначала)
    assert all_entries[0]["id"] == 2
    assert all_entries[0]["actor"] == "mcp"
    assert all_entries[0]["device_id"] is None
    assert all_entries[0]["ok"] is False
    assert all_entries[0]["error"] == "Устройство не в сети"

    assert all_entries[1]["id"] == 1

    # Проверка лимита
    only_latest = await audit.recent(limit=1)
    assert len(only_latest) == 1
    assert only_latest[0]["id"] == 2

    # Лимит <= 0
    empty = await audit.recent(limit=0)
    assert empty == []

    audit.close()


@pytest.mark.asyncio
async def test_audit_module_init_and_get(tmp_path: Path) -> None:
    # Сбрасываем глобальный _audit
    audit_module._audit = None

    with pytest.raises(RuntimeError) as exc_info:
        get_audit()
    assert "не инициализирован" in str(exc_info.value)

    db_file = tmp_path / "global_audit.db"
    instance = init_audit(db_file)
    assert get_audit() is instance

    await get_audit().log(
        actor="system",
        device_id="redmi",
        action="screenshot",
        params={"max_width": 720},
        ok=True,
    )

    records = await get_audit().recent(limit=5)
    assert len(records) == 1
    assert records[0]["actor"] == "system"
    assert records[0]["device_id"] == "redmi"
    assert records[0]["action"] == "screenshot"
    assert records[0]["params"] == {"max_width": 720}

    instance.close()


@pytest.mark.asyncio
async def test_audit_memory_mode() -> None:
    audit = Audit(":memory:")

    # Проверяем запись обычных параметров и несериализуемых напрямую в JSON объектов
    await audit.log(
        actor="mem_user",
        device_id="mem_dev",
        action="action_mem",
        params={
            "path": Path("/var/log/test"),
            "error_obj": ValueError("ошибка валидации"),
            "bytes_val": b"binary",
            "number": 42,
        },
        ok=True,
    )

    records = await audit.recent(limit=10)
    assert len(records) == 1
    assert records[0]["actor"] == "mem_user"
    assert records[0]["device_id"] == "mem_dev"
    assert records[0]["action"] == "action_mem"
    assert records[0]["ok"] is True
    assert records[0]["params"]["path"] == "/var/log/test"
    assert "ошибка валидации" in records[0]["params"]["error_obj"]
    assert "binary" in records[0]["params"]["bytes_val"]
    assert records[0]["params"]["number"] == 42

    audit.close()


@pytest.mark.asyncio
async def test_audit_concurrent_writes_memory() -> None:
    audit = Audit(":memory:")
    total = 30

    async def log_entry(i: int) -> None:
        await audit.log(
            actor=f"concurrent_actor_{i}",
            device_id=f"device_{i}",
            action="tap",
            params={"index": i},
            ok=(i % 2 == 0),
        )

    await asyncio.gather(*[log_entry(i) for i in range(total)])

    records = await audit.recent(limit=total + 10)
    assert len(records) == total
    actors = {r["actor"] for r in records}
    assert actors == {f"concurrent_actor_{i}" for i in range(total)}

    audit.close()


@pytest.mark.asyncio
async def test_audit_concurrent_writes_file(tmp_path: Path) -> None:
    db_file = tmp_path / "concurrent_file.db"
    audit = Audit(db_file)
    total = 30

    async def log_entry(i: int) -> None:
        await audit.log(
            actor=f"file_actor_{i}",
            device_id="dev1",
            action="swipe",
            params={"idx": i, "path": Path(f"/data/{i}")},
            ok=True,
        )

    await asyncio.gather(*[log_entry(i) for i in range(total)])

    records = await audit.recent(limit=total + 10)
    assert len(records) == total
    actors = {r["actor"] for r in records}
    assert actors == {f"file_actor_{i}" for i in range(total)}

    audit.close()
