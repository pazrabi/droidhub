from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Generator


class Audit:
    """Журнал аудита действий хаба на базе SQLite."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._lock = asyncio.Lock()
        if self.db_path == ":memory:":
            import uuid

            mem_name = f"mem_{uuid.uuid4().hex}"
            self._resolved_path = f"file:{mem_name}?mode=memory&cache=shared"
            self._is_uri = True
            self._keep_alive: sqlite3.Connection | None = sqlite3.connect(
                self._resolved_path, timeout=5.0, uri=True
            )
            self._keep_alive.execute("PRAGMA busy_timeout=5000;")
        else:
            self._resolved_path = self.db_path
            self._is_uri = False
            self._keep_alive = None
            parent = os.path.dirname(os.path.abspath(self.db_path))
            if parent:
                os.makedirs(parent, exist_ok=True)

        self._init_db_sync()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._resolved_path, timeout=5.0, uri=self._is_uri)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    @contextlib.contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _init_db_sync(self) -> None:
        with self._connection() as conn:
            with conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA busy_timeout=5000;")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS audit_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        actor TEXT NOT NULL,
                        device_id TEXT,
                        action TEXT NOT NULL,
                        params TEXT NOT NULL,
                        ok INTEGER NOT NULL,
                        error TEXT
                    )
                    """
                )

    async def log(
        self,
        actor: str,
        device_id: str | None,
        action: str,
        params: dict,
        ok: bool,
        error: str | None = None,
    ) -> None:
        """Записывает событие в журнал аудита."""
        def _do_log() -> None:
            safe_params = params if params is not None else {}
            params_json = json.dumps(safe_params, ensure_ascii=False, default=str)
            ts = time.time()
            with self._connection() as conn:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO audit_log (timestamp, actor, device_id, action, params, ok, error)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (ts, actor, device_id, action, params_json, 1 if ok else 0, error),
                    )

        async with self._lock:
            await asyncio.to_thread(_do_log)

    async def recent(self, limit: int = 100) -> list[dict]:
        """Возвращает последние записи журнала аудита (новые сначала)."""
        def _do_recent() -> list[dict]:
            if limit <= 0:
                return []
            with self._connection() as conn:
                cursor = conn.execute(
                    """
                    SELECT id, timestamp, actor, device_id, action, params, ok, error
                    FROM audit_log
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
                rows = cursor.fetchall()
                results: list[dict] = []
                for row in rows:
                    params_raw = row["params"]
                    try:
                        p = json.loads(params_raw) if params_raw else {}
                    except Exception:
                        p = {}
                    results.append(
                        {
                            "id": row["id"],
                            "timestamp": float(row["timestamp"]),
                            "actor": str(row["actor"]),
                            "device_id": row["device_id"],
                            "action": str(row["action"]),
                            "params": p,
                            "ok": bool(row["ok"]),
                            "error": row["error"],
                        }
                    )
                return results

        return await asyncio.to_thread(_do_recent)

    def close(self) -> None:
        """Закрывает вспомогательное соединение (если открыто)."""
        if self._keep_alive is not None:
            self._keep_alive.close()
            self._keep_alive = None


_audit: Audit | None = None


def init_audit(db_path: str | Path) -> Audit:
    """Инициализирует глобальный экземпляр журнала аудита."""
    global _audit
    _audit = Audit(db_path)
    return _audit


def get_audit() -> Audit:
    """Возвращает глобальный экземпляр журнала аудита."""
    global _audit
    if _audit is None:
        raise RuntimeError("Журнал аудита не инициализирован. Сначала вызовите init_audit().")
    return _audit


__all__ = ["Audit", "init_audit", "get_audit"]
