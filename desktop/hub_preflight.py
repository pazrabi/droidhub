"""Автономная проверка подключения к Hub для DroidHub Desktop.

Контракт:
    run_hub_preflight(*, load_settings, read_token, list_devices, output, errors) -> int

Коды возврата:
    0 — успех, Hub доступен и авторизован.
    2 — Hub отключён в настройках (startup disabled).
    3 — ошибка чтения настроек или токена из Keychain.
    4 — Hub недоступен или авторизация отклонена.

Принципы безопасности:
    - Токен и любые секреты никогда не выводятся, не логируются и не передаются
      в текст исключений, которые доходят до пользователя.
    - Текст исключений никогда не показывается пользователю.
    - В поток errors выводится только безопасная короткая причина на русском языке.
    - Не использует stdin/input.
    - Не содержит реализации Keychain или сетевых вызовов.
"""

from __future__ import annotations

from collections.abc import Callable, Sized
from typing import IO, Any


def run_hub_preflight(
    *,
    load_settings: Callable[[], tuple[str, bool]],
    read_token: Callable[[str], Any],
    list_devices: Callable[[str, Any], Sized],
    output: IO[str],
    errors: IO[str],
) -> int:
    """Выполняет предстартовую проверку подключения к Hub.

    Args:
        load_settings: Возвращает (hub_url, enabled). При ошибке бросает исключение.
        read_token: Принимает resolved hub_url, возвращает токен.
                    При ошибке (Keychain недоступен и т.п.) бросает исключение.
        list_devices: Принимает (hub_url, token), возвращает Sized (список устройств).
                      Бросает исключение при сетевой ошибке или отказе авторизации.
        output: Поток для вывода сообщения об успехе (stdout).
        errors: Поток для безопасных коротких сообщений об ошибках (stderr).

    Returns:
        Код завершения: 0, 2, 3 или 4.
    """
    # Шаг 1: загрузить настройки
    try:
        hub_url, enabled = load_settings()
    except Exception:
        errors.write("Hub: ошибка загрузки настроек.\n")
        return 3

    # Шаг 2: проверить, включён ли Hub при запуске
    if not enabled:
        return 2

    # Шаг 3: получить токен из Keychain (или аналогичного хранилища)
    try:
        token = read_token(hub_url)
    except Exception:
        errors.write("Hub: не удалось получить токен авторизации.\n")
        return 3

    # Шаг 4: запросить список устройств (проверка доступности Hub и авторизации)
    try:
        devices = list_devices(hub_url, token)
    except Exception:
        errors.write("Hub: сервер недоступен или авторизация отклонена.\n")
        return 4

    # Шаг 5: успех
    count = len(devices)
    output.write(f"Hub подключён. Устройств: {count}.\n")
    return 0
