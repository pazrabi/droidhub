"""Безопасный исполнитель операций настройки системного HTTP-прокси Android по USB.

Модуль предоставляет изолированную реализацию действий read/apply/clear для глобального
HTTP-прокси (Settings.Global.HTTP_PROXY) локальных аппаратных USB Android-устройств.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import re
import subprocess
from typing import Any

from desktop.adb_client import AdbClient, DeviceInfo
from desktop.link_service import (
    IncompatibleDeviceError,
    check_usb_device_eligibility,
)
from desktop.process_flags import NO_CONSOLE

# Максимальный допустимый размер вывода команды в байтах (16 КиБ)
MAX_PROXY_OUTPUT_BYTES: int = 16 * 1024

# Строго фиксированный таймаут выполнения команды (5 секунд)
PROXY_COMMAND_TIMEOUT: float = 5.0

# Поддерживаемые действия
SUPPORTED_ACTIONS: frozenset[str] = frozenset({"read", "apply", "clear"})


@dataclass(frozen=True)
class ProxyResult:
    """Результат выполнения операции с системным HTTP-прокси на устройстве.

    Атрибуты:
        serial: Серийный номер устройства.
        ok: Признак успешного выполнения операции.
        message: Поясняющее сообщение на русском языке.
    """

    serial: str
    ok: bool
    message: str


def validate_proxy_endpoint(endpoint: Any) -> tuple[bool, str]:
    """Строго валидирует адрес HTTP-прокси (host:port).

    Требования:
    - Строго ASCII DNS-имя хоста или IPv4.
    - Порт в диапазоне 1..65535 без знака и ведущих нулей.
    - Запрещены IPv6, авторизация, схемы протоколов, спецсимволы shell и пробелы.

    Возвращает:
        (True, "") если адрес валиден, иначе (False, "причина ошибки").
    """
    if not isinstance(endpoint, str):
        return False, "Адрес прокси-сервера должен быть строкой"

    if not endpoint:
        return False, "Адрес прокси-сервера не может быть пустым"

    if not endpoint.isascii():
        return False, "Адрес прокси-сервера должен содержать только ASCII-символы"

    if any(c.isspace() for c in endpoint):
        return False, "Адрес прокси-сервера не должен содержать пробельные символы"

    if "@" in endpoint:
        return False, "Авторизация в адресе прокси не поддерживается"

    if "://" in endpoint:
        return False, "Схема протокола в адресе прокси не поддерживается"

    if endpoint.count(":") != 1:
        if endpoint.count(":") > 1:
            return False, "IPv6 и множественные двоеточия запрещены"
        return False, "Отсутствует порт в адресе прокси (ожидается host:port)"

    host, port_str = endpoint.split(":", 1)

    if not host:
        return False, "Имя хоста не может быть пустым"

    if not port_str:
        return False, "Порт не может быть пустым"

    if not port_str.isdigit():
        return False, "Порт должен содержать только цифры"

    if len(port_str) > 1 and port_str.startswith("0"):
        return False, "Порт не должен содержать ведущих нулей"

    port = int(port_str)
    if not (1 <= port <= 65535):
        return False, f"Порт {port} выходит за пределы допустимого диапазона 1..65535"

    # Проверка допустимых символов в имени хоста: только буквы, цифры, дефис и точка
    if not re.fullmatch(r"[a-zA-Z0-9.-]+", host):
        return False, "Имя хоста содержит недопустимые символы или спецсимволы shell"

    # Проверка на полностью цифровые сегменты хоста (IPv4)
    parts = host.split(".")
    if all(p.isdigit() for p in parts if p):
        if len(parts) != 4:
            return False, "Некорректный формат IPv4-адреса (требуется ровно 4 октета)"
        for p in parts:
            if not p:
                return False, "Пустой октет в IPv4-адресе"
            if len(p) > 1 and p.startswith("0"):
                return False, "Октет IPv4 не должен содержать ведущих нулей"
            val = int(p)
            if not (0 <= val <= 255):
                return False, f"Октет IPv4 {val} выходит за пределы диапазона 0..255"
        return True, ""

    # Валидация DNS-имени хоста (RFC 1123)
    if len(host) > 253:
        return False, "Длина имени хоста не должна превышать 253 символа"

    if host.startswith(".") or host.endswith("."):
        return False, "Имя хоста не должно начинаться или заканчиваться точкой"

    labels = host.split(".")
    for label in labels:
        if not (1 <= len(label) <= 63):
            return False, "Длина метки DNS-имени должна быть от 1 до 63 символов"
        if label.startswith("-") or label.endswith("-"):
            return False, "Метка DNS-имени не может начинаться или заканчиваться дефисом"
        if not re.fullmatch(r"[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?", label):
            return False, f"Недопустимый формат метки DNS-имени '{label}'"

    if len(labels) > 1 and labels[-1].isdigit():
        return False, "Домен верхнего уровня (TLD) не может состоять только из цифр"

    return True, ""


def _run_adb_cmd(
    adb: AdbClient,
    transport_id: str,
    args: list[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
    timeout: float = PROXY_COMMAND_TIMEOUT,
) -> tuple[bool, str]:
    """Выполняет фиксированную команду ADB списком аргументов без shell.

    Гарантии безопасности:
    - shell=False, аргументы передаются строго списком (argv).
    - Флаг NO_CONSOLE.
    - Строгое ограничение по таймауту.
    - Ограничение размера вывода (16 КиБ).
    - Никогда не раскрывает stderr и пути файловой системы.
    """
    adb_path = getattr(adb, "adb_path", "adb")
    if not isinstance(adb_path, str) or not adb_path.strip():
        adb_path = "adb"

    cmd = [adb_path, "-t", transport_id, *args]

    try:
        if runner is not None:
            proc = runner(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                shell=False,
                check=False,
                **NO_CONSOLE,
            )
        else:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                shell=False,
                check=False,
                **NO_CONSOLE,
            )
    except subprocess.TimeoutExpired:
        return False, "Превышено время ожидания ответа от устройства"
    except FileNotFoundError:
        return False, "Исполняемый файл ADB не найден"
    except PermissionError:
        return False, "Отказано в доступе при запуске команды ADB"
    except OSError:
        return False, "Ошибка операционной системы при запуске ADB"
    except Exception:
        return False, "Сбой при выполнении команды ADB"

    if proc.returncode != 0:
        return False, f"Команда ADB завершилась с ошибкой (код {proc.returncode})"

    raw_stdout = proc.stdout
    if isinstance(raw_stdout, bytes):
        if len(raw_stdout) > MAX_PROXY_OUTPUT_BYTES:
            return False, "Размер вывода команды превышает допустимый лимит 16 КиБ"
        try:
            output = raw_stdout.decode("utf-8")
        except UnicodeDecodeError:
            return False, "Вывод команды содержит некорректный UTF-8"
    elif isinstance(raw_stdout, str):
        if len(raw_stdout.encode("utf-8")) > MAX_PROXY_OUTPUT_BYTES:
            return False, "Размер вывода команды превышает допустимый лимит 16 КиБ"
        output = raw_stdout
    else:
        output = ""

    return True, output.strip()


def _find_and_validate_device(
    devices: Sequence[DeviceInfo],
    serial: str,
) -> tuple[bool, str, str]:
    """Ищет устройство по serial и проверяет его валидность и физический USB.

    Возвращает:
        (True, "", transport_id) при успехе, иначе (False, "причина ошибки", "").
    """
    matches = [d for d in devices if getattr(d, "serial", None) == serial]
    if len(matches) == 0:
        return False, "Устройство не найдено среди подключенных к ADB", ""
    if len(matches) > 1:
        return False, "Обнаружены дубликаты устройства в ADB", ""

    device = matches[0]
    if not isinstance(device, DeviceInfo):
        return False, "Некорректный формат данных об устройстве", ""

    try:
        check_usb_device_eligibility(device)
    except IncompatibleDeviceError as exc:
        return False, str(exc), ""
    except Exception:
        return False, "Устройство не готово к работе или не является аппаратным USB", ""

    raw_tid = getattr(device, "transport_id", None)
    if raw_tid is None or isinstance(raw_tid, bool):
        return False, "Отсутствует transport_id для безопасной адресации", ""

    tid_str = str(raw_tid).strip()
    if not tid_str.isdigit() or int(tid_str) <= 0:
        return (
            False,
            "Отсутствует корректный положительный transport_id для безопасной адресации",
            "",
        )

    return True, "", tid_str


def _handle_single_device(
    adb: AdbClient,
    serial: str,
    action: str,
    endpoint: str | None,
    initial_transport_id: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
) -> ProxyResult:
    """Выполняет операцию с системным прокси для одного валидного USB-устройства."""
    # 1. Действие: read (чтение настроек)
    if action == "read":
        # Читаем PAC
        ok_pac, pac_val = _run_adb_cmd(
            adb,
            initial_transport_id,
            ["shell", "settings", "get", "global", "global_http_proxy_pac"],
            runner=runner,
        )
        if not ok_pac:
            return ProxyResult(serial=serial, ok=False, message=pac_val)

        # Читаем http_proxy
        ok_proxy, proxy_val = _run_adb_cmd(
            adb,
            initial_transport_id,
            ["shell", "settings", "get", "global", "http_proxy"],
            runner=runner,
        )
        if not ok_proxy:
            return ProxyResult(serial=serial, ok=False, message=proxy_val)

        has_pac = bool(pac_val and pac_val != "null")
        pac_suffix = f" (активен PAC: {pac_val})" if has_pac else ""

        if proxy_val in (":0", "null", ""):
            msg = f"Системный HTTP-прокси отключен ({proxy_val or 'null'}){pac_suffix}"
        else:
            msg = f"Текущая настройка http_proxy: {proxy_val}{pac_suffix}"

        return ProxyResult(serial=serial, ok=True, message=msg)

    # Для действий записи (apply, clear):
    # Шаг 1: Проверка PAC
    ok_pac, pac_val = _run_adb_cmd(
        adb,
        initial_transport_id,
        ["shell", "settings", "get", "global", "global_http_proxy_pac"],
        runner=runner,
    )
    if not ok_pac:
        return ProxyResult(serial=serial, ok=False, message=pac_val)

    if pac_val and pac_val != "null":
        verb = "установка" if action == "apply" else "сброс"
        return ProxyResult(
            serial=serial,
            ok=False,
            message=f"Обнаружен активный PAC-скрипт ({pac_val}), {verb} http_proxy отклонен(а) для защиты конфигурации",
        )

    # Шаг 2: Повторная проверка устройства перед записью (Recheck per write)
    try:
        fresh_devices = adb.list_devices()
    except Exception:
        return ProxyResult(
            serial=serial,
            ok=False,
            message="Не удалось обновить список устройств ADB перед записью",
        )

    ok_recheck, err_recheck, write_transport_id = _find_and_validate_device(
        fresh_devices, serial
    )
    if not ok_recheck:
        return ProxyResult(
            serial=serial,
            ok=False,
            message=f"Устройство недоступно перед записью: {err_recheck}",
        )

    # Шаг 3: Выполнение записи
    if action == "apply":
        target_val = endpoint  # гарантированно валидная непустая строка
        assert target_val is not None
        ok_put, put_err = _run_adb_cmd(
            adb,
            write_transport_id,
            ["shell", "settings", "put", "global", "http_proxy", target_val],
            runner=runner,
        )
        if not ok_put:
            return ProxyResult(serial=serial, ok=False, message=put_err)

        # Шаг 4: Чтение обратно для верификации
        ok_verify, read_back = _run_adb_cmd(
            adb,
            write_transport_id,
            ["shell", "settings", "get", "global", "http_proxy"],
            runner=runner,
        )
        if not ok_verify:
            return ProxyResult(serial=serial, ok=False, message=read_back)

        if read_back != target_val:
            return ProxyResult(
                serial=serial,
                ok=False,
                message=(
                    f"Несоответствие при проверке настройки: "
                    f"ожидалось '{target_val}', прочитано '{read_back}'"
                ),
            )

        return ProxyResult(
            serial=serial,
            ok=True,
            message=f"Системный HTTP-прокси установлен в настройках: {target_val}",
        )

    elif action == "clear":
        # Сброс через :0 (не delete)
        ok_put, put_err = _run_adb_cmd(
            adb,
            write_transport_id,
            ["shell", "settings", "put", "global", "http_proxy", ":0"],
            runner=runner,
        )
        if not ok_put:
            return ProxyResult(serial=serial, ok=False, message=put_err)

        # Шаг 4: Чтение обратно для верификации
        ok_verify, read_back = _run_adb_cmd(
            adb,
            write_transport_id,
            ["shell", "settings", "get", "global", "http_proxy"],
            runner=runner,
        )
        if not ok_verify:
            return ProxyResult(serial=serial, ok=False, message=read_back)

        if read_back not in (":0", "null", ""):
            return ProxyResult(
                serial=serial,
                ok=False,
                message=(
                    f"Несоответствие при проверке сброса: "
                    f"ожидалось ':0', прочитано '{read_back}'"
                ),
            )

        return ProxyResult(
            serial=serial,
            ok=True,
            message="Системный HTTP-прокси сброшен в настройках (:0)",
        )

    return ProxyResult(
        serial=serial, ok=False, message=f"Неподдерживаемое действие: {action}"
    )


def run_proxy_action(
    adb: AdbClient,
    serials: Sequence[str],
    action: str,
    endpoint: str | None = None,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
) -> list[ProxyResult]:
    """Выполняет операцию с системным HTTP-прокси для пакета USB Android-устройств.

    Параметры:
        adb: Клиент ADB (экземпляр AdbClient).
        serials: Последовательность серийных номеров устройств.
        action: Действие ('read', 'apply' или 'clear').
        endpoint: Адрес прокси 'host:port' (обязателен для действия 'apply').
        runner: Опциональная функция запуска подпроцесса для изолированного тестирования.

    Возвращает:
        Список объектов ProxyResult в том же порядке, что и serials (дедуплицированный).
    """
    # Дедупликация serials с сохранением исходного порядка
    unique_serials: list[str] = []
    seen: set[str] = set()
    if serials:
        for s in serials:
            if s not in seen:
                seen.add(s)
                unique_serials.append(s)

    if not unique_serials:
        return []

    # Проверка действия
    action_norm = action.strip().lower() if isinstance(action, str) else ""
    if action_norm not in SUPPORTED_ACTIONS:
        return [
            ProxyResult(
                serial=str(s),
                ok=False,
                message=f"Неподдерживаемое действие: '{action}'. Поддерживаются только read, apply, clear",
            )
            for s in unique_serials
        ]

    # Проверка endpoint перед вызовом ADB (Fail closed)
    if action_norm == "apply":
        if endpoint is None:
            return [
                ProxyResult(
                    serial=str(s),
                    ok=False,
                    message="Для действия 'apply' необходимо указать адрес прокси-сервера (хост:порт)",
                )
                for s in unique_serials
            ]
        is_valid, err_msg = validate_proxy_endpoint(endpoint)
        if not is_valid:
            return [
                ProxyResult(
                    serial=str(s),
                    ok=False,
                    message=f"Некорректный адрес прокси-сервера: {err_msg}",
                )
                for s in unique_serials
            ]
    else:
        # Для read и clear: если endpoint передан, проверяем его валидность
        if endpoint is not None:
            is_valid, err_msg = validate_proxy_endpoint(endpoint)
            if not is_valid:
                return [
                    ProxyResult(
                        serial=str(s),
                        ok=False,
                        message=f"Некорректный адрес прокси-сервера: {err_msg}",
                    )
                    for s in unique_serials
                ]

    # Проверка клиента ADB
    if adb is None:
        return [
            ProxyResult(serial=str(s), ok=False, message="Не указан клиент ADB")
            for s in unique_serials
        ]

    # Опрос списка устройств ADB перед началом батча (Re-list devices before every batch)
    try:
        batch_devices = adb.list_devices()
    except Exception:
        return [
            ProxyResult(
                serial=str(s),
                ok=False,
                message="Не удалось получить список устройств ADB",
            )
            for s in unique_serials
        ]

    if not isinstance(batch_devices, Sequence) or isinstance(
        batch_devices, (str, bytes)
    ):
        return [
            ProxyResult(
                serial=str(s),
                ok=False,
                message="Не удалось получить список устройств ADB",
            )
            for s in unique_serials
        ]

    results: list[ProxyResult] = []

    # Обработка каждого устройства независимо (Continue independent phones on partial failure)
    for serial in unique_serials:
        if not isinstance(serial, str) or not serial.strip():
            results.append(
                ProxyResult(
                    serial=str(serial),
                    ok=False,
                    message="Некорректный серийный номер устройства",
                )
            )
            continue

        ok_dev, dev_err, transport_id = _find_and_validate_device(
            batch_devices, serial
        )
        if not ok_dev:
            results.append(ProxyResult(serial=serial, ok=False, message=dev_err))
            continue

        res = _handle_single_device(
            adb=adb,
            serial=serial,
            action=action_norm,
            endpoint=endpoint,
            initial_transport_id=transport_id,
            runner=runner,
        )
        results.append(res)

    return results


__all__ = [
    "MAX_PROXY_OUTPUT_BYTES",
    "PROXY_COMMAND_TIMEOUT",
    "ProxyResult",
    "run_proxy_action",
    "validate_proxy_endpoint",
]
