from __future__ import annotations

import argparse
from dataclasses import dataclass
import getpass
import hashlib
import hmac
import io
import logging
import re
import secrets
import sys
import time
import urllib.parse

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from hub.config import Settings, get_settings

# Контракт этапа 5: импорт модуля единого входа (SSO)
try:
    from hub import sso
except (ImportError, ModuleNotFoundError):
    import types
    sso = types.ModuleType("hub.sso")
    def _verify_cookie_stub(value: str, secret: str, now: float | None = None) -> str | None:
        return None
    sso.verify_cookie = _verify_cookie_stub  # type: ignore[attr-defined]
    sys.modules["hub.sso"] = sso
    try:
        import hub
        setattr(hub, "sso", sso)
    except Exception:
        pass

logger = logging.getLogger(__name__)


def hash_password(pw: str, algo: str = "scrypt") -> str:
    """
    Хэширует пароль с солью с использованием stdlib (scrypt или pbkdf2).
    Формат результата: algo$params$salt$hash.
    """
    salt = secrets.token_hex(16)
    if algo == "scrypt" and hasattr(hashlib, "scrypt"):
        n, r, p = 16384, 8, 1
        dk = hashlib.scrypt(
            pw.encode("utf-8"),
            salt=bytes.fromhex(salt),
            n=n,
            r=r,
            p=p,
            maxmem=0,
            dklen=32,
        )
        return f"scrypt$n={n},r={r},p={p}${salt}${dk.hex()}"
    else:
        iterations = 100_000
        dk = hashlib.pbkdf2_hmac(
            "sha256",
            pw.encode("utf-8"),
            bytes.fromhex(salt),
            iterations,
            dklen=32,
        )
        return f"pbkdf2_sha256$iterations={iterations}${salt}${dk.hex()}"


def verify_password(pw: str, hashed: str) -> bool:
    """
    Проверяет пароль против хэша в формате algo$params$salt$hash за постоянное время.
    """
    if not isinstance(pw, str) or not isinstance(hashed, str):
        return False
    if not pw or not hashed:
        return False

    try:
        parts = hashed.split("$")
        if len(parts) != 4:
            return False
        algo, params_str, salt_str, hash_hex = parts
        salt = bytes.fromhex(salt_str)
        target = bytes.fromhex(hash_hex)
        dklen = len(target)
        if dklen == 0:
            return False

        if algo == "scrypt":
            params: dict[str, int] = {}
            if "=" in params_str:
                for item in params_str.split(","):
                    if "=" in item:
                        k, v = item.split("=", 1)
                        params[k.strip()] = int(v.strip())
            else:
                nums = [int(x.strip()) for x in params_str.split(",") if x.strip()]
                if len(nums) >= 3:
                    params["n"], params["r"], params["p"] = nums[0], nums[1], nums[2]

            n = params.get("n", 16384)
            r = params.get("r", 8)
            p = params.get("p", 1)
            candidate = hashlib.scrypt(
                pw.encode("utf-8"),
                salt=salt,
                n=n,
                r=r,
                p=p,
                maxmem=0,
                dklen=dklen,
            )
            return hmac.compare_digest(candidate, target)

        elif algo in ("pbkdf2", "pbkdf2_sha256"):
            params = {}
            if "=" in params_str:
                for item in params_str.split(","):
                    if "=" in item:
                        k, v = item.split("=", 1)
                        params[k.strip()] = int(v.strip())
            else:
                nums = [int(x.strip()) for x in params_str.split(",") if x.strip()]
                if nums:
                    params["iterations"] = nums[0]

            iterations = params.get("iterations", 100_000)
            candidate = hashlib.pbkdf2_hmac(
                "sha256",
                pw.encode("utf-8"),
                salt=salt,
                iterations=iterations,
                dklen=dklen,
            )
            return hmac.compare_digest(candidate, target)

        return False
    except Exception:
        return False


class LoginLimiter:
    """Ограничитель попыток входа с плавающим временным окном (rate limiter)."""

    def __init__(self, max_attempts: int = 5, window_s: float = 300.0) -> None:
        self.max_attempts = int(max_attempts)
        self.window_s = float(window_s)
        self._attempts: dict[str, list[float]] = {}

    def check(self, key: str) -> bool:
        """Проверяет, разрешена ли попытка входа для данного ключа."""
        now = time.monotonic()
        cutoff = now - self.window_s
        attempts = [t for t in self._attempts.get(key, []) if t > cutoff]
        if attempts:
            self._attempts[key] = attempts
        else:
            self._attempts.pop(key, None)
        return len(attempts) < self.max_attempts

    def fail(self, key: str) -> None:
        """Регистрирует неудачную попытку входа."""
        now = time.monotonic()
        cutoff = now - self.window_s
        attempts = [t for t in self._attempts.get(key, []) if t > cutoff]
        attempts.append(now)
        self._attempts[key] = attempts

    def reset(self, key: str) -> None:
        """Сбрасывает историю попыток для ключа."""
        self._attempts.pop(key, None)


login_limiter = LoginLimiter(max_attempts=5, window_s=300.0)


def get_login_limiter() -> LoginLimiter:
    """Возвращает текущий ограничитель попыток входа."""
    global login_limiter
    return login_limiter


def set_login_limiter(limiter: LoginLimiter) -> None:
    """Устанавливает текущий ограничитель попыток входа."""
    global login_limiter
    login_limiter = limiter


TRUSTED_PROXIES: set[str] = {"127.0.0.1", "::1", "localhost", "testclient"}


def get_client_ip(request: Request) -> str:
    """Извлекает IP-адрес клиента с защитой от спуфинга заголовков."""
    client_host = request.client.host if request.client and request.client.host else ""

    # Если клиент подключился напрямую (не через доверенный прокси),
    # возвращаем его реальный IP без доверия к X-Forwarded-For
    if client_host and client_host not in TRUSTED_PROXIES:
        return client_host

    # Если запрос пришел через доверенный прокси, извлекаем IP из X-Forwarded-For.
    # Обратный прокси (Caddy) дописывает реальный IP клиента в конец цепочки,
    # поэтому берем последний непустой IP из заголовка.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1]

    return client_host or "127.0.0.1"


_CLIENT_NAME_RE = re.compile(r"^[a-z0-9_-]+$")
_CLIENT_TOKEN_RE = re.compile(r"^[A-Za-z0-9_=-]+$")
_CLIENT_FLAG_RE = re.compile(r"^[a-z0-9_-]+$")


@dataclass(frozen=True)
class Client:
    name: str
    rate_limit_per_min: int | None = None
    allow_power: bool = True


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


def parse_client_tokens(raw: str) -> dict[str, Client]:
    """
    Парсит строку client_tokens формата:
    имя:токен[:лимит_в_минуту[:флаги]], записи через запятую.
    Флаги через '+'. Пример: pool:Abc-123_x:30:nopower.
    Возвращает dict[str, Client] с ключом токена.
    Некорректные записи пропускаются с предупреждением в лог (без токена в тексте).
    """
    if not raw or not isinstance(raw, str):
        return {}

    clients: dict[str, Client] = {}
    entries = raw.split(",")

    for item in entries:
        entry = item.strip()
        if not entry:
            continue

        parts = entry.split(":")
        if len(parts) < 2 or len(parts) > 4:
            logger.warning("Пропущена некорректная запись client_tokens: неверное количество полей")
            continue

        name = parts[0].strip()
        if not _CLIENT_NAME_RE.fullmatch(name):
            logger.warning("Пропущена некорректная запись client_tokens: недопустимое имя клиента '%s'", name)
            continue

        token = parts[1].strip()
        if not token:
            logger.warning("Пропущена некорректная запись client_tokens для клиента '%s': пустой токен", name)
            continue

        if not _CLIENT_TOKEN_RE.fullmatch(token):
            logger.warning("Пропущена некорректная запись client_tokens для клиента '%s': недопустимый формат токена", name)
            continue

        rate_limit_per_min: int | None = None
        if len(parts) >= 3:
            limit_raw = parts[2].strip()
            if limit_raw:
                try:
                    limit_val = int(limit_raw)
                    if limit_val <= 0:
                        logger.warning("Пропущена некорректная запись client_tokens для клиента '%s': лимит должен быть больше 0", name)
                        continue
                    rate_limit_per_min = limit_val
                except ValueError:
                    logger.warning("Пропущена некорректная запись client_tokens для клиента '%s': нечисловой лимит", name)
                    continue

        allow_power = True
        if len(parts) == 4:
            flags_raw = parts[3].strip()
            if flags_raw:
                flag_items = [f.strip() for f in flags_raw.split("+")]
                if any(not f or not _CLIENT_FLAG_RE.fullmatch(f) for f in flag_items):
                    logger.warning("Пропущена некорректная запись client_tokens для клиента '%s': некорректные флаги", name)
                    continue
                if "nopower" in flag_items:
                    allow_power = False

        if token in clients:
            logger.warning("Пропущена некорректная запись client_tokens для клиента '%s': дублирующийся токен", name)
            continue

        clients[token] = Client(
            name=name,
            rate_limit_per_min=rate_limit_per_min,
            allow_power=allow_power,
        )

    return clients


def resolve_token(token: str, settings: Settings | None = None) -> Client | None:
    """
    Разрешает токен авторизации в объект Client.
    Приоритет:
      1. client_tokens (из настроек HUB_CLIENT_TOKENS)
      2. legacy api_token -> Client(name="api")
      3. legacy mcp_token -> Client(name="mcp")
    Сравнение constant-time (hmac.compare_digest) по ВСЕМ кандидатам без раннего выхода.
    Пустые токены никогда не совпадают.
    """
    if settings is None:
        settings = get_settings()

    candidates: list[tuple[str, Client]] = []

    client_tokens_raw = getattr(settings, "client_tokens", "") or ""
    if client_tokens_raw:
        client_map = parse_client_tokens(client_tokens_raw)
        for c_token, client in client_map.items():
            candidates.append((c_token, client))

    api_token = getattr(settings, "api_token", "") or ""
    candidates.append((api_token, Client(name="api")))

    mcp_token = getattr(settings, "mcp_token", "") or ""
    candidates.append((mcp_token, Client(name="mcp")))

    matched_client: Client | None = None
    token_clean = token.strip() if (token and isinstance(token, str)) else ""

    for cand_tok, cand_client in candidates:
        cand_clean = cand_tok.strip() if (cand_tok and isinstance(cand_tok, str)) else ""
        if not token_clean or not cand_clean:
            hmac.compare_digest(token_clean or "dummy_a", cand_clean or "dummy_b")
            match = False
        else:
            match = hmac.compare_digest(token_clean, cand_clean)

        if match and matched_client is None:
            matched_client = cand_client

    return matched_client


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


def main(argv: list[str] | None = None) -> int:
    """CLI интерфейс модуля hub.auth."""
    parser = argparse.ArgumentParser(
        prog="python -m hub.auth",
        description="CLI авторизации DroidHub",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("hash", help="Спросить пароль через getpass и напечатать хэш для .env")

    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 2

    if args.command == "hash":
        try:
            if sys.stdin.isatty():
                pw = getpass.getpass("Пароль: ")
            elif getattr(getpass.getpass, "__module__", None) != "getpass":
                pw = getpass.getpass("Пароль: ")
            else:
                pw = sys.stdin.readline().rstrip("\r\n")
        except (io.UnsupportedOperation, OSError):
            pw = sys.stdin.readline().rstrip("\r\n")

        if not pw:
            print("Ошибка: пароль не может быть пустым", file=sys.stderr)
            return 1

        hashed = hash_password(pw)
        print(hashed)
        return 0

    return 1


if __name__ == "__main__":
    code = main()
    if code != 0:
        sys.exit(code)


__all__ = [
    "Client",
    "TRUSTED_PROXIES",
    "hash_password",
    "verify_password",
    "LoginLimiter",
    "login_limiter",
    "get_login_limiter",
    "set_login_limiter",
    "get_client_ip",
    "parse_client_tokens",
    "resolve_token",
    "get_sso_user",
    "require_user",
    "require_api",
    "auth_router",
    "main",
]
