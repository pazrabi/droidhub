"""Текущий авторизованный MCP-клиент (ContextVar на время обработки запроса)."""
from __future__ import annotations

import contextlib
import contextvars

from hub.auth import Client

_current_client: contextvars.ContextVar[Client | None] = contextvars.ContextVar(
    "_current_client", default=None
)


def get_current_client() -> Client:
    """Текущий клиент или дефолтный «mcp»."""
    return _current_client.get() or Client(name="mcp")


def set_current_client(client: Client | None) -> contextvars.Token:
    return _current_client.set(client)


@contextlib.contextmanager
def client_context(client: Client | None):
    """Устанавливает текущего клиента на время блока."""
    token = _current_client.set(client)
    try:
        yield
    finally:
        _current_client.reset(token)
