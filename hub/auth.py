from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import io
import secrets
import sys
import time
import urllib.parse

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from hub.config import get_settings


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


def require_user(request: Request) -> str:
    """
    FastAPI dependency: требует авторизованного пользователя в сессии.
    Возвращает логин пользователя или вызывает HTTPException(401).
    """
    user = None
    if "session" in request.scope:
        user = request.session.get("user")

    if user and isinstance(user, str) and user.strip():
        return user.strip()

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Требуется авторизация",
    )


def require_api(request: Request) -> str:
    """
    FastAPI dependency: сессия панели ИЛИ заголовок Authorization: Bearer <api_token>.
    Возвращает actor: логин из сессии или 'api'. Сравнение токенов через compare_digest.
    """
    # 1. Сессия панели
    if "session" in request.scope:
        user = request.session.get("user")
        if user and isinstance(user, str) and user.strip():
            return user.strip()

    # 2. Bearer токен API
    api_token = (get_settings().api_token or "").strip()
    if not api_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Не авторизован",
            headers={"WWW-Authenticate": "Bearer"},
        )

    auth_header = request.headers.get("Authorization")
    if auth_header:
        scheme, _, raw_token = auth_header.partition(" ")
        token = raw_token.strip()
        if scheme.lower() == "bearer" and token:
            if hmac.compare_digest(token, api_token):
                return "api"

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
    """Выход из панели: очистка сессии и редирект 303 на /login."""
    if "session" in request.scope:
        request.session.clear()
    return RedirectResponse(
        url="/login",
        status_code=status.HTTP_303_SEE_OTHER,
    )


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
    "TRUSTED_PROXIES",
    "hash_password",
    "verify_password",
    "LoginLimiter",
    "login_limiter",
    "get_login_limiter",
    "set_login_limiter",
    "get_client_ip",
    "require_user",
    "require_api",
    "auth_router",
    "main",
]
