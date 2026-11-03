"""Сборка ASGI-приложения MCP с авторизацией."""
from __future__ import annotations

from typing import Any, Callable

from mcp.server.transport_security import TransportSecuritySettings

from hub.config import Settings, get_settings
from hub.mcp_server.auth import MCPAuthMiddleware
from hub.mcp_server.tools import mcp_server


def create_mcp_app(settings: Settings | None = None) -> tuple[Any, Callable[[], Any]]:
    """ASGI-приложение MCP с авторизацией и функция для lifespan основного приложения."""
    starlette_app = mcp_server.streamable_http_app(
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    def get_token() -> str:
        return (settings or get_settings()).mcp_token

    auth_app = MCPAuthMiddleware(starlette_app, get_token=get_token, settings=settings)
    return auth_app, lambda: starlette_app.router.lifespan_context(starlette_app)
