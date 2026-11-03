"""Лимит действий в минуту на пару (клиент, устройство)."""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict

from hub.auth import Client
from hub.config import get_settings
from hub.mcp_server.context import get_current_client


class ClientDeviceRateLimiter:
    """Скользящее окно в памяти по паре (имя клиента, device_id)."""

    def __init__(self, window_seconds: float = 60.0) -> None:
        self.window_seconds = window_seconds
        self._history: dict[tuple[str, str], list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def check(
        self,
        device_id: str,
        client: Client | str | None = None,
        limit: int | None = None,
    ) -> bool:
        """True, если действие разрешено (и оно засчитано в окне)."""
        if not device_id:
            return True

        if client is None:
            client = get_current_client()
        if isinstance(client, str):
            client_name, client_limit = client, None
        else:
            client_name, client_limit = client.name, client.rate_limit_per_min

        if limit is None:
            limit = client_limit if client_limit is not None else get_settings().rate_limit_per_min
        if limit <= 0:
            return True

        now = time.monotonic()
        key = (client_name, device_id)
        async with self._lock:
            history = [t for t in self._history[key] if t > now - self.window_seconds]
            allowed = len(history) < limit
            if allowed:
                history.append(now)
            self._history[key] = history
            return allowed

    def reset(self, device_id: str | None = None, client: Client | str | None = None) -> None:
        """Сбрасывает историю: всю, по устройству, по клиенту или по паре."""
        client_name = client if isinstance(client, str) or client is None else client.name
        for key in [
            k for k in self._history
            if (device_id is None or k[1] == device_id) and (client_name is None or k[0] == client_name)
        ]:
            del self._history[key]


limiter = ClientDeviceRateLimiter()


async def enforce_rate_limit(device_id: str, client: Client | None = None) -> None:
    """Бросает RuntimeError, если лимит действий на устройство исчерпан."""
    if not device_id:
        return
    client = client or get_current_client()
    if not await limiter.check(device_id, client=client):
        raise RuntimeError(
            f"Превышен лимит действий на устройство '{device_id}' для клиента '{client.name}'"
        )
