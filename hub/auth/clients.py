"""Клиенты по токенам (HUB_CLIENT_TOKENS) и разбор токена."""

from __future__ import annotations

import hmac
import logging
import re
from dataclasses import dataclass


from hub.config import Settings, get_settings

logger = logging.getLogger(__name__)


_CLIENT_NAME_RE = re.compile(r"^[a-z0-9_-]+$")
_CLIENT_TOKEN_RE = re.compile(r"^[A-Za-z0-9_=-]+$")
_CLIENT_FLAG_RE = re.compile(r"^[a-z0-9_-]+$")


@dataclass(frozen=True)
class Client:
    name: str
    rate_limit_per_min: int | None = None
    allow_power: bool = True

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
