"""ASGI-авторизация MCP: заголовок Authorization: Bearer <token> или секретный путь /mcp/<token>."""
from __future__ import annotations

import hmac
from typing import Any, Callable

from fastapi import status
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from hub.auth import Client, resolve_token
from hub.config import Settings, get_settings
from hub.mcp_server.context import _current_client


class MCPAuthMiddleware:
    def __init__(
        self,
        app: Any,
        get_token: Callable[[], str] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.app = app
        self.get_token = get_token
        self.settings = settings

    def _settings(self) -> Settings:
        return self.settings if self.settings is not None else get_settings()

    def _is_server_enabled(self) -> bool:
        if self.get_token is not None and (self.get_token() or "").strip():
            return True
        st = self._settings()
        return bool(st.mcp_token.strip() or st.client_tokens.strip())

    def _resolve_client(self, token: str) -> Client | None:
        if not token:
            return None
        client = resolve_token(token, settings=self._settings())
        if client is not None:
            return client
        if self.get_token is not None:
            expected = (self.get_token() or "").strip()
            if expected and hmac.compare_digest(token, expected):
                return Client(name="mcp")
        return None

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, response: JSONResponse) -> None:
        if scope["type"] == "http":
            await response(scope, receive, send)
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return

        if not self._is_server_enabled():
            await self._reject(scope, receive, send, JSONResponse(
                {"detail": "MCP сервер отключен (mcp_token не настроен)"},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            ))
            return

        path = scope.get("path", "")
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("latin1")

        client: Client | None = None
        new_path = path

        # 1. Заголовок Authorization: Bearer <token>
        if auth_header.lower().startswith("bearer "):
            client = self._resolve_client(auth_header[7:].strip())

        # 2. Секретный путь: /mcp/<token> или /<token>
        if client is None:
            candidate_token, candidate_path = self._extract_path_token(path)
            candidate_client = self._resolve_client(candidate_token)
            if candidate_client is not None:
                client, new_path = candidate_client, candidate_path

        if client is None:
            await self._reject(scope, receive, send, JSONResponse(
                {"detail": "Не авторизован"},
                status_code=status.HTTP_401_UNAUTHORIZED,
                headers={"WWW-Authenticate": "Bearer"},
            ))
            return

        # Прямое обращение к приложению FastMCP по /mcp или /mcp/ (например, с Bearer)
        if new_path == "/mcp":
            new_path = "/"
        elif new_path.startswith("/mcp/"):
            new_path = new_path[4:] or "/"

        scope = dict(scope)
        scope["auth_client"] = client
        if new_path != path:
            scope["path"] = new_path
            scope["raw_path"] = new_path.encode("latin1")

        token = _current_client.set(client)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_client.reset(token)

    @staticmethod
    def _extract_path_token(path: str) -> tuple[str, str]:
        """Первый сегмент пути (после необязательного /mcp) как токен и остаток пути."""
        subpath = path[4:] if path == "/mcp" or path.startswith("/mcp/") else path
        parts = [p.strip() for p in subpath.split("/") if p.strip()]
        if not parts:
            return "", path
        candidate = parts[0]
        after = subpath[subpath.find(candidate) + len(candidate):]
        if not after or after == "/":
            return candidate, "/"
        return candidate, after if after.startswith("/") else "/" + after
