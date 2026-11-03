"""Роуты входа и выхода по паролю."""

from __future__ import annotations

import hmac
import logging
import urllib.parse

from fastapi import APIRouter, Form, Request, Response, status
from fastapi.responses import RedirectResponse

from hub.config import get_settings
from hub.auth.limiter import get_client_ip, get_login_limiter
from hub.auth.passwords import verify_password

logger = logging.getLogger(__name__)


auth_router = APIRouter()


@auth_router.post("/login")
async def login(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
) -> Response:
    """
    Вход в панель: проверка логина/пароля, ограничение попыток по IP.
    Успех -> сессия request.session["user"], редирект 303 на /
    Ошибка -> редирект 303 на /login?error=1
    """
    client_ip = get_client_ip(request)
    limiter = get_login_limiter()

    # Ограничение попыток по IP
    if not limiter.check(client_ip):
        return RedirectResponse(
            url="/login?error=1",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # Запасной вариант парсинга (если форма пришла как JSON или urlencoded body)
    if not username and not password:
        ct = request.headers.get("content-type", "").lower()
        if "application/json" in ct:
            try:
                data = await request.json()
                if isinstance(data, dict):
                    username = str(data.get("username", ""))
                    password = str(data.get("password", ""))
            except Exception:
                pass
        else:
            try:
                body = await request.body()
                if body:
                    parsed = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
                    if "username" in parsed and parsed["username"]:
                        username = parsed["username"][0]
                    if "password" in parsed and parsed["password"]:
                        password = parsed["password"][0]
            except Exception:
                pass

    settings = get_settings()
    expected_user = settings.admin_user.strip() if settings.admin_user else ""
    expected_hash = settings.admin_password_hash.strip() if settings.admin_password_hash else ""

    success = False
    if expected_user and expected_hash:
        if hmac.compare_digest(username.strip(), expected_user):
            if verify_password(password, expected_hash):
                success = True

    if success:
        limiter.reset(client_ip)
        if "session" in request.scope:
            request.session["user"] = expected_user
        return RedirectResponse(
            url="/",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    limiter.fail(client_ip)
    return RedirectResponse(
        url="/login?error=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@auth_router.post("/logout")
@auth_router.get("/logout")
async def logout(request: Request) -> Response:
    """Выход из панели: очистка сессии, удаление SSO-cookie (если SSO включен) и редирект 303 на /login."""
    if "session" in request.scope:
        request.session.clear()

    response = RedirectResponse(
        url="/login",
        status_code=status.HTTP_303_SEE_OTHER,
    )

    scope = getattr(request, "scope", None)
    app = scope.get("app") if isinstance(scope, dict) else None
    settings = getattr(getattr(app, "state", None), "settings", None) if app is not None else None
    if settings is None:
        settings = get_settings()

    sso_secret = (getattr(settings, "sso_secret", "") or "").strip()
    if sso_secret:
        cookie_name = getattr(settings, "sso_cookie_name", "molokohub_sso") or "molokohub_sso"
        domain = getattr(settings, "sso_cookie_domain", "") or None
        response.delete_cookie(cookie_name, domain=domain)

    return response
