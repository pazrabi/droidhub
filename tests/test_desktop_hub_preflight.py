"""Тесты для desktop/hub_preflight.py.

Проверяемые свойства:
  - При disabled=False (startup выключен) функция возвращает 2 и НЕ вызывает
    read_token / list_devices (нет обращения к Keychain/сети).
  - Ошибка load_settings → код 3.
  - Ошибка read_token → код 3.
  - Ошибка list_devices → код 4.
  - Успех с 0 устройствами → код 0, точная строка в output.
  - Успех с N устройствами → код 0, точная строка в output.
  - Секрет (токен) никогда не появляется в output/errors.
  - Текст исключений никогда не появляется в output/errors.
"""

from __future__ import annotations

import io
import unittest

from desktop.hub_preflight import run_hub_preflight


# ---------------------------------------------------------------------------
# Вспомогательные заглушки
# ---------------------------------------------------------------------------

_URL = "https://hub.example.com"
_SECRET_TOKEN = "super-secret-bearer-token-xyzzy"


def _ok_settings() -> tuple[str, bool]:
    return (_URL, True)


def _disabled_settings() -> tuple[str, bool]:
    return (_URL, False)


def _failing_settings() -> tuple[str, bool]:
    raise RuntimeError("DB locked — secret detail inside")


def _ok_token(url: str) -> str:  # noqa: ARG001
    return _SECRET_TOKEN


def _failing_token(url: str) -> str:  # noqa: ARG001
    raise PermissionError("Keychain denied — secret passphrase inside")


def _ok_devices(url: str, token: object) -> list[str]:  # noqa: ARG001
    return []


def _n_devices(n: int) -> list[str]:
    return [f"device-{i}" for i in range(n)]


def _failing_devices(url: str, token: object) -> list[str]:  # noqa: ARG001
    raise ConnectionError("TLS handshake failed — secret cert data inside")


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------


class TestDisabledStartup(unittest.TestCase):
    """Когда Hub выключен в настройках, должен возвращать 2 без доп. вызовов."""

    def setUp(self) -> None:
        self.read_token_called = False
        self.list_devices_called = False

        def spy_token(url: str) -> str:
            self.read_token_called = True
            return _SECRET_TOKEN

        def spy_devices(url: str, token: object) -> list[str]:
            self.list_devices_called = True
            return []

        self.output = io.StringIO()
        self.errors = io.StringIO()
        self.code = run_hub_preflight(
            load_settings=_disabled_settings,
            read_token=spy_token,
            list_devices=spy_devices,
            output=self.output,
            errors=self.errors,
        )

    def test_exit_code_is_2(self) -> None:
        self.assertEqual(self.code, 2)

    def test_no_token_read(self) -> None:
        self.assertFalse(self.read_token_called, "read_token не должен вызываться при disabled")

    def test_no_devices_read(self) -> None:
        self.assertFalse(self.list_devices_called, "list_devices не должен вызываться при disabled")

    def test_output_empty(self) -> None:
        self.assertEqual(self.output.getvalue(), "")


class TestSettingsError(unittest.TestCase):
    """Ошибка load_settings → код 3, безопасное сообщение в errors."""

    def setUp(self) -> None:
        self.output = io.StringIO()
        self.errors = io.StringIO()
        self.code = run_hub_preflight(
            load_settings=_failing_settings,
            read_token=_ok_token,
            list_devices=_ok_devices,
            output=self.output,
            errors=self.errors,
        )

    def test_exit_code_is_3(self) -> None:
        self.assertEqual(self.code, 3)

    def test_errors_not_empty(self) -> None:
        self.assertGreater(len(self.errors.getvalue()), 0)

    def test_output_empty(self) -> None:
        self.assertEqual(self.output.getvalue(), "")

    def test_exception_text_not_leaked(self) -> None:
        combined = self.output.getvalue() + self.errors.getvalue()
        self.assertNotIn("DB locked", combined)
        self.assertNotIn("secret detail", combined)


class TestTokenError(unittest.TestCase):
    """Ошибка read_token → код 3, текст исключения не утекает."""

    def setUp(self) -> None:
        self.output = io.StringIO()
        self.errors = io.StringIO()
        self.code = run_hub_preflight(
            load_settings=_ok_settings,
            read_token=_failing_token,
            list_devices=_ok_devices,
            output=self.output,
            errors=self.errors,
        )

    def test_exit_code_is_3(self) -> None:
        self.assertEqual(self.code, 3)

    def test_errors_not_empty(self) -> None:
        self.assertGreater(len(self.errors.getvalue()), 0)

    def test_output_empty(self) -> None:
        self.assertEqual(self.output.getvalue(), "")

    def test_exception_text_not_leaked(self) -> None:
        combined = self.output.getvalue() + self.errors.getvalue()
        self.assertNotIn("Keychain denied", combined)
        self.assertNotIn("secret passphrase", combined)


class TestDevicesError(unittest.TestCase):
    """Ошибка list_devices → код 4, текст исключения не утекает."""

    def setUp(self) -> None:
        self.output = io.StringIO()
        self.errors = io.StringIO()
        self.code = run_hub_preflight(
            load_settings=_ok_settings,
            read_token=_ok_token,
            list_devices=_failing_devices,
            output=self.output,
            errors=self.errors,
        )

    def test_exit_code_is_4(self) -> None:
        self.assertEqual(self.code, 4)

    def test_errors_not_empty(self) -> None:
        self.assertGreater(len(self.errors.getvalue()), 0)

    def test_output_empty(self) -> None:
        self.assertEqual(self.output.getvalue(), "")

    def test_exception_text_not_leaked(self) -> None:
        combined = self.output.getvalue() + self.errors.getvalue()
        self.assertNotIn("TLS handshake", combined)
        self.assertNotIn("secret cert", combined)


class TestSuccessZeroDevices(unittest.TestCase):
    """Успех с 0 устройствами → код 0, точная строка."""

    def setUp(self) -> None:
        self.output = io.StringIO()
        self.errors = io.StringIO()
        self.code = run_hub_preflight(
            load_settings=_ok_settings,
            read_token=_ok_token,
            list_devices=_ok_devices,
            output=self.output,
            errors=self.errors,
        )

    def test_exit_code_is_0(self) -> None:
        self.assertEqual(self.code, 0)

    def test_exact_output(self) -> None:
        self.assertEqual(self.output.getvalue(), "Hub подключён. Устройств: 0.\n")

    def test_errors_empty(self) -> None:
        self.assertEqual(self.errors.getvalue(), "")


class TestSuccessMultipleDevices(unittest.TestCase):
    """Успех с N устройствами → код 0, точная строка с N."""

    def _run(self, n: int) -> tuple[int, str]:
        output = io.StringIO()
        errors = io.StringIO()
        code = run_hub_preflight(
            load_settings=_ok_settings,
            read_token=_ok_token,
            list_devices=lambda url, token: _n_devices(n),
            output=output,
            errors=errors,
        )
        return code, output.getvalue()

    def test_three_devices(self) -> None:
        code, out = self._run(3)
        self.assertEqual(code, 0)
        self.assertEqual(out, "Hub подключён. Устройств: 3.\n")

    def test_one_device(self) -> None:
        code, out = self._run(1)
        self.assertEqual(code, 0)
        self.assertEqual(out, "Hub подключён. Устройств: 1.\n")


class TestSecretNeverLeaks(unittest.TestCase):
    """Токен никогда не появляется в output/errors при любом исходе."""

    def _streams_contain_secret(
        self,
        load_settings: object,
        read_token: object,
        list_devices: object,
    ) -> bool:
        output = io.StringIO()
        errors = io.StringIO()
        run_hub_preflight(
            load_settings=load_settings,  # type: ignore[arg-type]
            read_token=read_token,  # type: ignore[arg-type]
            list_devices=list_devices,  # type: ignore[arg-type]
            output=output,
            errors=errors,
        )
        combined = output.getvalue() + errors.getvalue()
        return _SECRET_TOKEN in combined

    def test_no_leak_on_success(self) -> None:
        self.assertFalse(
            self._streams_contain_secret(_ok_settings, _ok_token, _ok_devices)
        )

    def test_no_leak_on_devices_error(self) -> None:
        self.assertFalse(
            self._streams_contain_secret(_ok_settings, _ok_token, _failing_devices)
        )

    def test_no_leak_on_token_error(self) -> None:
        # read_token падает до возврата токена — утечки нет по определению
        self.assertFalse(
            self._streams_contain_secret(_ok_settings, _failing_token, _ok_devices)
        )


if __name__ == "__main__":
    unittest.main()
