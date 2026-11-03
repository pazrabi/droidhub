from __future__ import annotations

import html
import hmac
import logging
import urllib.parse

from fastapi import APIRouter, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from hub.auth import get_client_ip, get_login_limiter, verify_password
from hub.config import Settings, get_settings
from hub.sso import make_cookie, safe_next, verify_cookie

logger = logging.getLogger(__name__)

sso_router = APIRouter(prefix="/sso")


def _get_settings(request: Request) -> Settings:
    """Извлекает настройки из app.state или считывает через get_settings()."""
    if (
        hasattr(request, "app")
        and hasattr(request.app, "state")
        and getattr(request.app.state, "settings", None)
    ):
        return request.app.state.settings
    return get_settings()


def _get_default_target(settings: Settings) -> str:
    """Возвращает безопасный целевой URL по умолчанию."""
    suffix = settings.sso_allowed_suffix.strip().lower().lstrip(".")
    if suffix:
        return f"https://pool.{suffix}/"
    return "https://pool.molokohub.site/"


def _render_login_html(error: str = "", next_url: str = "") -> str:
    """Генерирует HTML страницы входа SSO в стиле темной темы DroidHub без CDN."""
    error_html = (
        f'<div class="alert alert-error" id="login-error-alert" role="alert">'
        f"<span>{html.escape(error)}</span>"
        f"</div>"
        if error
        else ""
    )
    next_input = (
        f'<input type="hidden" name="next" value="{html.escape(next_url, quote=True)}">'
        if next_url
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <meta name="color-scheme" content="dark">
  <title>Единый вход — MolokoHub</title>
  <link rel="stylesheet" href="/static/style.css">
  <style>
    :root {{
      --bg-body: #0d1117;
      --bg-surface: #161b22;
      --border-color: #30363d;
      --text-primary: #f0f6fc;
      --text-secondary: #8b949e;
      --accent-primary: #1f6feb;
      --accent-primary-hover: #388bfd;
      --accent-danger: #da3633;
    }}
    body {{
      margin: 0;
      padding: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background-color: var(--bg-body, #0d1117);
      color: var(--text-primary, #f0f6fc);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }}
    .login-wrapper {{
      display: flex;
      align-items: center;
      justify-content: center;
      width: 100%;
      padding: 1rem;
    }}
    .login-card {{
      width: 100%;
      max-width: 400px;
      background-color: var(--bg-surface, #161b22);
      border: 1px solid var(--border-color, #30363d);
      border-radius: 12px;
      padding: 2rem;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
    }}
    .login-title {{
      font-size: 1.5rem;
      font-weight: 700;
      text-align: center;
      margin-bottom: 0.25rem;
    }}
    .login-subtitle {{
      font-size: 0.85rem;
      color: var(--text-secondary, #8b949e);
      text-align: center;
      margin-bottom: 1.5rem;
    }}
    .alert-error {{
      background-color: rgba(218, 54, 51, 0.15);
      border: 1px solid rgba(248, 81, 73, 0.4);
      color: #f85149;
      padding: 0.75rem 1rem;
      border-radius: 6px;
      margin-bottom: 1rem;
      font-size: 0.9rem;
    }}
    .form-group {{
      margin-bottom: 1rem;
    }}
    .form-group label {{
      display: block;
      font-size: 0.85rem;
      font-weight: 500;
      color: var(--text-secondary, #8b949e);
      margin-bottom: 0.35rem;
    }}
    .form-control {{
      width: 100%;
      box-sizing: border-box;
      padding: 0.6rem 0.75rem;
      background-color: var(--bg-body, #0d1117);
      border: 1px solid var(--border-color, #30363d);
      border-radius: 6px;
      color: var(--text-primary, #f0f6fc);
      font-size: 0.9rem;
    }}
    .form-control:focus {{
      outline: none;
      border-color: var(--accent-primary-hover, #388bfd);
      box-shadow: 0 0 0 3px rgba(31, 111, 235, 0.25);
    }}
    .btn-primary {{
      width: 100%;
      padding: 0.65rem 1rem;
      background-color: var(--accent-primary, #1f6feb);
      color: #ffffff;
      border: none;
      border-radius: 6px;
      font-size: 0.95rem;
      font-weight: 600;
      cursor: pointer;
      margin-top: 0.5rem;
      transition: background-color 0.15s ease;
    }}
    .btn-primary:hover {{
      background-color: var(--accent-primary-hover, #388bfd);
    }}
  </style>
</head>
<body class="dark-theme">
  <div class="login-wrapper">
    <div class="login-card">
      <h1 class="login-title">Вход в MolokoHub</h1>
      <p class="login-subtitle">Единая учетная запись владельца</p>

      {error_html}

      <form action="/sso/login" method="post" class="login-form">
        {next_input}
        <div class="form-group">
          <label for="username">Имя пользователя</label>
          <input
            type="text"
            id="username"
            name="username"
            class="form-control"
            placeholder="Логин"
            required
            autocomplete="username"
            autofocus
          >
        </div>

        <div class="form-group">
          <label for="password">Пароль</label>
          <input
            type="password"
            id="password"
            name="password"
            class="form-control"
            placeholder="Пароль"
            required
            autocomplete="current-password"
          >
        </div>

        <button type="submit" class="btn btn-primary btn-block" id="btn-login-submit">
          Войти
        </button>
      </form>
    </div>
  </div>
</body>
</html>"""


@sso_router.get("/login")
async def sso_login_page(request: Request, next: str = "") -> Response:
    """
    Страница входа: форма логин/пароль.
    Если уже есть валидный SSO-cookie, сразу 303 на safe_next(next) или панель.
    """
    settings = _get_settings(request)
    cookie_name = settings.sso_cookie_name
    cookie_val = request.cookies.get(cookie_name)

    if cookie_val and settings.sso_secret:
        user = verify_cookie(cookie_val, settings.sso_secret)
        if user:
            target = safe_next(next, settings.sso_allowed_suffix) if next else None
            redirect_url = target or _get_default_target(settings)
            return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)

    return HTMLResponse(
        content=_render_login_html(next_url=next),
        status_code=status.HTTP_200_OK,
    )


@sso_router.post("/login")
async def sso_login_submit(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    next: str = Form(default=""),
) -> Response:
    """
    Аутентификация SSO: проверка логина/пароля по admin_user и admin_password_hash,
    ограничение попыток по IP с использованием LoginLimiter.
    Успех -> установка cookie и редирект 303 на safe_next(next) или дефолтный URL.
    Ошибка -> форма входа с русским сообщением об ошибке и статус 401.
    """
    settings = _get_settings(request)
    client_ip = get_client_ip(request)
    limiter = get_login_limiter()

    # Проверка ограничителя попыток входа по IP
    if not limiter.check(client_ip):
        return HTMLResponse(
            content=_render_login_html(
                error="Превышено число попыток входа. Попробуйте позже.",
                next_url=next,
            ),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # Резервный парсинг данных формы (JSON или raw body)
    if not username and not password:
        ct = request.headers.get("content-type", "").lower()
        if "application/json" in ct:
            try:
                data = await request.json()
                if isinstance(data, dict):
                    username = str(data.get("username", ""))
                    password = str(data.get("password", ""))
                    next = str(data.get("next", next or ""))
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
                    if "next" in parsed and parsed["next"]:
                        next = parsed["next"][0]
            except Exception:
                pass

    if not next:
        next = request.query_params.get("next", "")

    expected_user = settings.admin_user.strip() if settings.admin_user else ""
    expected_hash = settings.admin_password_hash.strip() if settings.admin_password_hash else ""
    sso_secret = settings.sso_secret.strip() if settings.sso_secret else ""

    # Constant-time проверка логина и пароля
    user_match = (
        hmac.compare_digest(username.strip(), expected_user)
        if (expected_user and username)
        else False
    )
    # Фиктивный хэш для предотвращения timing-атак при неверном пользователе
    dummy_hash = (
        expected_hash
        if expected_hash
        else "scrypt$n=16384,r=8,p=1$00000000000000000000000000000000$0000000000000000000000000000000000000000000000000000000000000000"
    )
    pw_match = verify_password(password, dummy_hash)

    success = bool(user_match and pw_match and expected_user and expected_hash and sso_secret)

    if success:
        limiter.reset(client_ip)
        ttl_seconds = settings.sso_ttl_hours * 3600
        cookie_val = make_cookie(expected_user, sso_secret, ttl_seconds)

        target = safe_next(next, settings.sso_allowed_suffix) if next else None
        redirect_url = target or _get_default_target(settings)

        response = RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)
        domain_val = settings.sso_cookie_domain.strip() if settings.sso_cookie_domain else None
        response.set_cookie(
            key=settings.sso_cookie_name,
            value=cookie_val,
            max_age=ttl_seconds,
            domain=domain_val,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        if "session" in request.scope:
            request.session["user"] = expected_user
        return response

    limiter.fail(client_ip)
    return HTMLResponse(
        content=_render_login_html(
            error="Неверный логин или пароль, либо превышено число попыток",
            next_url=next,
        ),
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


@sso_router.get("/logout")
@sso_router.post("/logout")
async def sso_logout(request: Request) -> Response:
    """Выход из SSO: удаление cookie и редирект 303 на /sso/login."""
    settings = _get_settings(request)
    domain_val = settings.sso_cookie_domain.strip() if settings.sso_cookie_domain else None

    response = RedirectResponse(url="/sso/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(
        key=settings.sso_cookie_name,
        domain=domain_val,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    if "session" in request.scope:
        request.session.clear()
    return response


@sso_router.get("/verify")
async def sso_verify(request: Request) -> Response:
    """
    Эндпоинт проверки для Caddy forward_auth:
    - При валидном SSO-cookie: 200 OK и заголовок X-Sso-User: <user>.
    - Иначе 302 Location: <sso_login_url>?next=<urlencoded https://{X-Forwarded-Host}{X-Forwarded-Uri}>.
    - Если sso_secret пуст: 503 Service Unavailable.
    """
    settings = _get_settings(request)
    if not settings.sso_secret:
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content="SSO не настроен")

    cookie_name = settings.sso_cookie_name
    cookie_val = request.cookies.get(cookie_name)
    user = verify_cookie(cookie_val, settings.sso_secret) if cookie_val else None

    if user:
        return Response(
            status_code=status.HTTP_200_OK,
            headers={"X-Sso-User": user},
            content="OK",
        )

    # Не авторизован: редирект 302 на страницу входа с параметром next
    login_url = settings.sso_login_url.strip() if settings.sso_login_url else "/sso/login"
    fwd_host = request.headers.get("x-forwarded-host", "").strip()
    fwd_uri = request.headers.get("x-forwarded-uri", "").strip()

    if fwd_host:
        if not fwd_uri:
            fwd_uri = "/"
        elif not fwd_uri.startswith("/"):
            fwd_uri = "/" + fwd_uri
        target = f"https://{fwd_host}{fwd_uri}"
        sep = "&" if "?" in login_url else "?"
        redirect_url = f"{login_url}{sep}next={urllib.parse.quote(target, safe='')}"
    else:
        redirect_url = login_url

    return RedirectResponse(
        url=redirect_url,
        status_code=status.HTTP_302_FOUND,
    )


__all__ = ["sso_router"]
