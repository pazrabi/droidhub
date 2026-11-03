"""MCP-сервер DroidHub. Публичный интерфейс пакета."""
from hub.auth import Client, resolve_token
from hub.mcp_server.app import create_mcp_app
from hub.mcp_server.auth import MCPAuthMiddleware
from hub.mcp_server.context import client_context, get_current_client, set_current_client
from hub.mcp_server.look import look_cache as _look_cache
from hub.mcp_server.ratelimit import ClientDeviceRateLimiter, limiter
from hub.mcp_server.tools import (
    device_status, list_apps, list_devices, look, mcp_server, open_app, open_url,
    press_key, screenshot, swipe, tap, tap_element, tap_index, type_text, ui_dump, wait,
)

__all__ = [
    "Client", "ClientDeviceRateLimiter", "MCPAuthMiddleware", "client_context", "create_mcp_app",
    "get_current_client", "limiter", "mcp_server", "resolve_token", "set_current_client",
    "device_status", "list_apps", "list_devices", "look", "open_app", "open_url", "press_key",
    "screenshot", "swipe", "tap", "tap_element", "tap_index", "type_text", "ui_dump", "wait",
]
