from __future__ import annotations

import hashlib
import hmac
import re
import time
import urllib.parse

_USER_RE = re.compile(r"^[A-Za-z0-9_.\-@]+$")


def make_cookie(user: str, secret: str, ttl_seconds: int, now: float | None = None) -> str:
    """
    Создает подписанный cookie единого входа (SSO).
    Формат: <user>|<expires_epoch>|<hmac_sha256_hex>.
    user разрешен только из символов [A-Za-z0-9_.@-].
    """
    if not isinstance(user, str) or not _USER_RE.fullmatch(user):
        raise ValueError("Недопустимое имя пользователя для SSO (разрешены только [A-Za-z0-9_.@-])")
    if not secret or not isinstance(secret, str):
        raise ValueError("Секрет SSO не может быть пустым")
    if ttl_seconds <= 0:
        raise ValueError("TTL cookie должен быть положительным числом секунд")

    if now is None:
        now = time.time()

    expires_epoch = int(now + ttl_seconds)
    payload = f"{user}|{expires_epoch}"
    sig = hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return f"{payload}|{sig}"


def verify_cookie(value: str, secret: str, now: float | None = None) -> str | None:
    """
    Проверяет SSO cookie и возвращает имя пользователя.
    Возвращает user, если подпись верна (constant-time сравнение),
    срок действия не истёк и secret не пуст; иначе None.
    Никаких исключений наружу не выбрасывается.
    """
    try:
        if not secret or not isinstance(secret, str):
            return None
        if not value or not isinstance(value, str):
            return None

        parts = value.split("|")
        if len(parts) != 3:
            return None

        user, expires_str, sig = parts
        if not user or not _USER_RE.fullmatch(user):
            return None
        if not sig or len(sig) != 64:
            return None

        try:
            expires = int(expires_str)
        except (ValueError, TypeError):
            return None

        if now is None:
            now = time.time()

        if expires < now:
            return None

        payload = f"{user}|{expires_str}"
        expected_sig = hmac.new(
            secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(sig, expected_sig):
            return None

        return user
    except Exception:
        return None


def safe_next(url: str, allowed_suffix: str) -> str | None:
    """
    Проверяет безопасность URL для перенаправления (защита от open redirect).
    Принимает только https:// URL, чей host равен allowed_suffix или
    оканчивается на '.' + allowed_suffix, без userinfo (@), без backslash (\\),
    длиной до 500 символов; иначе возвращает None.
    """
    if not url or not isinstance(url, str):
        return None
    if not allowed_suffix or not isinstance(allowed_suffix, str):
        return None
    if len(url) > 500:
        return None

    # Запрет недопустимых символов: @ (userinfo), \ (backslash), пробельные/управляющие
    if "@" in url or "\\" in url:
        return None
    if any(c in url for c in " \t\r\n\x00"):
        return None

    # Проверка декодированного URL (на случай обхода через urlencoding %40 или %5c)
    try:
        unquoted = urllib.parse.unquote(url)
        if "@" in unquoted or "\\" in unquoted:
            return None
        if any(c in unquoted for c in " \t\r\n\x00"):
            return None
    except Exception:
        return None

    # Схема должна быть строго https://
    if not url.startswith("https://") and not url.startswith("HTTPS://"):
        return None

    try:
        parsed = urllib.parse.urlsplit(url)
        # Валидация корректности порта при наличии
        _ = parsed.port
    except Exception:
        return None

    if parsed.scheme.lower() != "https":
        return None

    if not parsed.netloc:
        return None

    # Извлекаем hostname (без порта, приведенный к нижнему регистру)
    host = parsed.hostname
    if not host:
        return None

    host = host.lower()
    suffix = allowed_suffix.strip().lower().lstrip(".")
    if not suffix:
        return None

    # Запрещаем хосты с точками по краям (.molokohub.site)
    if host.startswith(".") or host.endswith("."):
        return None

    # Хост должен быть в точности suffix ИЛИ поддоменом (оканчиваться на '.' + suffix)
    if host != suffix and not host.endswith("." + suffix):
        return None

    return url


__all__ = ["make_cookie", "verify_cookie", "safe_next"]
