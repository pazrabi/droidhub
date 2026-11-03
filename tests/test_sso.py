from __future__ import annotations

import time
import urllib.parse

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from hub.auth import LoginLimiter, hash_password, set_login_limiter
from hub.config import Settings
from hub.main import create_app
from hub.sso import make_cookie, safe_next, verify_cookie
from hub.sso_routes import sso_router


# =====================================================================
# 1. Тесты hub/sso.py: make_cookie и verify_cookie
# =====================================================================


def test_make_cookie_and_verify_cookie_roundtrip() -> None:
    secret = "test_super_secret_key_12345"
    user = "admin"
    ttl = 3600
    now = 1700000000.0

    cookie = make_cookie(user=user, secret=secret, ttl_seconds=ttl, now=now)
    assert isinstance(cookie, str)
    parts = cookie.split("|")
    assert len(parts) == 3
    assert parts[0] == "admin"
    assert parts[1] == str(int(now + ttl))
    assert len(parts[2]) == 64  # sha256 hex digest

    # Успешная верификация
    verified_user = verify_cookie(cookie, secret=secret, now=now + 100)
    assert verified_user == "admin"


def test_make_cookie_allowed_user_characters() -> None:
    secret = "test_secret_key"
    allowed_users = [
        "admin",
        "user123",
        "user.name",
        "user-name",
        "user_name",
        "user@domain.com",
        "A-Z.0_9@-",
    ]
    for u in allowed_users:
        cookie = make_cookie(u, secret, ttl_seconds=60)
        assert verify_cookie(cookie, secret) == u


def test_make_cookie_invalid_inputs() -> None:
    secret = "test_secret"
    # Недопустимые символы в имени пользователя
    for bad_user in ["", "admin|hacker", "user name", "user/name", "admin\n", "админ"]:
        with pytest.raises(ValueError):
            make_cookie(bad_user, secret, ttl_seconds=60)

    # Пустой секрет
    with pytest.raises(ValueError):
        make_cookie("admin", "", ttl_seconds=60)

    # Неположительный TTL
    with pytest.raises(ValueError):
        make_cookie("admin", secret, ttl_seconds=0)
    with pytest.raises(ValueError):
        make_cookie("admin", secret, ttl_seconds=-10)


def test_verify_cookie_tampered_and_invalid() -> None:
    secret = "test_secret"
    valid_cookie = make_cookie("admin", secret, ttl_seconds=3600, now=1000.0)

    # 1. Изменение подписи
    tampered_sig = valid_cookie[:-1] + ("0" if valid_cookie[-1] != "0" else "1")
    assert verify_cookie(tampered_sig, secret, now=1000.0) is None

    # 2. Изменение имени пользователя в payload
    parts = valid_cookie.split("|")
    tampered_user = f"hacker|{parts[1]}|{parts[2]}"
    assert verify_cookie(tampered_user, secret, now=1000.0) is None

    # 3. Изменение времени истечения
    tampered_time = f"{parts[0]}|9999999999|{parts[2]}"
    assert verify_cookie(tampered_time, secret, now=1000.0) is None

    # 4. Проверка с другим секретом
    assert verify_cookie(valid_cookie, "wrong_secret", now=1000.0) is None

    # 5. Проверка с пустым секретом
    assert verify_cookie(valid_cookie, "", now=1000.0) is None

    # 6. Битое значение cookie (не 3 части)
    for bad_val in ["", "singlepart", "two|parts", "too|many|parts|here", "||"]:
        assert verify_cookie(bad_val, secret, now=1000.0) is None

    # 7. Нечисловой timestamp
    assert verify_cookie("admin|not_a_number|abcdef", secret, now=1000.0) is None

    # 8. Недопустимые символы в имени пользователя
    assert verify_cookie("user name|2000|0000000000000000000000000000000000000000000000000000000000000000", secret, now=1000.0) is None


def test_verify_cookie_expired() -> None:
    secret = "test_secret"
    now_created = 1000.0
    ttl = 60
    # expires at 1060
    cookie = make_cookie("admin", secret, ttl_seconds=ttl, now=now_created)

    # До истечения срока: валиден
    assert verify_cookie(cookie, secret, now=1059.0) == "admin"

    # В момент истечения (или чуть позже): недействителен
    assert verify_cookie(cookie, secret, now=1061.0) is None
    assert verify_cookie(cookie, secret, now=2000.0) is None


# =====================================================================
# 2. Тесты hub/sso.py: safe_next (десять атакующих примеров)
# =====================================================================


def test_safe_next_valid_urls() -> None:
    suffix = "molokohub.site"

    valid_cases = [
        "https://molokohub.site",
        "https://molokohub.site/",
        "https://pool.molokohub.site/",
        "https://droidhub.molokohub.site/device/1",
        "https://pool.molokohub.site/dashboard?view=grid&sort=asc#summary",
        "https://sub.pool.molokohub.site:8443/custom",
    ]
    for url in valid_cases:
        assert safe_next(url, suffix) == url
        # Проверка, когда allowed_suffix начинается с точки
        assert safe_next(url, ".molokohub.site") == url


def test_safe_next_attacks() -> None:
    suffix = "molokohub.site"

    attacks = [
        # 1. Схема http вместо https
        "http://molokohub.site/",
        # 2. Протокольно-относительный URL (//)
        "//molokohub.site/",
        # 3. Подмена префикса домена (evil-molokohub.site)
        "https://evil-molokohub.site/",
        # 4. Подмена суффикса домена (molokohub.site.evil.com)
        "https://molokohub.site.evil.com/",
        # 5. Userinfo трюк с символом @
        "https://evil.com@molokohub.site/",
        # 6. Обратный userinfo трюк
        "https://molokohub.site@evil.com/",
        # 7. Обратный слэш для обхода парсера (\)
        "https://molokohub.site\\evil.com/",
        # 8. Схема javascript
        "javascript:alert(document.cookie)",
        # 9. Сторонний хост с легитимным URL в query
        "https://evil.com/?next=https://molokohub.site",
        # 10. Протокольно-относительный URL с userinfo
        "//evil.com@molokohub.site",
        # 11. URL-encoded символ @ (%40)
        "https://evil.com%40molokohub.site/",
        # 12. URL-encoded обратный слэш (%5c)
        "https://molokohub.site%5cevil.com/",
        # 13. Превышение максимальной длины 500 символов
        "https://" + ("a" * 490) + ".molokohub.site/",
        # 14. Некорректный порт
        "https://molokohub.site:evil.com/",
        # 15. Тройной слэш (пустой netloc)
        "https:///molokohub.site",
        # 16. Некорректные точки в начале хоста
        "https://.molokohub.site/",
    ]

    for attack_url in attacks:
        result = safe_next(attack_url, suffix)
        assert result is None, f"Атакующий URL должен быть отклонен: {attack_url!r}, получено: {result!r}"


# =====================================================================
# 3. Тесты hub/sso_routes.py: sso_router
# =====================================================================


def _create_test_sso_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings(
            admin_user="owner",
            admin_password_hash=hash_password("SuperSecret123"),
            sso_secret="sso_test_secret_32bytes_value_here",
            sso_cookie_domain=".molokohub.site",
            sso_login_url="https://molokohub.site/sso/login",
            sso_allowed_suffix="molokohub.site",
            sso_ttl_hours=24,
            sso_cookie_name="molokohub_sso",
        )
    app = FastAPI()
    app.state.settings = settings
    app.include_router(sso_router)
    return app


def test_sso_login_get_unauthenticated() -> None:
    app = _create_test_sso_app()
    client = TestClient(app)

    res = client.get("/sso/login")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "Вход в MolokoHub" in res.text
    assert 'action="/sso/login"' in res.text
    assert 'name="username"' in res.text
    assert 'name="password"' in res.text


def test_sso_login_get_with_valid_cookie_redirects() -> None:
    settings = Settings(
        admin_user="owner",
        admin_password_hash=hash_password("SuperSecret123"),
        sso_secret="secret_key_sso",
        sso_cookie_domain=".molokohub.site",
        sso_login_url="https://molokohub.site/sso/login",
        sso_allowed_suffix="molokohub.site",
        sso_cookie_name="molokohub_sso",
    )
    app = _create_test_sso_app(settings)
    client = TestClient(app)

    valid_cookie = make_cookie("owner", settings.sso_secret, ttl_seconds=3600)
    client.cookies.set("molokohub_sso", valid_cookie)

    # 1. Редирект на валидный next
    res = client.get(
        "/sso/login?next=https://pool.molokohub.site/tasks",
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "https://pool.molokohub.site/tasks"

    # 2. Невалидный next перенаправляет на дефолтный URL панели
    res_unsafe = client.get(
        "/sso/login?next=https://evil.com",
        follow_redirects=False,
    )
    assert res_unsafe.status_code == 303
    assert res_unsafe.headers["location"] == "https://pool.molokohub.site/"


def test_sso_login_post_success() -> None:
    settings = Settings(
        admin_user="owner",
        admin_password_hash=hash_password("SuperSecret123"),
        sso_secret="test_sso_secret_value",
        sso_cookie_domain=".molokohub.site",
        sso_login_url="https://molokohub.site/sso/login",
        sso_allowed_suffix="molokohub.site",
        sso_ttl_hours=48,
        sso_cookie_name="molokohub_sso",
    )
    app = _create_test_sso_app(settings)
    client = TestClient(app, base_url="https://molokohub.site")

    res = client.post(
        "/sso/login",
        data={
            "username": "owner",
            "password": "SuperSecret123",
            "next": "https://pool.molokohub.site/dashboard",
        },
        follow_redirects=False,
    )

    assert res.status_code == 303
    assert res.headers["location"] == "https://pool.molokohub.site/dashboard"

    # Проверка cookie в ответе
    set_cookie_header = res.headers.get("set-cookie", "")
    assert "molokohub_sso=" in set_cookie_header
    assert "httponly" in set_cookie_header.lower()
    assert "secure" in set_cookie_header.lower()
    assert "samesite=lax" in set_cookie_header.lower()
    assert "domain=.molokohub.site" in set_cookie_header.lower()

    # Извлечение и валидация значения cookie
    cookie_val = res.cookies.get("molokohub_sso")
    assert cookie_val is not None
    verified_user = verify_cookie(cookie_val, settings.sso_secret)
    assert verified_user == "owner"


def test_sso_login_post_failure_and_limiter() -> None:
    # Изолированный limiter для теста
    test_limiter = LoginLimiter(max_attempts=3, window_s=60.0)
    orig_limiter = set_login_limiter(test_limiter)

    try:
        app = _create_test_sso_app()
        client = TestClient(app)

        # 1. Неверный пароль -> 401 и форма с ошибкой
        res_wrong_pw = client.post(
            "/sso/login",
            data={"username": "owner", "password": "WrongPassword"},
        )
        assert res_wrong_pw.status_code == 401
        assert "Неверный логин или пароль" in res_wrong_pw.text

        # 2. Неверный логин -> 401
        res_wrong_user = client.post(
            "/sso/login",
            data={"username": "wrong_user", "password": "SuperSecret123"},
        )
        assert res_wrong_user.status_code == 401

        # 3. Третья неудачная попытка
        res_third = client.post(
            "/sso/login",
            data={"username": "owner", "password": "Bad"},
        )
        assert res_third.status_code == 401

        # 4. Четвертая попытка блокируется ограничителем rate limit
        res_blocked = client.post(
            "/sso/login",
            data={"username": "owner", "password": "SuperSecret123"},
        )
        assert res_blocked.status_code == 401
        assert "Превышено число попыток входа" in res_blocked.text
    finally:
        set_login_limiter(orig_limiter)


def test_sso_verify_endpoint_success() -> None:
    settings = Settings(
        sso_secret="verify_secret_123",
        sso_cookie_name="molokohub_sso",
    )
    app = _create_test_sso_app(settings)
    client = TestClient(app)

    valid_cookie = make_cookie("owner", settings.sso_secret, ttl_seconds=3600)
    client.cookies.set("molokohub_sso", valid_cookie)

    res = client.get("/sso/verify")
    assert res.status_code == 200
    assert res.headers["x-sso-user"] == "owner"
    assert res.text == "OK"


def test_sso_verify_endpoint_unauthorized_redirect() -> None:
    settings = Settings(
        sso_secret="verify_secret_123",
        sso_login_url="https://molokohub.site/sso/login",
        sso_cookie_name="molokohub_sso",
    )
    app = _create_test_sso_app(settings)
    client = TestClient(app)

    # Запрос от Caddy forward_auth без cookie
    headers = {
        "x-forwarded-host": "pool.molokohub.site",
        "x-forwarded-uri": "/task/123?filter=active",
    }
    res = client.get("/sso/verify", headers=headers, follow_redirects=False)

    assert res.status_code == 302
    expected_next = urllib.parse.quote("https://pool.molokohub.site/task/123?filter=active", safe="")
    expected_location = f"https://molokohub.site/sso/login?next={expected_next}"
    assert res.headers["location"] == expected_location


def test_sso_verify_endpoint_503_when_secret_empty() -> None:
    settings = Settings(
        sso_secret="",
        sso_login_url="https://molokohub.site/sso/login",
    )
    app = _create_test_sso_app(settings)
    client = TestClient(app)

    res = client.get("/sso/verify")
    assert res.status_code == 503
    assert "SSO не настроен" in res.text


def test_sso_logout() -> None:
    settings = Settings(
        sso_secret="test_secret",
        sso_cookie_domain=".molokohub.site",
        sso_cookie_name="molokohub_sso",
    )
    app = _create_test_sso_app(settings)
    client = TestClient(app)

    # 1. GET /sso/logout
    res_get = client.get("/sso/logout", follow_redirects=False)
    assert res_get.status_code == 303
    assert res_get.headers["location"] == "/sso/login"
    set_cookie_get = res_get.headers.get("set-cookie", "")
    assert 'molokohub_sso=""' in set_cookie_get or 'molokohub_sso=;' in set_cookie_get
    assert "Max-Age=0" in set_cookie_get

    # 2. POST /sso/logout
    res_post = client.post("/sso/logout", follow_redirects=False)
    assert res_post.status_code == 303
    assert res_post.headers["location"] == "/sso/login"
    set_cookie_post = res_post.headers.get("set-cookie", "")
    assert "Max-Age=0" in set_cookie_post


# =====================================================================
# 4. Тесты подключения sso_router в hub/main.py
# =====================================================================


def test_main_app_sso_router_mounted_only_when_secret_not_empty() -> None:
    # 1. Без секрета SSO роутер не подключается
    settings_no_sso = Settings(db_path=":memory:", devices_file="", sso_secret="")
    app_no_sso = create_app(settings_no_sso)
    client_no_sso = TestClient(app_no_sso)

    res_no_sso = client_no_sso.get("/sso/login")
    assert res_no_sso.status_code == 404

    # 2. С непустым секретом SSO роутер подключается
    settings_with_sso = Settings(
        db_path=":memory:",
        devices_file="",
        sso_secret="activated_secret",
        admin_user="owner",
        admin_password_hash=hash_password("TestPass123"),
    )
    app_with_sso = create_app(settings_with_sso)
    client_with_sso = TestClient(app_with_sso)

    res_with_sso = client_with_sso.get("/sso/login")
    assert res_with_sso.status_code == 200
    assert "Вход в MolokoHub" in res_with_sso.text
