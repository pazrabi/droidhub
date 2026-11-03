"""Ограничитель попыток входа и определение IP клиента."""

from __future__ import annotations

import logging
import time

from fastapi import Request


logger = logging.getLogger(__name__)


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
