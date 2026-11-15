"""Изолированные юнит-тесты сервиса системного HTTP-прокси Android по USB."""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import MagicMock

import pytest

from desktop.adb_client import AdbClient, DeviceInfo
from desktop.usb_proxy import (
    MAX_PROXY_OUTPUT_BYTES,
    ProxyResult,
    run_proxy_action,
    validate_proxy_endpoint,
)


def _make_device_info(
    serial: str,
    *,
    transport_id: str | None = "1",
    usb_info: str | None = "usb:1-1",
    state: str = "device",
    is_ready: bool = True,
    is_network: bool = False,
) -> DeviceInfo:
    """Создает тестовый объект DeviceInfo с заданными параметрами."""
    return DeviceInfo(
        serial=serial,
        state=state,
        state_display="Готов к работе" if is_ready else "Не готов",
        is_network=is_network,
        connection_type="USB" if not is_network else "Сетевое (ADB TCP/IP)",
        is_ready=is_ready,
        transport_id=transport_id,
        usb_info=usb_info,
    )


class FakeRunner:
    """Фейковый исполнитель команд подпроцесса ADB для изолированного тестирования."""

    def __init__(self, responses: dict[tuple[str, ...], subprocess.CompletedProcess[Any]] | None = None) -> None:
        self.responses: dict[tuple[str, ...], subprocess.CompletedProcess[Any]] = responses or {}
        self.recorded_calls: list[list[str]] = []

    def set_response(self, cmd_pattern: tuple[str, ...], proc: subprocess.CompletedProcess[Any]) -> None:
        self.responses[cmd_pattern] = proc

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        self.recorded_calls.append(list(cmd))
        cmd_tuple = tuple(cmd)

        if cmd_tuple in self.responses:
            return self.responses[cmd_tuple]

        # Поиск по суффиксу команды (без пути adb и флага -t <tid>)
        if len(cmd) >= 4 and cmd[1] == "-t" and cmd[3] == "shell":
            shell_sub = tuple(cmd[3:])
            if shell_sub in self.responses:
                return self.responses[shell_sub]

        # Дефолтный ответ - пустая успешная команда
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"", stderr=b"")


# ---------------------------------------------------------------------------
# Тесты строгой валидации endpoint
# ---------------------------------------------------------------------------


def test_validate_proxy_endpoint_valid():
    """Проверка корректных адресов: IPv4 и DNS hostnames с портами 1..65535."""
    valid_cases = [
        "127.0.0.1:8080",
        "10.0.2.2:1080",
        "192.168.1.1:1",
        "255.255.255.255:65535",
        "localhost:8080",
        "proxy.example.com:3128",
        "my-proxy-server.corp:8888",
        "p1.sub.internal:80",
    ]
    for case in valid_cases:
        ok, err = validate_proxy_endpoint(case)
        assert ok is True, f"Ожидался успешный результат для {case}, получена ошибка: {err}"
        assert err == ""


def test_validate_proxy_endpoint_invalid_types_and_empty():
    """Проверка отклонения не-строковых и пустых значений."""
    assert validate_proxy_endpoint(None)[0] is False
    assert validate_proxy_endpoint(123)[0] is False
    assert validate_proxy_endpoint("")[0] is False


def test_validate_proxy_endpoint_whitespace_and_non_ascii():
    """Проверка запрета пробельных символов и не-ASCII."""
    invalid_cases = [
        " 127.0.0.1:8080",
        "127.0.0.1:8080 ",
        "127.0.0.1 :8080",
        "127.0.0.1:8080\n",
        "127.0.0.1:8080\t",
        "прокси.рф:8080",
        "127.0.0.1:8080\u200b",
    ]
    for case in invalid_cases:
        ok, err = validate_proxy_endpoint(case)
        assert ok is False, f"Ожидалась ошибка для '{case}'"
        assert len(err) > 0


def test_validate_proxy_endpoint_auth_and_scheme():
    """Проверка строгого запрета схем (http://) и авторизации (@)."""
    invalid_cases = [
        "http://127.0.0.1:8080",
        "https://localhost:8080",
        "socks5://127.0.0.1:1080",
        "user:pass@127.0.0.1:8080",
        "@127.0.0.1:8080",
    ]
    for case in invalid_cases:
        ok, err = validate_proxy_endpoint(case)
        assert ok is False, f"Ожидалась ошибка для '{case}'"


def test_validate_proxy_endpoint_ipv6_and_colons():
    """Проверка строгого запрета IPv6 и некорректного числа двоеточий."""
    invalid_cases = [
        "[::1]:8080",
        "::1:8080",
        "fe80::1:8080",
        "2001:db8::1:8080",
        "127.0.0.1",
        "localhost",
        ":8080",
        "127.0.0.1:",
        "127.0.0.1:80:80",
    ]
    for case in invalid_cases:
        ok, err = validate_proxy_endpoint(case)
        assert ok is False, f"Ожидалась ошибка для '{case}'"


def test_validate_proxy_endpoint_ports():
    """Проверка диапазона портов 1..65535, запрета знаков и ведущих нулей."""
    invalid_cases = [
        "127.0.0.1:0",
        "127.0.0.1:65536",
        "127.0.0.1:99999",
        "127.0.0.1:-80",
        "127.0.0.1:+80",
        "127.0.0.1:080",
        "127.0.0.1:80a",
        "127.0.0.1:port",
    ]
    for case in invalid_cases:
        ok, err = validate_proxy_endpoint(case)
        assert ok is False, f"Ожидалась ошибка для '{case}'"


def test_validate_proxy_endpoint_shell_metacharacters():
    """Проверка запрета спецсимволов shell."""
    shell_injections = [
        "127.0.0.1:8080; rm -rf /",
        "127.0.0.1:8080 && echo 1",
        "127.0.0.1:8080 || id",
        "127.0.0.1:8080 | cat",
        "127.0.0.1:8080$PATH",
        "127.0.0.1:8080`whoami`",
        "127.0.0.1:8080>out",
        "127.0.0.1:8080<in",
        "127.0.0.1:8080\"test\"",
        "127.0.0.1:8080'test'",
        "127.0.0.1:8080(test)",
        "127.0.0.1:8080{test}",
    ]
    for case in shell_injections:
        ok, err = validate_proxy_endpoint(case)
        assert ok is False, f"Ожидалась ошибка инъекции для '{case}'"


def test_validate_proxy_endpoint_malformed_ipv4_and_hostnames():
    """Проверка некорректных форматов IPv4 и доменных имен."""
    invalid_cases = [
        "256.0.0.1:8080",
        "1.2.3:8080",
        "1.2.3.4.5:8080",
        "01.2.3.4:8080",
        "1..2.3.4:8080",
        "-proxy.com:8080",
        "proxy-.com:8080",
        ".proxy.com:8080",
        "proxy.com.:8080",
        "proxy..com:8080",
        "my_proxy:8080",
        "123.456:8080",
        ("a" * 254) + ":8080",
    ]
    for case in invalid_cases:
        ok, err = validate_proxy_endpoint(case)
        assert ok is False, f"Ожидалась ошибка для '{case}'"


# ---------------------------------------------------------------------------
# Тесты дедупликации serials и порядка
# ---------------------------------------------------------------------------


def test_run_proxy_action_deduplication_and_order():
    """Проверка дедупликации serials с сохранением исходного порядка."""
    fake_client = MagicMock(spec=AdbClient)
    dev1 = _make_device_info("dev1", transport_id="1")
    dev2 = _make_device_info("dev2", transport_id="2")
    dev3 = _make_device_info("dev3", transport_id="3")
    fake_client.list_devices.return_value = [dev1, dev2, dev3]

    runner = FakeRunner()
    # Ответы для read
    runner.set_response(
        ("shell", "settings", "get", "global", "global_http_proxy_pac"),
        subprocess.CompletedProcess(["adb"], 0, stdout=b"null\n", stderr=b""),
    )
    runner.set_response(
        ("shell", "settings", "get", "global", "http_proxy"),
        subprocess.CompletedProcess(["adb"], 0, stdout=b"10.0.0.1:8080\n", stderr=b""),
    )

    serials_input = ["dev2", "dev1", "dev2", "dev3", "dev1"]
    results = run_proxy_action(fake_client, serials_input, "read", runner=runner)

    assert len(results) == 3
    assert [r.serial for r in results] == ["dev2", "dev1", "dev3"]
    assert all(r.ok for r in results)


def test_run_proxy_action_empty_serials():
    """Проверка вызова с пустым списком serials."""
    fake_client = MagicMock(spec=AdbClient)
    results = run_proxy_action(fake_client, [], "read")
    assert results == []
    fake_client.list_devices.assert_not_called()


# ---------------------------------------------------------------------------
# Тесты Fail-Closed (до обращения к ADB)
# ---------------------------------------------------------------------------


def test_run_proxy_action_fail_closed_unknown_action():
    """Неизвестное действие отклоняется до обращения к ADB."""
    fake_client = MagicMock(spec=AdbClient)
    results = run_proxy_action(fake_client, ["dev1", "dev2"], "unknown_action")
    assert len(results) == 2
    assert not results[0].ok
    assert not results[1].ok
    assert "Неподдерживаемое действие" in results[0].message
    fake_client.list_devices.assert_not_called()


def test_run_proxy_action_fail_closed_missing_endpoint_for_apply():
    """Действие apply без endpoint отклоняется до обращения к ADB."""
    fake_client = MagicMock(spec=AdbClient)
    results = run_proxy_action(fake_client, ["dev1"], "apply", endpoint=None)
    assert len(results) == 1
    assert not results[0].ok
    assert "необходимо указать адрес" in results[0].message
    fake_client.list_devices.assert_not_called()


def test_run_proxy_action_fail_closed_malformed_endpoint():
    """Некорректный endpoint отклоняется до обращения к ADB."""
    fake_client = MagicMock(spec=AdbClient)
    malformed_endpoints = [
        "[::1]:8080",
        "http://127.0.0.1:8080",
        "127.0.0.1:0",
        "127.0.0.1:8080; rm -rf /",
        "user:pass@127.0.0.1:8080",
    ]
    for ep in malformed_endpoints:
        results = run_proxy_action(fake_client, ["dev1"], "apply", endpoint=ep)
        assert len(results) == 1
        assert not results[0].ok
        assert "Некорректный адрес прокси-сервера" in results[0].message
        fake_client.list_devices.assert_not_called()


def test_run_proxy_action_missing_adb_client():
    """Проверка передачи adb=None."""
    results = run_proxy_action(None, ["dev1"], "read")
    assert len(results) == 1
    assert not results[0].ok
    assert "Не указан клиент ADB" in results[0].message


# ---------------------------------------------------------------------------
# Тесты отбора устройств и адресации через transport_id
# ---------------------------------------------------------------------------


def test_run_proxy_action_rejects_network_device():
    """Сетевое устройство (ADB TCP/IP) отклоняется по правилам безопасности USB."""
    fake_client = MagicMock(spec=AdbClient)
    net_dev = _make_device_info("192.168.1.50:5555", is_network=True)
    fake_client.list_devices.return_value = [net_dev]

    results = run_proxy_action(fake_client, ["192.168.1.50:5555"], "read")
    assert len(results) == 1
    assert not results[0].ok
    assert "является сетевым" in results[0].message


def test_run_proxy_action_rejects_missing_usb_info():
    """Устройство без usb_info отклоняется."""
    fake_client = MagicMock(spec=AdbClient)
    dev = _make_device_info("dev1", usb_info=None)
    fake_client.list_devices.return_value = [dev]

    results = run_proxy_action(fake_client, ["dev1"], "read")
    assert len(results) == 1
    assert not results[0].ok
    assert "usb_info" in results[0].message


def test_run_proxy_action_rejects_unready_device():
    """Неавторизованное / не готовое устройство отклоняется."""
    fake_client = MagicMock(spec=AdbClient)
    dev = _make_device_info("dev1", state="unauthorized", is_ready=False)
    fake_client.list_devices.return_value = [dev]

    results = run_proxy_action(fake_client, ["dev1"], "read")
    assert len(results) == 1
    assert not results[0].ok
    assert "не готово к работе" in results[0].message


def test_run_proxy_action_rejects_non_positive_transport_id():
    """Устройство без положительного ADB transport_id отклоняется."""
    fake_client = MagicMock(spec=AdbClient)
    bad_tids = [None, "0", "-1", "abc", True]
    for tid in bad_tids:
        dev = _make_device_info("dev1", transport_id=tid)  # type: ignore[arg-type]
        fake_client.list_devices.return_value = [dev]
        results = run_proxy_action(fake_client, ["dev1"], "read")
        assert len(results) == 1
        assert not results[0].ok
        assert "transport_id" in results[0].message


def test_run_proxy_action_device_not_found_or_duplicate():
    """Проверка обработки отсутствия устройства и обнаружения дубликатов."""
    fake_client = MagicMock(spec=AdbClient)
    dev_a = _make_device_info("devA", transport_id="1")
    dev_dup1 = _make_device_info("devDup", transport_id="2")
    dev_dup2 = _make_device_info("devDup", transport_id="3")
    fake_client.list_devices.return_value = [dev_a, dev_dup1, dev_dup2]

    results = run_proxy_action(fake_client, ["devMissing", "devDup"], "read")
    assert len(results) == 2
    assert not results[0].ok
    assert "не найдено" in results[0].message
    assert not results[1].ok
    assert "дубликаты" in results[1].message


def test_run_proxy_action_pins_positive_transport_id():
    """Проверка обязательного использования флага -t <transport_id> для адресации."""
    fake_client = MagicMock(spec=AdbClient)
    dev = _make_device_info("dev1", transport_id="42")
    fake_client.list_devices.return_value = [dev]

    runner = FakeRunner()
    runner.set_response(
        ("shell", "settings", "get", "global", "global_http_proxy_pac"),
        subprocess.CompletedProcess(["adb"], 0, stdout=b"null\n", stderr=b""),
    )
    runner.set_response(
        ("shell", "settings", "get", "global", "http_proxy"),
        subprocess.CompletedProcess(["adb"], 0, stdout=b":0\n", stderr=b""),
    )

    results = run_proxy_action(fake_client, ["dev1"], "read", runner=runner)
    assert len(results) == 1
    assert results[0].ok

    # Проверяем аргументы каждого вызова
    for call in runner.recorded_calls:
        assert "-t" in call
        idx = call.index("-t")
        assert call[idx + 1] == "42"


# ---------------------------------------------------------------------------
# Тесты защиты PAC-скрипта
# ---------------------------------------------------------------------------


def test_run_proxy_action_protects_existing_pac_on_apply():
    """При наличии настроенного PAC-скрипта операция apply отклоняется."""
    fake_client = MagicMock(spec=AdbClient)
    dev = _make_device_info("dev1", transport_id="10")
    fake_client.list_devices.return_value = [dev]

    runner = FakeRunner()
    runner.set_response(
        ("shell", "settings", "get", "global", "global_http_proxy_pac"),
        subprocess.CompletedProcess(["adb"], 0, stdout=b"http://pac.corp/proxy.pac\n", stderr=b""),
    )

    results = run_proxy_action(
        fake_client, ["dev1"], "apply", endpoint="127.0.0.1:8080", runner=runner
    )
    assert len(results) == 1
    assert not results[0].ok
    assert "PAC-скрипт" in results[0].message
    assert "http://pac.corp/proxy.pac" in results[0].message
    # Убеждаемся, что settings put не вызывался
    for call in runner.recorded_calls:
        assert "put" not in call


def test_run_proxy_action_protects_existing_pac_on_clear():
    """При наличии настроенного PAC-скрипта операция clear отклоняется."""
    fake_client = MagicMock(spec=AdbClient)
    dev = _make_device_info("dev1", transport_id="10")
    fake_client.list_devices.return_value = [dev]

    runner = FakeRunner()
    runner.set_response(
        ("shell", "settings", "get", "global", "global_http_proxy_pac"),
        subprocess.CompletedProcess(["adb"], 0, stdout=b"http://pac.corp/proxy.pac\n", stderr=b""),
    )

    results = run_proxy_action(fake_client, ["dev1"], "clear", runner=runner)
    assert len(results) == 1
    assert not results[0].ok
    assert "PAC-скрипт" in results[0].message
    for call in runner.recorded_calls:
        assert "put" not in call


def test_run_proxy_action_read_reports_existing_pac_without_refusing():
    """Действие read сообщает об активном PAC-скрипте, не отклоняя чтение."""
    fake_client = MagicMock(spec=AdbClient)
    dev = _make_device_info("dev1", transport_id="10")
    fake_client.list_devices.return_value = [dev]

    runner = FakeRunner()
    runner.set_response(
        ("shell", "settings", "get", "global", "global_http_proxy_pac"),
        subprocess.CompletedProcess(["adb"], 0, stdout=b"http://pac.corp/proxy.pac\n", stderr=b""),
    )
    runner.set_response(
        ("shell", "settings", "get", "global", "http_proxy"),
        subprocess.CompletedProcess(["adb"], 0, stdout=b"null\n", stderr=b""),
    )

    results = run_proxy_action(fake_client, ["dev1"], "read", runner=runner)
    assert len(results) == 1
    assert results[0].ok
    assert "http://pac.corp/proxy.pac" in results[0].message


# ---------------------------------------------------------------------------
# Тесты apply: запись и обратная верификация
# ---------------------------------------------------------------------------


def test_run_proxy_action_apply_success():
    """Успешное применение прокси: проверка PAC -> запись -> чтение -> успех."""
    fake_client = MagicMock(spec=AdbClient)
    dev = _make_device_info("dev1", transport_id="5")
    fake_client.list_devices.return_value = [dev]

    endpoint = "10.0.0.1:8080"
    runner = FakeRunner()
    runner.set_response(
        ("shell", "settings", "get", "global", "global_http_proxy_pac"),
        subprocess.CompletedProcess(["adb"], 0, stdout=b"null\n", stderr=b""),
    )
    runner.set_response(
        ("shell", "settings", "put", "global", "http_proxy", endpoint),
        subprocess.CompletedProcess(["adb"], 0, stdout=b"", stderr=b""),
    )
    runner.set_response(
        ("shell", "settings", "get", "global", "http_proxy"),
        subprocess.CompletedProcess(["adb"], 0, stdout=f"{endpoint}\n".encode(), stderr=b""),
    )

    results = run_proxy_action(
        fake_client, ["dev1"], "apply", endpoint=endpoint, runner=runner
    )
    assert len(results) == 1
    assert results[0].ok
    assert endpoint in results[0].message
    assert "установлен в настройках" in results[0].message


def test_run_proxy_action_apply_verification_mismatch():
    """Если прочитанное значение не совпадает с записанным, операция завершается с ошибкой."""
    fake_client = MagicMock(spec=AdbClient)
    dev = _make_device_info("dev1", transport_id="5")
    fake_client.list_devices.return_value = [dev]

    endpoint = "10.0.0.1:8080"
    runner = FakeRunner()
    runner.set_response(
        ("shell", "settings", "get", "global", "global_http_proxy_pac"),
        subprocess.CompletedProcess(["adb"], 0, stdout=b"null\n", stderr=b""),
    )
    runner.set_response(
        ("shell", "settings", "put", "global", "http_proxy", endpoint),
        subprocess.CompletedProcess(["adb"], 0, stdout=b"", stderr=b""),
    )
    # Возвращаем старое или некорректное значение при проверке
    runner.set_response(
        ("shell", "settings", "get", "global", "http_proxy"),
        subprocess.CompletedProcess(["adb"], 0, stdout=b":0\n", stderr=b""),
    )

    results = run_proxy_action(
        fake_client, ["dev1"], "apply", endpoint=endpoint, runner=runner
    )
    assert len(results) == 1
    assert not results[0].ok
    assert "Несоответствие при проверке настройки" in results[0].message


# ---------------------------------------------------------------------------
# Тесты clear: сброс через :0 и верификация
# ---------------------------------------------------------------------------


def test_run_proxy_action_clear_success():
    """Успешный сброс прокси: запись :0 (не delete) -> чтение -> успех."""
    fake_client = MagicMock(spec=AdbClient)
    dev = _make_device_info("dev1", transport_id="7")
    fake_client.list_devices.return_value = [dev]

    runner = FakeRunner()
    runner.set_response(
        ("shell", "settings", "get", "global", "global_http_proxy_pac"),
        subprocess.CompletedProcess(["adb"], 0, stdout=b"null\n", stderr=b""),
    )
    runner.set_response(
        ("shell", "settings", "put", "global", "http_proxy", ":0"),
        subprocess.CompletedProcess(["adb"], 0, stdout=b"", stderr=b""),
    )
    runner.set_response(
        ("shell", "settings", "get", "global", "http_proxy"),
        subprocess.CompletedProcess(["adb"], 0, stdout=b":0\n", stderr=b""),
    )

    results = run_proxy_action(fake_client, ["dev1"], "clear", runner=runner)
    assert len(results) == 1
    assert results[0].ok
    assert ":0" in results[0].message

    # Убеждаемся, что не было команды 'settings delete'
    for call in runner.recorded_calls:
        assert "delete" not in call
    # Убеждаемся, что вызывался именно put ... :0
    put_calls = [c for c in runner.recorded_calls if "put" in c]
    assert len(put_calls) == 1
    assert put_calls[0][-1] == ":0"


def test_run_proxy_action_clear_verification_mismatch():
    """Если при сбросе прочитанное значение не сбросилось в :0/null, возвращается ошибка."""
    fake_client = MagicMock(spec=AdbClient)
    dev = _make_device_info("dev1", transport_id="7")
    fake_client.list_devices.return_value = [dev]

    runner = FakeRunner()
    runner.set_response(
        ("shell", "settings", "get", "global", "global_http_proxy_pac"),
        subprocess.CompletedProcess(["adb"], 0, stdout=b"null\n", stderr=b""),
    )
    runner.set_response(
        ("shell", "settings", "put", "global", "http_proxy", ":0"),
        subprocess.CompletedProcess(["adb"], 0, stdout=b"", stderr=b""),
    )
    runner.set_response(
        ("shell", "settings", "get", "global", "http_proxy"),
        subprocess.CompletedProcess(["adb"], 0, stdout=b"192.168.1.1:8080\n", stderr=b""),
    )

    results = run_proxy_action(fake_client, ["dev1"], "clear", runner=runner)
    assert len(results) == 1
    assert not results[0].ok
    assert "Несоответствие при проверке сброса" in results[0].message


# ---------------------------------------------------------------------------
# Тесты частичного отказа и независимости телефонов
# ---------------------------------------------------------------------------


def test_run_proxy_action_continue_on_partial_failure():
    """Сбой на одном телефоне не должен останавливать выполнение для остальных."""
    fake_client = MagicMock(spec=AdbClient)
    dev_bad = _make_device_info("devBad", state="unauthorized", is_ready=False)
    dev_pac = _make_device_info("devPac", transport_id="11")
    dev_ok = _make_device_info("devOk", transport_id="12")
    fake_client.list_devices.return_value = [dev_bad, dev_pac, dev_ok]

    runner = FakeRunner()
    # devPac имеет активный PAC
    runner.set_response(
        ("adb", "-t", "11", "shell", "settings", "get", "global", "global_http_proxy_pac"),
        subprocess.CompletedProcess(["adb"], 0, stdout=b"http://pac.local/proxy.pac\n", stderr=b""),
    )
    # devOk чист
    runner.set_response(
        ("adb", "-t", "12", "shell", "settings", "get", "global", "global_http_proxy_pac"),
        subprocess.CompletedProcess(["adb"], 0, stdout=b"null\n", stderr=b""),
    )
    runner.set_response(
        ("adb", "-t", "12", "shell", "settings", "put", "global", "http_proxy", "127.0.0.1:8080"),
        subprocess.CompletedProcess(["adb"], 0, stdout=b"", stderr=b""),
    )
    runner.set_response(
        ("adb", "-t", "12", "shell", "settings", "get", "global", "http_proxy"),
        subprocess.CompletedProcess(["adb"], 0, stdout=b"127.0.0.1:8080\n", stderr=b""),
    )

    results = run_proxy_action(
        fake_client,
        ["devBad", "devPac", "devOk"],
        "apply",
        endpoint="127.0.0.1:8080",
        runner=runner,
    )

    assert len(results) == 3
    # devBad упал на проверке готовности
    assert not results[0].ok
    assert "не готово к работе" in results[0].message

    # devPac упал из-за защиты PAC
    assert not results[1].ok
    assert "PAC-скрипт" in results[1].message

    # devOk успешно применил настройку
    assert results[2].ok
    assert "127.0.0.1:8080" in results[2].message


# ---------------------------------------------------------------------------
# Тесты безопасности: отсутствие stderr, лимит вывода, таймаут
# ---------------------------------------------------------------------------


def test_run_proxy_action_does_not_expose_stderr():
    """Сообщения об ошибках гарантированно не содержат stderr команды."""
    fake_client = MagicMock(spec=AdbClient)
    dev = _make_device_info("dev1", transport_id="3")
    fake_client.list_devices.return_value = [dev]

    runner = FakeRunner()
    sensitive_stderr = b"SECRET_PASSWORD_OR_PATH_IN_STDERR /var/lib/secret"
    runner.set_response(
        ("shell", "settings", "get", "global", "global_http_proxy_pac"),
        subprocess.CompletedProcess(
            ["adb"],
            returncode=1,
            stdout=b"",
            stderr=sensitive_stderr,
        ),
    )

    results = run_proxy_action(fake_client, ["dev1"], "read", runner=runner)
    assert len(results) == 1
    assert not results[0].ok
    assert "SECRET" not in results[0].message
    assert "secret" not in results[0].message
    assert "код 1" in results[0].message


def test_run_proxy_action_output_cap_exceeded():
    """Превышение лимита вывода 16 КиБ приводит к безопасной ошибке."""
    fake_client = MagicMock(spec=AdbClient)
    dev = _make_device_info("dev1", transport_id="3")
    fake_client.list_devices.return_value = [dev]

    runner = FakeRunner()
    huge_output = b"A" * (MAX_PROXY_OUTPUT_BYTES + 100)
    runner.set_response(
        ("shell", "settings", "get", "global", "global_http_proxy_pac"),
        subprocess.CompletedProcess(["adb"], returncode=0, stdout=huge_output, stderr=b""),
    )

    results = run_proxy_action(fake_client, ["dev1"], "read", runner=runner)
    assert len(results) == 1
    assert not results[0].ok
    assert "превышает допустимый лимит" in results[0].message


def test_run_proxy_action_subprocess_timeout():
    """Обработка таймаута выполнения команды ADB."""
    fake_client = MagicMock(spec=AdbClient)
    dev = _make_device_info("dev1", transport_id="3")
    fake_client.list_devices.return_value = [dev]

    def timeout_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        raise subprocess.TimeoutExpired(cmd, timeout=5.0)

    results = run_proxy_action(fake_client, ["dev1"], "read", runner=timeout_runner)
    assert len(results) == 1
    assert not results[0].ok
    assert "Превышено время ожидания" in results[0].message


# ---------------------------------------------------------------------------
# Тест Recheck per write: отключение устройства перед записью
# ---------------------------------------------------------------------------


def test_run_proxy_action_recheck_fails_if_device_disconnects_before_write():
    """Если устройство исчезло из списка ADB перед записью, операция прерывается."""
    fake_client = MagicMock(spec=AdbClient)
    dev = _make_device_info("dev1", transport_id="3")

    # Первый вызов list_devices возвращает устройство, второй (перед записью) - пустой список
    fake_client.list_devices.side_effect = [[dev], []]

    runner = FakeRunner()
    runner.set_response(
        ("shell", "settings", "get", "global", "global_http_proxy_pac"),
        subprocess.CompletedProcess(["adb"], 0, stdout=b"null\n", stderr=b""),
    )

    results = run_proxy_action(
        fake_client, ["dev1"], "apply", endpoint="127.0.0.1:8080", runner=runner
    )
    assert len(results) == 1
    assert not results[0].ok
    assert "Устройство недоступно перед записью" in results[0].message
