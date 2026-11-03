from __future__ import annotations

from dataclasses import FrozenInstanceError
import getpass
import hashlib
import io
import logging
import subprocess
import sys
import time
from typing import Any

from fastapi import Depends, FastAPI, Request, status
from fastapi.testclient import TestClient
import pytest
from starlette.middleware.sessions import SessionMiddleware

from hub.auth import (
    Client,
    LoginLimiter,
    TRUSTED_PROXIES,
    auth_router,
    get_client_ip,
    get_login_limiter,
    hash_password,
    main as cli_main,
    parse_client_tokens,
    require_api,
    require_user,
    resolve_token,
    set_login_limiter,
    verify_password,
)
from hub.config import Settings


# =====================================================================
# Тесты хэширования и проверки паролей
# =====================================================================


@pytest.mark.skipif(not hasattr(hashlib, "scrypt"), reason="нет hashlib.scrypt в этом Python")
def test_hash_password_format_scrypt() -> None:
    pw = "super_secret_123"
    hashed = hash_password(pw, algo="scrypt")
    parts = hashed.split("$")
    assert len(parts) == 4
    algo, params, salt, hash_hex = parts
    assert algo == "scrypt"
    assert "n=16384" in params
    assert "r=8" in params
    assert "p=1" in params
    assert len(salt) == 32  # 16 байт в hex
    assert len(hash_hex) == 64  # 32 байта в hex


def test_hash_password_format_pbkdf2() -> None:
    pw = "super_secret_123"
    hashed = hash_password(pw, algo="pbkdf2")
    parts = hashed.split("$")
    assert len(parts) == 4
    algo, params, salt, hash_hex = parts
    assert algo == "pbkdf2_sha256"
    assert "iterations=100000" in params
    assert len(salt) == 32
    assert len(hash_hex) == 64


def test_verify_password_success() -> None:
    pw = "correct_password"
    hashed = hash_password(pw)
    assert verify_password(pw, hashed) is True


def test_verify_password_failure() -> None:
    pw = "correct_password"
    hashed = hash_password(pw)
    assert verify_password("wrong_password", hashed) is False
    assert verify_password("", hashed) is False
    assert verify_password(pw, "") is False
    assert verify_password("correct_password", "invalid_format") is False
    assert verify_password("correct_password", "scrypt$params$salt") is False
    assert verify_password("correct_password", "unknown$n=1$123$abc") is False
    assert verify_password("correct_password", "scrypt$n=16384,r=8,p=1$not_hex$not_hex") is False


def test_hash_password_unique_salts() -> None:
    pw = "same_password"
    h1 = hash_password(pw)
    h2 = hash_password(pw)
    assert h1 != h2
    assert verify_password(pw, h1) is True
    assert verify_password(pw, h2) is True


def test_hash_password_unicode_cyrillic_and_emojis() -> None:
    pw = "Привет, мир! 🚀🔐 123 !@#"
    hashed = hash_password(pw)
    assert verify_password(pw, hashed) is True
    assert verify_password("Привет, мир!", hashed) is False


def test_verify_password_pbkdf2_variants() -> None:
    pw = "test_pbkdf2"
    hashed_kw = hash_password(pw, algo="pbkdf2")
    assert verify_password(pw, hashed_kw) is True

    # Проверка формата с числовыми параметрами без ключей (iterations)
    salt = "a" * 32
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), 1000, 32).hex()
    hashed_raw = f"pbkdf2$1000${salt}${dk}"
    assert verify_password(pw, hashed_raw) is True


@pytest.mark.skipif(not hasattr(hashlib, "scrypt"), reason="нет hashlib.scrypt в этом Python")
def test_verify_password_scrypt_raw_params() -> None:
    pw = "test_scrypt_raw"
    salt = "b" * 32
    dk = hashlib.scrypt(pw.encode("utf-8"), salt=bytes.fromhex(salt), n=1024, r=8, p=1, dklen=32).hex()
    hashed_raw = f"scrypt$1024,8,1${salt}${dk}"
    assert verify_password(pw, hashed_raw) is True


# =====================================================================
# Тесты LoginLimiter и управления лимитером
# =====================================================================


def test_login_limiter_defaults() -> None:
    limiter = LoginLimiter()
    assert limiter.max_attempts == 5
    assert limiter.window_s == 300.0
    assert limiter.check("1.2.3.4") is True


def test_login_limiter_lockout_and_reset() -> None:
    limiter = LoginLimiter(max_attempts=3, window_s=60.0)
    key = "192.168.1.10"

    assert limiter.check(key) is True
    limiter.fail(key)
    assert limiter.check(key) is True
    limiter.fail(key)
    assert limiter.check(key) is True
    limiter.fail(key)

    # 3 неудачных попытки исчерпаны
    assert limiter.check(key) is False

    # Сброс
    limiter.reset(key)
    assert limiter.check(key) is True


def test_login_limiter_independent_keys() -> None:
    limiter = LoginLimiter(max_attempts=2, window_s=60.0)
    key1 = "10.0.0.1"
    key2 = "10.0.0.2"

    limiter.fail(key1)
    limiter.fail(key1)
    assert limiter.check(key1) is False
    assert limiter.check(key2) is True


def test_login_limiter_window_expiry() -> None:
    limiter = LoginLimiter(max_attempts=1, window_s=0.05)
    key = "127.0.0.1"

    limiter.fail(key)
    assert limiter.check(key) is False

    # Ждём истечения временного окна
    time.sleep(0.07)
    assert limiter.check(key) is True


def test_get_and_set_login_limiter() -> None:
    original = get_login_limiter()
    custom = LoginLimiter(max_attempts=10, window_s=600.0)
    set_login_limiter(custom)
    try:
        assert get_login_limiter() is custom
    finally:
        set_login_limiter(original)


# =====================================================================
# Тесты get_client_ip и защиты от спуфинга
# =====================================================================


def test_get_client_ip_anti_spoofing() -> None:
    assert "127.0.0.1" in TRUSTED_PROXIES
    assert "::1" in TRUSTED_PROXIES

    # 1. Прямое подключение (не доверенный прокси)
    # Заголовок X-Forwarded-For должен игнорироваться, берется реальный адрес подключения
    req_direct = Request(
        scope={
            "type": "http",
            "client": ("198.51.100.25", 12345),
            "headers": [(b"x-forwarded-for", b"203.0.113.1, 10.0.0.1")],
        }
    )
    assert get_client_ip(req_direct) == "198.51.100.25"

    # 2. Прямое подключение без заголовка X-Forwarded-For
    req_direct_no_header = Request(
        scope={
            "type": "http",
            "client": ("198.51.100.25", 12345),
            "headers": [],
        }
    )
    assert get_client_ip(req_direct_no_header) == "198.51.100.25"

    # 3. Запрос через доверенный локальный прокси (127.0.0.1) с цепочкой IP
    # Caddy дописывает реальный IP в конец, берем последний элемент
    req_proxy_chain = Request(
        scope={
            "type": "http",
            "client": ("127.0.0.1", 54321),
            "headers": [(b"x-forwarded-for", b"spoofed_ip, 203.0.113.99")],
        }
    )
    assert get_client_ip(req_proxy_chain) == "203.0.113.99"

    # 4. Запрос через доверенный прокси (::1) с одним IP в заголовке
    req_proxy_ipv6 = Request(
        scope={
            "type": "http",
            "client": ("::1", 54321),
            "headers": [(b"x-forwarded-for", b"203.0.113.50")],
        }
    )
    assert get_client_ip(req_proxy_ipv6) == "203.0.113.50"

    # 5. Запрос через доверенный прокси без X-Forwarded-For
    req_proxy_no_header = Request(
        scope={
            "type": "http",
            "client": ("127.0.0.1", 54321),
            "headers": [],
        }
    )
    assert get_client_ip(req_proxy_no_header) == "127.0.0.1"

    # 6. Запрос без client (например, внутренний вызов/mock)
    req_no_client = Request(
        scope={
            "type": "http",
            "headers": [(b"x-forwarded-for", b"198.51.100.77")],
        }
    )
    assert get_client_ip(req_no_client) == "198.51.100.77"


# =====================================================================
# Тесты FastAPI зависимостей require_user и require_api
# =====================================================================


def create_test_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test_session_secret")

    @app.get("/user-only")
    def user_only(user: str = Depends(require_user)) -> dict[str, str]:
        return {"user": user}

    @app.get("/api-protected")
    def api_protected(actor: str = Depends(require_api)) -> dict[str, str]:
        return {"actor": actor}

    @app.post("/set-session")
    def set_session(request: Request) -> dict[str, bool]:
        request.session["user"] = "admin_user"
        return {"ok": True}

    return app


def test_require_user_unauthorized() -> None:
    app = create_test_app()
    client = TestClient(app)
    response = client.get("/user-only")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json()["detail"] == "Требуется авторизация"


def test_require_user_authorized() -> None:
    app = create_test_app()
    client = TestClient(app)

    # Устанавливаем сессию
    login_res = client.post("/set-session")
    assert login_res.status_code == 200

    # Проверяем доступ
    response = client.get("/user-only")
    assert response.status_code == 200
    assert response.json() == {"user": "admin_user"}


def test_require_api_with_session() -> None:
    app = create_test_app()
    client = TestClient(app)

    client.post("/set-session")
    response = client.get("/api-protected")
    assert response.status_code == 200
    assert response.json() == {"actor": "admin_user"}


def test_require_api_with_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_API_TOKEN", "valid_api_token_123")
    app = create_test_app()
    client = TestClient(app)

    # Без авторизации -> 401
    res_no_auth = client.get("/api-protected")
    assert res_no_auth.status_code == status.HTTP_401_UNAUTHORIZED

    # С неверным токеном -> 401
    res_wrong_token = client.get(
        "/api-protected",
        headers={"Authorization": "Bearer wrong_token"},
    )
    assert res_wrong_token.status_code == status.HTTP_401_UNAUTHORIZED

    # С не-Bearer схемой -> 401
    res_basic = client.get(
        "/api-protected",
        headers={"Authorization": "Basic valid_api_token_123"},
    )
    assert res_basic.status_code == status.HTTP_401_UNAUTHORIZED

    # С корректным токеном -> 200, actor='api'
    res_valid = client.get(
        "/api-protected",
        headers={"Authorization": "Bearer valid_api_token_123"},
    )
    assert res_valid.status_code == 200
    assert res_valid.json() == {"actor": "api"}


def test_require_api_token_empty_and_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Токен в HUB_API_TOKEN пустой или состоит из пробелов
    for empty_val in ["", "   "]:
        monkeypatch.setenv("HUB_API_TOKEN", empty_val)
        app = create_test_app()
        client = TestClient(app)

        res_empty = client.get(
            "/api-protected",
            headers={"Authorization": "Bearer "},
        )
        assert res_empty.status_code == status.HTTP_401_UNAUTHORIZED

        res_spaces = client.get(
            "/api-protected",
            headers={"Authorization": "Bearer   "},
        )
        assert res_spaces.status_code == status.HTTP_401_UNAUTHORIZED

        res_any = client.get(
            "/api-protected",
            headers={"Authorization": "Bearer some_token"},
        )
        assert res_any.status_code == status.HTTP_401_UNAUTHORIZED

    # 2. Токен задан, но запрос шлет пустой или пробельный Bearer токен
    monkeypatch.setenv("HUB_API_TOKEN", "my_secret_token")
    app = create_test_app()
    client = TestClient(app)

    assert client.get("/api-protected", headers={"Authorization": "Bearer"}).status_code == 401
    assert client.get("/api-protected", headers={"Authorization": "Bearer "}).status_code == 401
    assert client.get("/api-protected", headers={"Authorization": "Bearer   "}).status_code == 401
    assert (
        client.get("/api-protected", headers={"Authorization": "Bearer  my_secret_token  "}).status_code
        == 200
    )


# =====================================================================
# Тесты работы без SessionMiddleware
# =====================================================================


def test_require_user_without_session_middleware() -> None:
    app = FastAPI()

    @app.get("/user-only")
    def user_only(user: str = Depends(require_user)) -> dict[str, str]:
        return {"user": user}

    client = TestClient(app)
    # Должен возвращаться 401 Unauthorized, а не падать с 500 / AssertionError
    response = client.get("/user-only")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json()["detail"] == "Требуется авторизация"


def test_require_api_without_session_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_API_TOKEN", "api_token_no_session")
    app = FastAPI()

    @app.get("/api-protected")
    def api_protected(actor: str = Depends(require_api)) -> dict[str, str]:
        return {"actor": actor}

    client = TestClient(app)

    # Без заголовка -> 401
    res_no_auth = client.get("/api-protected")
    assert res_no_auth.status_code == status.HTTP_401_UNAUTHORIZED

    # С неверным токеном -> 401
    res_wrong = client.get(
        "/api-protected",
        headers={"Authorization": "Bearer wrong_token"},
    )
    assert res_wrong.status_code == status.HTTP_401_UNAUTHORIZED

    # С валидным Bearer-токеном -> 200, actor='api' (без 500 из-за отсутствия SessionMiddleware)
    res_valid = client.get(
        "/api-protected",
        headers={"Authorization": "Bearer api_token_no_session"},
    )
    assert res_valid.status_code == status.HTTP_200_OK
    assert res_valid.json() == {"actor": "api"}


def test_auth_router_without_session_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_pw = "pass_no_session"
    hashed = hash_password(admin_pw)
    monkeypatch.setenv("HUB_ADMIN_USER", "admin")
    monkeypatch.setenv("HUB_ADMIN_PASSWORD_HASH", hashed)

    app = FastAPI()
    app.include_router(auth_router)
    client = TestClient(app, follow_redirects=False)

    # Успешный вход без SessionMiddleware не падает с 500, а выполняет редирект на /
    res_login = client.post(
        "/login",
        data={"username": "admin", "password": admin_pw},
    )
    assert res_login.status_code == status.HTTP_303_SEE_OTHER
    assert res_login.headers["location"] == "/"

    # Выход без SessionMiddleware также не падает с 500
    res_logout = client.post("/logout")
    assert res_logout.status_code == status.HTTP_303_SEE_OTHER
    assert res_logout.headers["location"] == "/login"


# =====================================================================
# Тесты auth_router (POST /login, POST /logout)
# =====================================================================


def create_auth_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test_session_secret")
    app.include_router(auth_router)

    @app.get("/check-session")
    def check_session(request: Request) -> dict[str, Any]:
        return {"user": request.session.get("user")}

    return app


def test_auth_router_login_success(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_pw = "secret_admin_pass"
    hashed = hash_password(admin_pw)
    monkeypatch.setenv("HUB_ADMIN_USER", "admin")
    monkeypatch.setenv("HUB_ADMIN_PASSWORD_HASH", hashed)

    app = create_auth_app()
    client = TestClient(app, follow_redirects=False)

    # 1. Вход через форму (application/x-www-form-urlencoded)
    response = client.post(
        "/login",
        data={"username": "admin", "password": admin_pw},
    )
    assert response.status_code == status.HTTP_303_SEE_OTHER
    assert response.headers["location"] == "/"

    # Проверяем наличие пользователя в сессии
    check = client.get("/check-session")
    assert check.json() == {"user": "admin"}


def test_auth_router_login_json_success(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_pw = "json_admin_pass"
    hashed = hash_password(admin_pw)
    monkeypatch.setenv("HUB_ADMIN_USER", "json_admin")
    monkeypatch.setenv("HUB_ADMIN_PASSWORD_HASH", hashed)

    app = create_auth_app()
    client = TestClient(app, follow_redirects=False)

    # Вход через JSON
    response = client.post(
        "/login",
        json={"username": "json_admin", "password": admin_pw},
    )
    assert response.status_code == status.HTTP_303_SEE_OTHER
    assert response.headers["location"] == "/"

    check = client.get("/check-session")
    assert check.json() == {"user": "json_admin"}


def test_auth_router_login_invalid_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_pw = "valid_password"
    hashed = hash_password(admin_pw)
    monkeypatch.setenv("HUB_ADMIN_USER", "admin")
    monkeypatch.setenv("HUB_ADMIN_PASSWORD_HASH", hashed)

    app = create_auth_app()
    client = TestClient(app, follow_redirects=False)

    # Неверный пароль
    res_wrong_pw = client.post(
        "/login",
        data={"username": "admin", "password": "wrong_password"},
    )
    assert res_wrong_pw.status_code == status.HTTP_303_SEE_OTHER
    assert res_wrong_pw.headers["location"] == "/login?error=1"

    # Неверный логин
    res_wrong_user = client.post(
        "/login",
        data={"username": "not_admin", "password": admin_pw},
    )
    assert res_wrong_user.status_code == status.HTTP_303_SEE_OTHER
    assert res_wrong_user.headers["location"] == "/login?error=1"

    # Пустые данные
    res_empty = client.post("/login", data={})
    assert res_empty.status_code == status.HTTP_303_SEE_OTHER
    assert res_empty.headers["location"] == "/login?error=1"

    # Сессия осталась пустой
    check = client.get("/check-session")
    assert check.json() == {"user": None}


def test_auth_router_login_rate_limiting(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_pw = "secure_pw_123"
    hashed = hash_password(admin_pw)
    monkeypatch.setenv("HUB_ADMIN_USER", "admin")
    monkeypatch.setenv("HUB_ADMIN_PASSWORD_HASH", hashed)

    # Устанавливаем лимитер с ограничением в 3 попытки
    original_limiter = get_login_limiter()
    limiter = LoginLimiter(max_attempts=3, window_s=60.0)
    set_login_limiter(limiter)

    try:
        app = create_auth_app()
        client = TestClient(app, follow_redirects=False)

        # 3 неудачные попытки
        for _ in range(3):
            res = client.post(
                "/login",
                data={"username": "admin", "password": "bad_password"},
                headers={"X-Forwarded-For": "203.0.113.1"},
            )
            assert res.status_code == status.HTTP_303_SEE_OTHER
            assert res.headers["location"] == "/login?error=1"

        # 4-я попытка с ПРАВИЛЬНЫМ паролем с того же IP блокируется
        res_blocked = client.post(
            "/login",
            data={"username": "admin", "password": admin_pw},
            headers={"X-Forwarded-For": "203.0.113.1"},
        )
        assert res_blocked.status_code == status.HTTP_303_SEE_OTHER
        assert res_blocked.headers["location"] == "/login?error=1"

        check = client.get("/check-session")
        assert check.json() == {"user": None}

        # Попытка с другого IP проходит успешно
        res_other_ip = client.post(
            "/login",
            data={"username": "admin", "password": admin_pw},
            headers={"X-Forwarded-For": "203.0.113.2"},
        )
        assert res_other_ip.status_code == status.HTTP_303_SEE_OTHER
        assert res_other_ip.headers["location"] == "/"
    finally:
        set_login_limiter(original_limiter)


def test_auth_router_logout(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_pw = "test_pass"
    hashed = hash_password(admin_pw)
    monkeypatch.setenv("HUB_ADMIN_USER", "admin")
    monkeypatch.setenv("HUB_ADMIN_PASSWORD_HASH", hashed)

    app = create_auth_app()
    client = TestClient(app, follow_redirects=False)

    # Входим
    login_res = client.post(
        "/login",
        data={"username": "admin", "password": admin_pw},
    )
    assert login_res.status_code == status.HTTP_303_SEE_OTHER

    check = client.get("/check-session")
    assert check.json() == {"user": "admin"}

    # Выходим через POST /logout
    logout_res = client.post("/logout")
    assert logout_res.status_code == status.HTTP_303_SEE_OTHER
    assert logout_res.headers["location"] == "/login"

    # Сессия очищена
    check_after = client.get("/check-session")
    assert check_after.json() == {"user": None}

    # GET /logout также поддерживается
    get_logout_res = client.get("/logout")
    assert get_logout_res.status_code == status.HTTP_303_SEE_OTHER
    assert get_logout_res.headers["location"] == "/login"


# =====================================================================
# Тесты CLI python -m hub.auth hash
# =====================================================================


def test_cli_hash_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    secret_pw = "my_cli_pass_123"
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": secret_pw)

    code = cli_main(["hash"])
    assert code == 0

    out, err = capsys.readouterr()
    hash_str = out.strip()
    assert hash_str.startswith("scrypt$") or hash_str.startswith("pbkdf2_sha256$")
    assert verify_password(secret_pw, hash_str) is True


def test_cli_hash_empty_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "")

    code = cli_main(["hash"])
    assert code == 1

    out, err = capsys.readouterr()
    assert "не может быть пустым" in err


def test_cli_hash_fallback_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def raise_oserror(prompt: str = "") -> str:
        raise OSError("Inappropriate ioctl for device")

    monkeypatch.setattr(getpass, "getpass", raise_oserror)
    monkeypatch.setattr(sys, "stdin", io.StringIO("stdin_pass\n"))

    code = cli_main(["hash"])
    assert code == 0

    out, err = capsys.readouterr()
    hash_str = out.strip()
    assert verify_password("stdin_pass", hash_str) is True


def test_cli_no_args() -> None:
    code = cli_main([])
    assert code != 0


def test_cli_unknown_arg() -> None:
    code = cli_main(["unknown_command"])
    assert code != 0


def test_cli_subprocess_pipe() -> None:
    res = subprocess.run(
        [sys.executable, "-m", "hub.auth", "hash"],
        input="pipe_cli_secret\n",
        text=True,
        capture_output=True,
    )
    assert res.returncode == 0
    hashed = res.stdout.strip()
    assert verify_password("pipe_cli_secret", hashed) is True


# =====================================================================
# Тесты этапа 4: токены по клиентам (Client, parse_client_tokens, resolve_token, require_api)
# =====================================================================


def test_client_dataclass() -> None:
    # Дефолтные значения
    c1 = Client(name="test_client")
    assert c1.name == "test_client"
    assert c1.rate_limit_per_min is None
    assert c1.allow_power is True

    # Пользовательские значения
    c2 = Client(name="pool", rate_limit_per_min=30, allow_power=False)
    assert c2.name == "pool"
    assert c2.rate_limit_per_min == 30
    assert c2.allow_power is False

    # Неизменяемость (frozen=True)
    with pytest.raises(FrozenInstanceError):
        c1.name = "new_name"  # type: ignore[misc]


def test_parse_client_tokens_format_with_limit_and_flags() -> None:
    # 1. Формат с лимитом и флагом nopower
    raw1 = "pool:Abc-123_x:30:nopower"
    res1 = parse_client_tokens(raw1)
    assert len(res1) == 1
    assert "Abc-123_x" in res1
    assert res1["Abc-123_x"] == Client(name="pool", rate_limit_per_min=30, allow_power=False)

    # 2. Формат только с лимитом
    raw2 = "claude:Def-456_y:60"
    res2 = parse_client_tokens(raw2)
    assert res2["Def-456_y"] == Client(name="claude", rate_limit_per_min=60, allow_power=True)

    # 3. Формат без лимита и флагов (только имя и токен)
    raw3 = "bot:Ghi-789_z"
    res3 = parse_client_tokens(raw3)
    assert res3["Ghi-789_z"] == Client(name="bot", rate_limit_per_min=None, allow_power=True)

    # 4. Несколько флагов через '+' включая nopower
    raw4 = "agent:Jkl-012_w:10:otherflag+nopower+audit"
    res4 = parse_client_tokens(raw4)
    assert res4["Jkl-012_w"] == Client(name="agent", rate_limit_per_min=10, allow_power=False)

    # 5. Флаги через '+' без nopower
    raw5 = "admin:Mno-345_v:100:audit+readonly"
    res5 = parse_client_tokens(raw5)
    assert res5["Mno-345_v"] == Client(name="admin", rate_limit_per_min=100, allow_power=True)

    # 6. Пустой сегмент флага
    raw6 = "service:Pqr-678_u:20:"
    res6 = parse_client_tokens(raw6)
    assert res6["Pqr-678_u"] == Client(name="service", rate_limit_per_min=20, allow_power=True)


def test_parse_client_tokens_multiple_clients() -> None:
    raw = (
        " pool:Abc-123_x:30:nopower , "
        "claude:Def-456_y:60, "
        "bot:Ghi-789_z , "
        "pool:Backup-Token_99:10 "
    )
    res = parse_client_tokens(raw)
    assert len(res) == 4
    assert res["Abc-123_x"] == Client(name="pool", rate_limit_per_min=30, allow_power=False)
    assert res["Def-456_y"] == Client(name="claude", rate_limit_per_min=60, allow_power=True)
    assert res["Ghi-789_z"] == Client(name="bot", rate_limit_per_min=None, allow_power=True)
    assert res["Backup-Token_99"] == Client(name="pool", rate_limit_per_min=10, allow_power=True)


def test_parse_client_tokens_invalid_records(caplog: pytest.LogCaptureFixture) -> None:
    secret_bad_token1 = "SECRET_BAD_TOKEN_1"
    secret_bad_token2 = "SECRET_BAD_TOKEN_2"

    raw = (
        f"valid_one:Valid-Token_1:15,"
        f"bad_no_colon,"
        f"too:many:colons:in:this:record,"
        f"UPPERCASE:{secret_bad_token1}:10,"
        f"name with space:Tok1:10,"
        f"name@bad:Tok2:10,"
        f":EmptyNameTok:10,"
        f"client_empty_token:,"
        f"client_spaces_token:   :20,"
        f"client_bad_chars:bad tok space:10,"
        f"client_bad_symbol:bad!token:10,"
        f"client_bad_limit:Tok3:notanumber,"
        f"client_zero_limit:Tok4:0,"
        f"client_neg_limit:Tok5:-5,"
        f"client_bad_flag:Tok6:10:bad!flag,"
        f"client_empty_flag:Tok7:10:flag1++flag2,"
        f"valid_two:Valid-Token_2:25:nopower"
    )

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        res = parse_client_tokens(raw)

    # Только корректные записи попали в результат
    assert len(res) == 2
    assert "Valid-Token_1" in res
    assert "Valid-Token_2" in res
    assert res["Valid-Token_1"] == Client(name="valid_one", rate_limit_per_min=15, allow_power=True)
    assert res["Valid-Token_2"] == Client(name="valid_two", rate_limit_per_min=25, allow_power=False)

    # Проверяем логирование предупреждений
    assert len(caplog.records) > 0
    all_log_text = " ".join(record.message for record in caplog.records)

    # КРИТИЧНО: токены не должны утекать в лог
    for bad_tok in (
        secret_bad_token1,
        secret_bad_token2,
        "Valid-Token_1",
        "Valid-Token_2",
        "Tok1",
        "Tok2",
        "Tok3",
        "Tok4",
        "Tok5",
        "Tok6",
        "Tok7",
        "bad tok space",
        "bad!token",
    ):
        assert bad_tok not in all_log_text


def test_parse_client_tokens_duplicate_tokens(caplog: pytest.LogCaptureFixture) -> None:
    raw = "client1:Duplicate-Token-123:30, client2:Duplicate-Token-123:60"
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        res = parse_client_tokens(raw)

    assert len(res) == 1
    assert res["Duplicate-Token-123"] == Client(name="client1", rate_limit_per_min=30, allow_power=True)
    all_log_text = " ".join(record.message for record in caplog.records)
    assert "Duplicate-Token-123" not in all_log_text
    assert "client2" in all_log_text


def test_empty_tokens_do_not_match() -> None:
    # 1. parse_client_tokens при пустых входных данных
    assert parse_client_tokens("") == {}
    assert parse_client_tokens("   ") == {}
    assert parse_client_tokens(",  ,  ,") == {}

    # 2. resolve_token с пустыми токенами
    settings = Settings(
        client_tokens="pool:valid_tok:30",
        api_token="",
        mcp_token="",
    )
    assert resolve_token("", settings=settings) is None
    assert resolve_token("   ", settings=settings) is None

    # 3. Пустой токен запроса не совпадает даже с пустым legacy api_token или mcp_token
    settings_all_empty = Settings(client_tokens="", api_token="", mcp_token="")
    assert resolve_token("", settings=settings_all_empty) is None
    assert resolve_token("   ", settings=settings_all_empty) is None
    assert resolve_token("api", settings=settings_all_empty) is None
    assert resolve_token("mcp", settings=settings_all_empty) is None


def test_resolve_token_legacy_and_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Legacy api_token -> Client(name="api")
    settings_api = Settings(api_token="legacy_api_token_val")
    res_api = resolve_token("legacy_api_token_val", settings=settings_api)
    assert res_api == Client(name="api", rate_limit_per_min=None, allow_power=True)

    # 2. Legacy mcp_token -> Client(name="mcp")
    settings_mcp = Settings(mcp_token="legacy_mcp_token_val")
    res_mcp = resolve_token("legacy_mcp_token_val", settings=settings_mcp)
    assert res_mcp == Client(name="mcp", rate_limit_per_min=None, allow_power=True)

    # 3. Приоритет: client_tokens выше legacy api_token и mcp_token
    settings_priority = Settings(
        client_tokens="pool:shared_token_1:30:nopower",
        api_token="shared_token_1",
        mcp_token="shared_token_2",
    )
    res_priority = resolve_token("shared_token_1", settings=settings_priority)
    assert res_priority == Client(name="pool", rate_limit_per_min=30, allow_power=False)

    # 4. Приоритет: legacy api_token выше legacy mcp_token
    settings_both_legacy = Settings(
        api_token="same_legacy_token",
        mcp_token="same_legacy_token",
    )
    res_both = resolve_token("same_legacy_token", settings=settings_both_legacy)
    assert res_both == Client(name="api")

    # 5. Неверный токен
    assert resolve_token("completely_unknown_token", settings=settings_priority) is None

    # 6. Чтение настроек по умолчанию из get_settings() при settings=None
    monkeypatch.setenv("HUB_CLIENT_TOKENS", "agent:env_agent_tok_456:15")
    monkeypatch.delenv("HUB_API_TOKEN", raising=False)
    monkeypatch.delenv("HUB_MCP_TOKEN", raising=False)
    res_default_settings = resolve_token("env_agent_tok_456")
    assert res_default_settings == Client(name="agent", rate_limit_per_min=15, allow_power=True)


def test_resolve_token_constant_time_evaluates_all_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        client_tokens="c1:tok1:10, c2:tok2:20, c3:tok3:30",
        api_token="legacy_api",
        mcp_token="legacy_mcp",
    )
    # Всего кандидатов: 3 client_tokens + 1 api_token + 1 mcp_token = 5

    calls: list[tuple[str, str]] = []
    real_compare = hashlib.sha256  # just reference

    import hmac

    original_compare = hmac.compare_digest

    def spy_compare(a: Any, b: Any) -> bool:
        calls.append((str(a), str(b)))
        return original_compare(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy_compare)

    # Проверяем первый же токен кандидата: совпадение на шаге 1,
    # но цикл обязан проверить ВСЕ 5 кандидатов без раннего break/return
    calls.clear()
    res = resolve_token("tok1", settings=settings)
    assert res == Client(name="c1", rate_limit_per_min=10, allow_power=True)
    assert len(calls) == 5

    # Проверяем несуществующий токен: также ровно 5 сравнений
    calls.clear()
    res_none = resolve_token("nonexistent", settings=settings)
    assert res_none is None
    assert len(calls) == 5


def test_require_api_client_and_state(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test_state_secret")

    @app.get("/api/state-info")
    def state_info(request: Request, actor: str = Depends(require_api)) -> dict[str, Any]:
        client = getattr(request.state, "client", None)
        return {
            "actor": actor,
            "has_client": client is not None,
            "client_name": client.name if client else None,
            "rate_limit_per_min": client.rate_limit_per_min if client else None,
            "allow_power": client.allow_power if client else None,
        }

    @app.post("/set-user-session")
    def set_user_session(request: Request) -> dict[str, bool]:
        request.session["user"] = "login_user_bob"
        return {"ok": True}

    client = TestClient(app)

    # 1. Авторизация через сессию панели -> Client(name=логин)
    client.post("/set-user-session")
    res_session = client.get("/api/state-info")
    assert res_session.status_code == 200
    data_session = res_session.json()
    assert data_session["actor"] == "login_user_bob"
    assert data_session["has_client"] is True
    assert data_session["client_name"] == "login_user_bob"
    assert data_session["rate_limit_per_min"] is None
    assert data_session["allow_power"] is True

    # 2. Клиент без сессии, токен из client_tokens
    monkeypatch.setenv("HUB_CLIENT_TOKENS", "pool:pool_token_xyz:40:nopower")
    monkeypatch.delenv("HUB_API_TOKEN", raising=False)
    monkeypatch.delenv("HUB_MCP_TOKEN", raising=False)
    app2 = FastAPI()
    app2.add_middleware(SessionMiddleware, secret_key="test_state_secret_2")

    @app2.get("/api/state-info")
    def state_info2(request: Request, actor: str = Depends(require_api)) -> dict[str, Any]:
        client_obj = getattr(request.state, "client", None)
        return {
            "actor": actor,
            "client_name": client_obj.name if client_obj else None,
            "rate_limit_per_min": client_obj.rate_limit_per_min if client_obj else None,
            "allow_power": client_obj.allow_power if client_obj else None,
        }

    client2 = TestClient(app2)
    res_bearer_pool = client2.get(
        "/api/state-info",
        headers={"Authorization": "Bearer pool_token_xyz"},
    )
    assert res_bearer_pool.status_code == 200
    data_pool = res_bearer_pool.json()
    assert data_pool["actor"] == "pool"
    assert data_pool["client_name"] == "pool"
    assert data_pool["rate_limit_per_min"] == 40
    assert data_pool["allow_power"] is False

    # 3. Клиент через legacy api_token -> Client(name="api")
    monkeypatch.delenv("HUB_CLIENT_TOKENS", raising=False)
    monkeypatch.setenv("HUB_API_TOKEN", "legacy_token_api_999")
    res_bearer_api = client2.get(
        "/api/state-info",
        headers={"Authorization": "Bearer legacy_token_api_999"},
    )
    assert res_bearer_api.status_code == 200
    data_api = res_bearer_api.json()
    assert data_api["actor"] == "api"
    assert data_api["client_name"] == "api"
    assert data_api["rate_limit_per_min"] is None
    assert data_api["allow_power"] is True

    # 4. Клиент через legacy mcp_token -> Client(name="mcp")
    monkeypatch.delenv("HUB_API_TOKEN", raising=False)
    monkeypatch.setenv("HUB_MCP_TOKEN", "legacy_token_mcp_555")
    res_bearer_mcp = client2.get(
        "/api/state-info",
        headers={"Authorization": "Bearer legacy_token_mcp_555"},
    )
    assert res_bearer_mcp.status_code == 200
    data_mcp = res_bearer_mcp.json()
    assert data_mcp["actor"] == "mcp"
    assert data_mcp["client_name"] == "mcp"
    assert data_mcp["rate_limit_per_min"] is None
    assert data_mcp["allow_power"] is True

    # 5. Ошибочный токен
    res_unauth = client2.get(
        "/api/state-info",
        headers={"Authorization": "Bearer invalid_tok"},
    )
    assert res_unauth.status_code == status.HTTP_401_UNAUTHORIZED

