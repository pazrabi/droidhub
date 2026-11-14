"""Тесты автономного HTTP-клиента настольного контроллера DroidHub."""

from __future__ import annotations

import dataclasses
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from typing import Any, Generator
import urllib.error

import pytest

from desktop.hub_client import (
    DEFAULT_MAX_RESPONSE_BYTES,
    HubClient,
    HubClientError,
    HubDevice,
)


# --- Вспомогательные фикстуры и классы локального сервера ---

class FakeServerHandler(BaseHTTPRequestHandler):
    """Настраиваемый обработчик запросов для локального тестового сервера."""

    response_status: int = 200
    response_headers: dict[str, str] = {"Content-Type": "application/json; charset=utf-8"}
    response_body: bytes = b"[]"
    delay_seconds: float = 0.0
    received_auth_header: str | None = None
    received_path: str | None = None
    received_headers: dict[str, str] = {}
    redirect_location: str | None = None

    def log_message(self, format: str, *args: Any) -> None:
        """Подавляем вывод логов http.server в консоль тестов."""
        pass

    def do_GET(self) -> None:
        FakeServerHandler.received_auth_header = self.headers.get("Authorization")
        FakeServerHandler.received_path = self.path
        FakeServerHandler.received_headers = {k: v for k, v in self.headers.items()}

        if FakeServerHandler.delay_seconds > 0:
            time.sleep(FakeServerHandler.delay_seconds)

        self.send_response(FakeServerHandler.response_status)
        for h_name, h_val in FakeServerHandler.response_headers.items():
            self.send_header(h_name, h_val)
        if FakeServerHandler.redirect_location:
            self.send_header("Location", FakeServerHandler.redirect_location)
        self.end_headers()

        if FakeServerHandler.response_body:
            self.wfile.write(FakeServerHandler.response_body)


@pytest.fixture
def fake_http_server() -> Generator[tuple[str, int], None, None]:
    """Запускает временный локальный HTTP-сервер на 127.0.0.1 с динамическим портом."""
    FakeServerHandler.response_status = 200
    FakeServerHandler.response_headers = {"Content-Type": "application/json; charset=utf-8"}
    FakeServerHandler.response_body = b"[]"
    FakeServerHandler.delay_seconds = 0.0
    FakeServerHandler.received_auth_header = None
    FakeServerHandler.received_path = None
    FakeServerHandler.received_headers = {}
    FakeServerHandler.redirect_location = None

    server = HTTPServer(("127.0.0.1", 0), FakeServerHandler)
    port = server.server_port
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    yield f"http://127.0.0.1:{port}", port

    server.shutdown()
    server.server_close()


@pytest.fixture
def fake_https_server() -> Generator[tuple[str, int, ssl.SSLContext], None, None]:
    """Запускает временный локальный HTTPS-сервер с самоподписанным сертификатом."""
    FakeServerHandler.response_status = 200
    FakeServerHandler.response_headers = {"Content-Type": "application/json; charset=utf-8"}
    FakeServerHandler.response_body = b"[]"
    FakeServerHandler.delay_seconds = 0.0
    FakeServerHandler.received_auth_header = None
    FakeServerHandler.received_path = None
    FakeServerHandler.received_headers = {}
    FakeServerHandler.redirect_location = None

    with tempfile.TemporaryDirectory() as td:
        cert_path = os.path.join(td, "cert.pem")
        key_path = os.path.join(td, "key.pem")

        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-out",
                cert_path,
                "-keyout",
                key_path,
                "-days",
                "1",
                "-subj",
                "/CN=127.0.0.1",
                "-addext",
                "subjectAltName=IP:127.0.0.1",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        server_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        server_ctx.load_cert_chain(cert_path, key_path)

        client_ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        client_ctx.load_verify_locations(cert_path)

        server = HTTPServer(("127.0.0.1", 0), FakeServerHandler)
        server.socket = server_ctx.wrap_socket(server.socket, server_side=True)
        port = server.server_port

        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        yield f"https://127.0.0.1:{port}", port, client_ctx

        server.shutdown()
        server.server_close()


# --- Тесты HubDevice ---

class TestHubDevice:
    """Тестирование структуры данных HubDevice."""

    def test_hub_device_dataclass_fields(self) -> None:
        """Проверка обязательных полей dataclass HubDevice."""
        dev = HubDevice(
            id="device-1",
            name="Pixel 7",
            online=True,
            battery=85,
            temperature=29.4,
            screen_on=True,
            charging=False,
        )
        assert dataclasses.is_dataclass(dev)
        assert dev.id == "device-1"
        assert dev.name == "Pixel 7"
        assert dev.online is True
        assert dev.battery == 85
        assert dev.temperature == 29.4
        assert dev.screen_on is True
        assert dev.charging is False
        assert dev.display_name == "Pixel 7"

    def test_hub_device_positional_arguments(self) -> None:
        """Проверка позиционных аргументов в порядке id, name, online, battery, temperature, screen_on, charging."""
        dev = HubDevice("d1", "Phone", True, 90, 31.0, False, True)
        assert dev.id == "d1"
        assert dev.name == "Phone"
        assert dev.online is True
        assert dev.battery == 90
        assert dev.temperature == 31.0
        assert dev.screen_on is False
        assert dev.charging is True

    def test_hub_device_defaults(self) -> None:
        """Проверка значений по умолчанию для необязательных полей."""
        dev = HubDevice(id="d2", name="")
        assert dev.id == "d2"
        assert dev.name == "d2"  # fallback на id
        assert dev.online is False
        assert dev.battery is None
        assert dev.temperature is None
        assert dev.screen_on is None
        assert dev.charging is None
        assert dev.last_seen is None
        assert dev.error is None
        assert dev.busy is None
        assert dev.activity is None
        assert dev.extra == {}

    def test_hub_device_unknown_fields_preservation(self) -> None:
        """Неизвестные поля от сервера сохраняются и доступны через атрибуты и extra."""
        dev = HubDevice(
            id="d3",
            name="Custom Device",
            online=True,
            custom_key="special_value",
            android_version=14,
        )
        assert dev.custom_key == "special_value"
        assert dev.android_version == 14
        assert dev.extra["custom_key"] == "special_value"
        assert dev.extra["android_version"] == 14

    def test_hub_device_from_dict_and_conversions(self) -> None:
        """Парсинг словаря API с приведением типов."""
        data = {
            "id": "pixel6",
            "name": "Pixel 6",
            "online": 1,
            "battery": "72",
            "temperature": "28.5",
            "screen_on": 1,
            "charging": 0,
            "last_seen": "1700000000.5",
            "error": "some_err",
            "busy": True,
            "activity": "recording",
            "vendor": "Google",
        }
        dev = HubDevice.from_dict(data)
        assert dev.id == "pixel6"
        assert dev.name == "Pixel 6"
        assert dev.online is True
        assert dev.battery == 72
        assert dev.temperature == 28.5
        assert dev.screen_on is True
        assert dev.charging is False
        assert dev.last_seen == 1700000000.5
        assert dev.error == "some_err"
        assert dev.busy is True
        assert dev.activity == "recording"
        assert dev.vendor == "Google"

    def test_hub_device_from_dict_invalid_inputs(self) -> None:
        """HubDevice.from_dict проверяет структуру словаря и наличие id."""
        with pytest.raises(HubClientError, match="ожидался словарь"):
            HubDevice.from_dict("не словарь")  # type: ignore

        with pytest.raises(HubClientError, match="отсутствует обязательное поле 'id'"):
            HubDevice.from_dict({"name": "Без ID"})


# --- Тесты инициализации HubClient и параметров ---

class TestHubClientInit:
    """Тестирование инициализации и валидации параметров HubClient."""

    def test_valid_initialization(self) -> None:
        """Корректная инициализация со значениями по умолчанию."""
        client = HubClient("http://127.0.0.1:8090", "valid_token_123")
        assert client.base_url == "http://127.0.0.1:8090"
        assert client.timeout == 5.0
        assert client.devices_url == "http://127.0.0.1:8090/api/devices"

    def test_custom_timeout(self) -> None:
        """Передача пользовательского таймаута (позиционно и по ключу)."""
        client1 = HubClient("http://localhost:8090", "token", 10.5)
        assert client1.timeout == 10.5

        client2 = HubClient("http://localhost:8090", "token", timeout=3)
        assert client2.timeout == 3.0

    def test_invalid_timeout(self) -> None:
        """Ошибки при передаче некорректного таймаута."""
        with pytest.raises(HubClientError, match="положительным числом"):
            HubClient("http://127.0.0.1:8090", "token", timeout=0)

        with pytest.raises(HubClientError, match="положительным числом"):
            HubClient("http://127.0.0.1:8090", "token", timeout=-5)

        with pytest.raises(HubClientError, match="Некорректное значение таймаута"):
            HubClient("http://127.0.0.1:8090", "token", timeout="not_a_number")  # type: ignore

    def test_empty_or_invalid_token(self) -> None:
        """Проверка запрета пустых токенов и токенов с переводом строки."""
        with pytest.raises(HubClientError, match="непустой строкой"):
            HubClient("http://127.0.0.1:8090", "")

        with pytest.raises(HubClientError, match="непустой строкой"):
            HubClient("http://127.0.0.1:8090", "   ")

        with pytest.raises(HubClientError, match="непустой строкой"):
            HubClient("http://127.0.0.1:8090", None)  # type: ignore

        with pytest.raises(HubClientError, match="недопустимые символы"):
            HubClient("http://127.0.0.1:8090", "token\r\nInjected-Header: evil")

    def test_empty_or_invalid_url(self) -> None:
        """Проверка валидации базового URL."""
        with pytest.raises(HubClientError, match="непустой строкой"):
            HubClient("", "token")

        with pytest.raises(HubClientError, match="непустой строкой"):
            HubClient("   ", "token")

        with pytest.raises(HubClientError, match="непустой строкой"):
            HubClient(None, "token")  # type: ignore

    def test_context_manager(self) -> None:
        """Поддержка контекстного менеджера."""
        with HubClient("http://127.0.0.1:8090", "token") as client:
            assert client.timeout == 5.0


# --- Тесты безопасности URL и протоколов ---

class TestHubClientSecurityAndUrls:
    """Тестирование политик безопасности URL (HTTPS, localhost, credentials, query/fragment)."""

    def test_allowed_http_hosts(self) -> None:
        """HTTP разрешён исключительно для localhost, 127.0.0.1 и ::1."""
        client_lh = HubClient("http://localhost:8090", "tok")
        assert client_lh.devices_url == "http://localhost:8090/api/devices"

        client_ip4 = HubClient("http://127.0.0.1:8090", "tok")
        assert client_ip4.devices_url == "http://127.0.0.1:8090/api/devices"

        client_ip6 = HubClient("http://[::1]:8090", "tok")
        assert client_ip6.devices_url == "http://[::1]:8090/api/devices"

    def test_forbidden_http_hosts(self) -> None:
        """HTTP запрещён для любых внешних хостов и локальных IP кроме 127.0.0.1/::1/localhost."""
        forbidden_urls = [
            "http://hub.example.com",
            "http://droidhub.company.internal:8090",
            "http://192.168.1.100:8090",
            "http://10.0.0.1:8090",
            "http://127.0.0.2:8090",
            "http://0.0.0.0:8090",
        ]
        for url in forbidden_urls:
            with pytest.raises(HubClientError, match="Протокол HTTP разрешён только для localhost"):
                HubClient(url, "tok")

    def test_https_allowed_for_any_host(self) -> None:
        """HTTPS разрешён для любых хостов."""
        c1 = HubClient("https://hub.example.com", "tok")
        assert c1.devices_url == "https://hub.example.com/api/devices"

        c2 = HubClient("https://127.0.0.1:8443", "tok")
        assert c2.devices_url == "https://127.0.0.1:8443/api/devices"

    def test_forbidden_schemes(self) -> None:
        """Запрет протоколов отличных от HTTPS и HTTP."""
        for scheme_url in ["ftp://localhost:21", "file:///etc/passwd", "ws://localhost:8090", "javascript:void(0)"]:
            with pytest.raises(HubClientError, match="Недопустимый протокол"):
                HubClient(scheme_url, "tok")

    def test_credentials_forbidden_in_url(self) -> None:
        """Запрет учётных данных (user:password) в URL."""
        with pytest.raises(HubClientError, match="Учётные данные в URL запрещены"):
            HubClient("http://admin:secret@localhost:8090", "tok")

        with pytest.raises(HubClientError, match="Учётные данные в URL запрещены"):
            HubClient("https://token@hub.example.com", "tok")

    def test_query_and_fragment_forbidden_in_url(self) -> None:
        """Запрет query-параметров и фрагментов в URL для исключения утечек токена."""
        with pytest.raises(HubClientError, match="Параметры запроса"):
            HubClient("http://localhost:8090?api_key=secret", "tok")

        with pytest.raises(HubClientError, match="Параметры запроса"):
            HubClient("https://hub.example.com/api?debug=true", "tok")

        with pytest.raises(HubClientError, match="Фрагменты"):
            HubClient("http://localhost:8090#section", "tok")

    def test_url_normalization_variations(self) -> None:
        """Корректное построение /api/devices для различных базовых путей."""
        cases = [
            ("http://localhost:8090", "http://localhost:8090/api/devices"),
            ("http://localhost:8090/", "http://localhost:8090/api/devices"),
            ("http://localhost:8090/api", "http://localhost:8090/api/devices"),
            ("http://localhost:8090/api/", "http://localhost:8090/api/devices"),
            ("http://localhost:8090/api/devices", "http://localhost:8090/api/devices"),
            ("https://proxy.example.com/droidhub", "https://proxy.example.com/droidhub/api/devices"),
            ("https://proxy.example.com/droidhub/", "https://proxy.example.com/droidhub/api/devices"),
        ]
        for base_in, expected_out in cases:
            c = HubClient(base_in, "tok")
            assert c.devices_url == expected_out


# --- Тесты маскирования токена ---

class TestTokenMasking:
    """Тестирование защиты и маскирования токена."""

    def test_repr_and_str_mask_token(self) -> None:
        """Токен маскируется в __repr__ и __str__."""
        secret = "ultra_secret_token_never_expose_me_12345"
        client = HubClient("http://localhost:8090", secret)
        assert secret not in repr(client)
        assert secret not in str(client)
        assert "token='***'" in repr(client)
        assert "token='***'" in str(client)

    def test_token_not_in_exception_messages(self, fake_http_server: tuple[str, int]) -> None:
        """Токен никогда не попадает в текст возникающих исключений."""
        base_url, _ = fake_http_server
        secret = "my_confidential_bearer_token_xyz"
        FakeServerHandler.response_status = 401
        FakeServerHandler.response_body = f'{{"detail": "bad token {secret}"}}'.encode("utf-8")

        client = HubClient(base_url, secret)
        with pytest.raises(HubClientError) as exc_info:
            client.list_devices()

        error_text = str(exc_info.value)
        assert secret not in error_text


# --- Тесты запрета редиректов ---

class TestRedirectBlocking:
    """Тестирование блокировки перенаправлений (редиректов)."""

    @pytest.mark.parametrize("status_code", [301, 302, 303, 307, 308])
    def test_redirects_are_strictly_forbidden(
        self,
        fake_http_server: tuple[str, int],
        status_code: int,
    ) -> None:
        """Любой HTTP 3xx редирект немедленно прерывается с ошибкой HubClientError."""
        base_url, _ = fake_http_server
        FakeServerHandler.response_status = status_code
        FakeServerHandler.redirect_location = "http://127.0.0.1:9999/unauthorized_destination"
        FakeServerHandler.response_body = b"Redirecting"

        client = HubClient(base_url, "my_token")
        with pytest.raises(HubClientError) as exc_info:
            client.list_devices()

        assert "Редиректы запрещены" in str(exc_info.value)
        assert str(status_code) in str(exc_info.value)


# --- Тесты сетевых запросов и разбора ответов ---

class TestDeviceListAndErrorHandling:
    """Тестирование реального обмена с локальным сервером (GET /api/devices)."""

    def test_list_devices_success(self, fake_http_server: tuple[str, int]) -> None:
        """Успешный запрос списка устройств и проверка заголовка Bearer."""
        base_url, _ = fake_http_server
        FakeServerHandler.response_status = 200
        FakeServerHandler.response_body = json.dumps([
            {
                "id": "dev-1",
                "name": "Pixel 7 Pro",
                "online": True,
                "battery": 88,
                "temperature": 27.5,
                "screen_on": True,
                "charging": False,
                "last_seen": 1700000000.0,
                "busy": False,
                "activity": None,
            },
            {
                "id": "dev-2",
                "name": "Galaxy S23",
                "online": False,
                "battery": None,
                "temperature": None,
                "screen_on": None,
                "charging": None,
            },
        ]).encode("utf-8")

        client = HubClient(base_url, "secret_auth_token_999")
        devices = client.list_devices()

        # Проверка переданного заголовка Authorization Bearer
        assert FakeServerHandler.received_auth_header == "Bearer secret_auth_token_999"
        assert FakeServerHandler.received_path == "/api/devices"

        # Проверка разбора устройств
        assert len(devices) == 2

        d1 = devices[0]
        assert d1.id == "dev-1"
        assert d1.name == "Pixel 7 Pro"
        assert d1.online is True
        assert d1.battery == 88
        assert d1.temperature == 27.5
        assert d1.screen_on is True
        assert d1.charging is False
        assert d1.busy is False

        d2 = devices[1]
        assert d2.id == "dev-2"
        assert d2.name == "Galaxy S23"
        assert d2.online is False
        assert d2.battery is None
        assert d2.charging is None

    def test_list_devices_empty_list(self, fake_http_server: tuple[str, int]) -> None:
        """Сервер возвращает пустой список устройств []."""
        base_url, _ = fake_http_server
        FakeServerHandler.response_status = 200
        FakeServerHandler.response_body = b"[]"

        client = HubClient(base_url, "token")
        devices = client.list_devices()
        assert devices == []

    def test_http_401_unauthorized(self, fake_http_server: tuple[str, int]) -> None:
        """Обработка HTTP 401 Unauthorized с понятным русским сообщением."""
        base_url, _ = fake_http_server
        FakeServerHandler.response_status = 401
        FakeServerHandler.response_body = b'{"detail": "Invalid token"}'

        client = HubClient(base_url, "wrong_token")
        with pytest.raises(HubClientError, match="Ошибка авторизации: неверный или недействительный токен"):
            client.list_devices()

    def test_http_403_forbidden(self, fake_http_server: tuple[str, int]) -> None:
        """Обработка HTTP 403 Forbidden с понятным русским сообщением."""
        base_url, _ = fake_http_server
        FakeServerHandler.response_status = 403
        FakeServerHandler.response_body = b'{"detail": "Forbidden"}'

        client = HubClient(base_url, "token")
        with pytest.raises(HubClientError, match="Доступ запрещён: недостаточно прав"):
            client.list_devices()

    def test_http_404_not_found(self, fake_http_server: tuple[str, int]) -> None:
        """Обработка HTTP 404 Not Found."""
        base_url, _ = fake_http_server
        FakeServerHandler.response_status = 404

        client = HubClient(base_url, "token")
        with pytest.raises(HubClientError, match="Эндпоинт /api/devices не найден"):
            client.list_devices()

    def test_http_429_rate_limited(self, fake_http_server: tuple[str, int]) -> None:
        """Обработка HTTP 429 Too Many Requests."""
        base_url, _ = fake_http_server
        FakeServerHandler.response_status = 429

        client = HubClient(base_url, "token")
        with pytest.raises(HubClientError, match="Превышен лимит запросов"):
            client.list_devices()

    def test_http_500_502_503_server_errors(self, fake_http_server: tuple[str, int]) -> None:
        """Обработка серверных ошибок 500, 502, 503."""
        base_url, _ = fake_http_server
        client = HubClient(base_url, "token")

        FakeServerHandler.response_status = 500
        with pytest.raises(HubClientError, match="Внутренняя ошибка сервера"):
            client.list_devices()

        FakeServerHandler.response_status = 502
        with pytest.raises(HubClientError, match="Ошибка шлюза или сетевого адаптера"):
            client.list_devices()

        FakeServerHandler.response_status = 503
        with pytest.raises(HubClientError, match="Сервер DroidHub временно недоступен"):
            client.list_devices()

    def test_malformed_json_response(self, fake_http_server: tuple[str, int]) -> None:
        """Ошибка при некорректном JSON в ответе."""
        base_url, _ = fake_http_server
        FakeServerHandler.response_status = 200
        FakeServerHandler.response_body = b"{not a json valid at all"

        client = HubClient(base_url, "token")
        with pytest.raises(HubClientError, match="Некорректный JSON в ответе"):
            client.list_devices()

    def test_non_list_json_response(self, fake_http_server: tuple[str, int]) -> None:
        """Ошибка если корневой элемент JSON не список."""
        base_url, _ = fake_http_server
        FakeServerHandler.response_status = 200
        FakeServerHandler.response_body = b'{"status": "ok", "message": "hello"}'

        client = HubClient(base_url, "token")
        with pytest.raises(HubClientError, match="ожидался список устройств, получен dict"):
            client.list_devices()

    def test_invalid_device_element_in_list(self, fake_http_server: tuple[str, int]) -> None:
        """Ошибка если элемент списка не является объектом или не содержит id."""
        base_url, _ = fake_http_server
        client = HubClient(base_url, "token")

        # Элемент не словарь
        FakeServerHandler.response_body = b'[123, "string"]'
        with pytest.raises(HubClientError, match="ожидался словарь"):
            client.list_devices()

        # Словарь без поля id
        FakeServerHandler.response_body = b'[{"name": "No ID Device"}]'
        with pytest.raises(HubClientError, match="отсутствует обязательное поле 'id'"):
            client.list_devices()

    def test_response_size_limit_content_length(self, fake_http_server: tuple[str, int]) -> None:
        """Проверка превышения лимита размера по Content-Length."""
        base_url, _ = fake_http_server
        FakeServerHandler.response_status = 200
        FakeServerHandler.response_headers = {
            "Content-Type": "application/json",
            "Content-Length": str(DEFAULT_MAX_RESPONSE_BYTES + 500),
        }
        FakeServerHandler.response_body = b"[]"

        client = HubClient(base_url, "token")
        with pytest.raises(HubClientError, match="превышает допустимый лимит"):
            client.list_devices()

    def test_response_size_limit_stream_body(self, fake_http_server: tuple[str, int]) -> None:
        """Проверка превышения лимита размера при потоковом чтении тела ответа."""
        base_url, _ = fake_http_server
        FakeServerHandler.response_status = 200
        FakeServerHandler.response_headers = {"Content-Type": "application/json"}
        # Устанавливаем размер тела чуть больше лимита
        client = HubClient(base_url, "token", max_response_bytes=100)
        FakeServerHandler.response_body = b"[" + b'{"id":"1"},' * 20 + b'{"id":"2"}]'

        with pytest.raises(HubClientError, match="превышает допустимый лимит"):
            client.list_devices()

    def test_timeout_handling(self, fake_http_server: tuple[str, int]) -> None:
        """Проверка обработки превышения таймаута."""
        base_url, _ = fake_http_server
        FakeServerHandler.response_status = 200
        FakeServerHandler.delay_seconds = 0.5

        client = HubClient(base_url, "token", timeout=0.1)
        with pytest.raises(HubClientError, match="Превышено время ожидания ответа"):
            client.list_devices()

    def test_connection_refused_handling(self) -> None:
        """Проверка обработки ошибки подключения к неработающему серверу."""
        # Подбираем свободный порт и сразу закрываем сокет
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        unused_port = sock.getsockname()[1]
        sock.close()

        client = HubClient(f"http://127.0.0.1:{unused_port}", "token", timeout=1.0)
        with pytest.raises(HubClientError, match="соединение отклонено"):
            client.list_devices()


# --- Тесты HTTPS и валидации сертификатов ---

class TestHttpsAndSsl:
    """Тестирование взаимодействия по протоколу HTTPS и проверки сертификатов."""

    def test_https_strict_verification_fails_untrusted(
        self,
        fake_https_server: tuple[str, int, ssl.SSLContext],
    ) -> None:
        """Клиент с обычной проверкой сертификата отвергает недоверенный самоподписанный сертификат."""
        base_url, _, _ = fake_https_server
        client = HubClient(base_url, "test_token")
        with pytest.raises(HubClientError, match="Ошибка проверки SSL-сертификата"):
            client.list_devices()

    def test_https_success_with_trusted_ssl_context(
        self,
        fake_https_server: tuple[str, int, ssl.SSLContext],
    ) -> None:
        """Клиент с доверенным тестовым SSL-контекстом успешно выполняет запрос по HTTPS."""
        base_url, _, client_ctx = fake_https_server
        FakeServerHandler.response_status = 200
        FakeServerHandler.response_body = json.dumps([
            {
                "id": "https-phone",
                "name": "Secure Phone",
                "online": True,
                "battery": 99,
                "temperature": 25.0,
                "screen_on": False,
                "charging": True,
            }
        ]).encode("utf-8")

        client = HubClient(base_url, "my_https_token", ssl_context=client_ctx)
        devices = client.list_devices()

        assert len(devices) == 1
        assert devices[0].id == "https-phone"
        assert devices[0].name == "Secure Phone"
        assert devices[0].online is True
        assert devices[0].battery == 99
        assert FakeServerHandler.received_auth_header == "Bearer my_https_token"
