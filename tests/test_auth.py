from __future__ import annotations

import getpass
import hashlib
import io
import subprocess
import sys
import time
from typing import Any

from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest
from starlette.middleware.sessions import SessionMiddleware

from hub.auth import (
    LoginLimiter,
    auth_router,
    get_client_ip,
    get_login_limiter,
    hash_password,
    main as cli_main,
    require_api,
    require_user,
    set_login_limiter,
    verify_password,
)
from hub.config import Settings, get_settings


# =====================================================================
# Тесты хэширования и проверки паролей
# =====================================================================


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


def test_verify_password_scrypt_raw_params() -> None:
    pw = "test_scrypt_raw"
    salt = "b" * 32
    dk = hashlib.scrypt(pw.encode("utf-8"), salt=bytes.fromhex(salt), n=1024, r=8, p=1, dklen=32).hex()
    hashed_raw = f"scrypt$1024,8,1${salt}${dk}"
    assert verify_password(pw, hashed_raw) is True


# =====================================================================
# Тесты LoginLimiter
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


def test_require_api_when_api_token_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_API_TOKEN", "")
    app = create_test_app()
    client = TestClient(app)

    res = client.get(
        "/api-protected",
        headers={"Authorization": "Bearer "},
    )
    assert res.status_code == status.HTTP_401_UNAUTHORIZED


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
    limiter = LoginLimiter(max_attempts=3, window_s=60.0)
    set_login_limiter(limiter)

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

