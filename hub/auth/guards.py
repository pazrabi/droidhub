"""Зависимости FastAPI: пользователь (сессия/SSO) и доступ к API."""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status

from hub import sso
from hub.config import Settings, get_settings
from hub.auth.clients import Client, resolve_token

logger = logging.getLogger(__name__)


def get_sso_user(request: Request, settings: Settings | None = None) -> str | None:
    """
    Проверяет SSO-cookie в запросе через hub.sso.verify_cookie.
    Возвращает логин пользователя или None.
    Если sso_secret не настроен или пуст, возвращает None (SSO отключен).
    """
    if settings is None:
        scope = getattr(request, "scope", None)
        app = scope.get("app") if isinstance(scope, dict) else None
        if app is not None:
            settings = getattr(getattr(app, "state", None), "settings", None)
        if settings is None:
            settings = get_settings()

    sso_secret = (getattr(settings, "sso_secret", "") or "").strip()
    if not sso_secret:
        return None

    cookie_name = getattr(settings, "sso_cookie_name", "molokohub_sso") or "molokohub_sso"
    cookies = getattr(request, "cookies", {}) or {}
    cookie_val = cookies.get(cookie_name)
    if not cookie_val:
        return None

    try:
        user = sso.verify_cookie(cookie_val, sso_secret)
    except Exception:
        return None

    if user and isinstance(user, str) and user.strip():
        return user.strip()
    return None


def require_user(request: Request) -> str:
    """
    FastAPI dependency: требует авторизованного пользователя в сессии или валидный SSO-cookie.
    Возвращает логин пользователя или вызывает HTTPException(401).
    Сохраняет Client в request.state.client (Client(name=логин)).
    """
    user = None
    if "session" in request.scope:
        user = request.session.get("user")

    if user and isinstance(user, str) and user.strip():
        u = user.strip()
        request.state.client = Client(name=u)
        return u

    sso_user = get_sso_user(request)
    if sso_user:
        request.state.client = Client(name=sso_user)
        return sso_user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Требуется авторизация",
    )

def require_api(request: Request) -> str:
    """
    FastAPI dependency: сессия панели ИЛИ заголовок Authorization: Bearer <token> ИЛИ валидный SSO-cookie.
    Проверяет Bearer через resolve_token.
    Сохраняет Client в request.state.client (для сессии панели и SSO: Client(name=логин)).
    Возвращает client.name (логин или имя клиента токена, например 'api', 'mcp', 'pool').
    """
    # 1. Сессия панели
    if "session" in request.scope:
        user = request.session.get("user")
        if user and isinstance(user, str) and user.strip():
            client = Client(name=user.strip())
            request.state.client = client
            return client.name

    # 2. Bearer токен авторизации
    auth_header = request.headers.get("Authorization")
    if auth_header:
        scheme, _, raw_token = auth_header.partition(" ")
        token = raw_token.strip()
        if scheme.lower() == "bearer" and token:
            client = resolve_token(token)
            if client is not None:
                request.state.client = client
                return client.name

    # 3. Валидный SSO-cookie
    sso_user = get_sso_user(request)
    if sso_user:
        client = Client(name=sso_user)
        request.state.client = client
        return client.name

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Не авторизован",
        headers={"WWW-Authenticate": "Bearer"},
    )
