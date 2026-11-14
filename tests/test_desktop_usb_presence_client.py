"""Тесты синхронного HTTPS-клиента USB-присутствия USBPresenceClient."""

from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
import socket
import ssl
import subprocess
import tempfile
import threading
from typing import Any, Generator
import urllib.error
import urllib.request
import uuid

import pytest

from desktop.hub_client import HubClientError
from desktop.usb_presence_client import (
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_TIMEOUT,
    USBPresenceAuthError,
    USBPresenceClient,
    USBPresenceClientError,
    USBPresenceConflictError,
    USBPresenceNetworkError,
    USBPresenceNotFoundError,
    USBPresenceSessionConflictError,
)


# --- Вспомогательный класс и фикстура локального TLS сервера ---

class FakeTLSServerHandler(BaseHTTPRequestHandler):
    """Настраиваемый обработчик запросов для локального HTTPS-сервера."""

    response_status: int = 200
    response_headers: dict[str, str] = {"Content-Type": "application/json; charset=utf-8"}
    response_body: bytes = b'{"session_id": "c9bf9e571685472eb136c26731234567"}'
    expected_token: str = "valid-secret-token"

    last_method: str | None = None
    last_path: str | None = None
    last_headers: dict[str, str] = {}
    last_body: bytes = b""

    def log_message(self, format: str, *args: Any) -> None:
        """Подавляем логи сервера в консоль тестов."""
        pass

    def _handle_request(self) -> None:
        FakeTLSServerHandler.last_method = self.command
        FakeTLSServerHandler.last_path = self.path
        FakeTLSServerHandler.last_headers = {k: v for k, v in self.headers.items()}

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 0:
            FakeTLSServerHandler.last_body = self.rfile.read(content_length)
        else:
            FakeTLSServerHandler.last_body = b""

        auth_header = self.headers.get("Authorization", "")
        if FakeTLSServerHandler.expected_token:
            if auth_header != f"Bearer {FakeTLSServerHandler.expected_token}":
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "Unauthorized"}')
                return

        self.send_response(FakeTLSServerHandler.response_status)
        for h_name, h_val in FakeTLSServerHandler.response_headers.items():
            self.send_header(h_name, h_val)
        self.end_headers()

        if FakeTLSServerHandler.response_body and FakeTLSServerHandler.response_status != 204:
            self.wfile.write(FakeTLSServerHandler.response_body)

    do_GET = _handle_request
    do_POST = _handle_request
    do_PUT = _handle_request
    do_DELETE = _handle_request


@pytest.fixture
def fake_https_server() -> Generator[tuple[str, int, ssl.SSLContext], None, None]:
    """Запускает временный локальный HTTPS-сервер с самоподписанным сертификатом."""
    FakeTLSServerHandler.response_status = 200
    FakeTLSServerHandler.response_headers = {"Content-Type": "application/json; charset=utf-8"}
    FakeTLSServerHandler.response_body = b'{"session_id": "c9bf9e571685472eb136c26731234567"}'
    FakeTLSServerHandler.expected_token = "valid-secret-token"
    FakeTLSServerHandler.last_method = None
    FakeTLSServerHandler.last_path = None
    FakeTLSServerHandler.last_headers = {}
    FakeTLSServerHandler.last_body = b""

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

        server = HTTPServer(("127.0.0.1", 0), FakeTLSServerHandler)
        server.socket = server_ctx.wrap_socket(server.socket, server_side=True)
        port = server.server_port

        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        yield f"https://127.0.0.1:{port}", port, client_ctx

        server.shutdown()
        server.server_close()


# --- Вспомогательный макет HTTP-ответа для тестов моков ---

class FakeHTTPResponse:
    """Макет объекта HTTP-ответа urllib."""

    def __init__(
        self,
        status: int = 200,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.code = status
        self._body = io.BytesIO(body)
        raw_headers = headers or {}
        # Эмуляция структуры заголовков HTTPMessage
        self.headers = http.client.HTTPMessage()
        for k, v in raw_headers.items():
            self.headers.add_header(k, v)

    def read(self, amt: int | None = None) -> bytes:
        if amt is None:
            return self._body.read()
        return self._body.read(amt)

    def __enter__(self) -> FakeHTTPResponse:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass


# --- Тесты инициализации, URL и параметров ---

class TestUSBPresenceClientInit:
    """Тестирование инициализации и валидации URL клиента."""

    @pytest.mark.parametrize(
        "raw_url,expected_session_url",
        [
            ("https://hub.example.com", "https://hub.example.com/api/usb-presence/session"),
            ("https://hub.example.com/", "https://hub.example.com/api/usb-presence/session"),
            ("https://hub.example.com/api", "https://hub.example.com/api/usb-presence/session"),
            ("https://hub.example.com/api/", "https://hub.example.com/api/usb-presence/session"),
            ("https://hub.example.com/api/devices", "https://hub.example.com/api/usb-presence/session"),
            ("https://hub.example.com/api/devices/", "https://hub.example.com/api/usb-presence/session"),
            ("https://hub.example.com/myprefix", "https://hub.example.com/myprefix/api/usb-presence/session"),
            ("https://hub.example.com/myprefix/", "https://hub.example.com/myprefix/api/usb-presence/session"),
            ("https://hub.example.com/myprefix/api", "https://hub.example.com/myprefix/api/usb-presence/session"),
            ("https://hub.example.com/myprefix/api/", "https://hub.example.com/myprefix/api/usb-presence/session"),
            ("https://hub.example.com/myprefix/api/devices", "https://hub.example.com/myprefix/api/usb-presence/session"),
            ("https://hub.example.com/myprefix/api/devices/", "https://hub.example.com/myprefix/api/usb-presence/session"),
            ("https://hub.example.com/api/usb-presence/session", "https://hub.example.com/api/usb-presence/session"),
            ("https://hub.example.com/deploy/api/usb-presence", "https://hub.example.com/deploy/api/usb-presence/session"),
        ],
    )
    def test_base_url_variations(self, raw_url: str, expected_session_url: str) -> None:
        """Проверка всех допустимых форм base_url, включая префикс deployment."""
        client = USBPresenceClient(raw_url, "test-token")
        assert client.session_url == expected_session_url

    @pytest.mark.parametrize(
        "invalid_scheme_url",
        [
            "http://127.0.0.1:8080",
            "http://localhost:8080",
            "http://::1:8080",
            "http://hub.example.com",
            "ftp://hub.example.com",
            "ws://hub.example.com",
        ],
    )
    def test_strictly_https_required(self, invalid_scheme_url: str) -> None:
        """Проверка строгого запрета HTTP (даже для localhost) и любых других протоколов."""
        with pytest.raises(USBPresenceClientError) as exc_info:
            USBPresenceClient(invalid_scheme_url, "test-token")
        assert "HTTPS" in str(exc_info.value)

    @pytest.mark.parametrize(
        "bad_url",
        [
            "",
            "   ",
            "https://user:password@hub.example.com",
            "https://hub.example.com?query=1",
            "https://hub.example.com#section",
            "https:///api/devices",
        ],
    )
    def test_invalid_urls_rejected(self, bad_url: str) -> None:
        """Проверка отклонения некорректных URL (учетные данные, query, fragment, пустые)."""
        with pytest.raises(USBPresenceClientError):
            USBPresenceClient(bad_url, "test-token")

    @pytest.mark.parametrize(
        "bad_token",
        [
            "",
            "   ",
            "token\r\n",
            "token\nwith_newline",
            None,
        ],
    )
    def test_invalid_tokens_rejected(self, bad_token: Any) -> None:
        """Проверка отклонения пустых и небезопасных токенов."""
        with pytest.raises(USBPresenceClientError):
            USBPresenceClient("https://hub.example.com", bad_token)

    @pytest.mark.parametrize("bad_timeout", [0, -1, -5.5, "five", None])
    def test_invalid_timeout_rejected(self, bad_timeout: Any) -> None:
        """Проверка отклонения некорректных значений таймаута."""
        with pytest.raises(USBPresenceClientError):
            USBPresenceClient("https://hub.example.com", "test-token", timeout=bad_timeout)

    def test_token_masked_in_repr_and_str(self) -> None:
        """Токен не должен фигурировать в строковом представлении клиента."""
        token = "secret-super-token-12345"
        client = USBPresenceClient("https://hub.example.com", token)
        assert token not in repr(client)
        assert token not in str(client)
        assert "***" in repr(client)
        assert "***" in str(client)

    def test_context_manager_support(self) -> None:
        """Поддержка работы в контекстном менеджере with."""
        with USBPresenceClient("https://hub.example.com", "token") as client:
            assert client.timeout == DEFAULT_TIMEOUT
        client.close()


# --- Тесты методов open_session, publish_snapshot, close_session (через моки) ---

class TestUSBPresenceClientMethods:
    """Тестирование функциональности методов с изоляцией сетевого транспорта."""

    def test_open_session_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Успешное открытие сессии: POST без тела, ответ JSON session_id."""
        client = USBPresenceClient("https://hub.example.com", "valid-token")
        valid_id = "c9bf9e571685472eb136c26731234567"

        recorded_req: urllib.request.Request | None = None

        def fake_open(req: urllib.request.Request, timeout: float = 5.0) -> FakeHTTPResponse:
            nonlocal recorded_req
            recorded_req = req
            return FakeHTTPResponse(
                status=200,
                body=json.dumps({"session_id": valid_id}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )

        monkeypatch.setattr(client._opener, "open", fake_open)

        session_id = client.open_session()
        assert session_id == valid_id
        assert recorded_req is not None
        assert recorded_req.get_method() == "POST"
        assert recorded_req.full_url == "https://hub.example.com/api/usb-presence/session"
        assert recorded_req.headers.get("Authorization") == "Bearer valid-token"
        assert recorded_req.headers.get("Accept") == "application/json"
        assert recorded_req.data is None

    @pytest.mark.parametrize(
        "invalid_session_id",
        [
            "C9BF9E571685472EB136C26731234567",  # Upper case
            "c9bf9e57-1685-472e-b136-c26731234567",  # With hyphens
            "c9bf9e571685472eb136c2673123456g",  # Non-hex character
            "short_session",  # Too short
            "c9bf9e571685472eb136c267312345678",  # Too long (33 chars)
            "",
            None,
            12345,
        ],
    )
    def test_open_session_rejects_invalid_session_id(
        self, monkeypatch: pytest.MonkeyPatch, invalid_session_id: Any
    ) -> None:
        """Открытие сессии отклоняет session_id, не соответствующий opaque lowercase hex 32."""
        client = USBPresenceClient("https://hub.example.com", "valid-token")

        def fake_open(req: urllib.request.Request, timeout: float = 5.0) -> FakeHTTPResponse:
            return FakeHTTPResponse(
                status=200,
                body=json.dumps({"session_id": invalid_session_id}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )

        monkeypatch.setattr(client._opener, "open", fake_open)

        with pytest.raises(USBPresenceClientError) as exc_info:
            client.open_session()
        assert "session_id" in str(exc_info.value) or "сесси" in str(exc_info.value)

    def test_open_session_malformed_json_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Открытие сессии с некорректным JSON не раскрывает тело ответа."""
        client = USBPresenceClient("https://hub.example.com", "valid-token")
        secret_body = b"<html><title>Error 502 with secret token leak</title></html>"

        def fake_open(req: urllib.request.Request, timeout: float = 5.0) -> FakeHTTPResponse:
            return FakeHTTPResponse(status=200, body=secret_body)

        monkeypatch.setattr(client._opener, "open", fake_open)

        with pytest.raises(USBPresenceClientError) as exc_info:
            client.open_session()
        # Тело ответа не должно утекать в текст исключения
        assert "secret token leak" not in str(exc_info.value)
        assert "<html>" not in str(exc_info.value)

    def test_publish_snapshot_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Успешная публикация снимка: PUT с телом JSON {"device_ids": [...]}, успех 204."""
        client = USBPresenceClient("https://hub.example.com", "valid-token")
        valid_id = "c9bf9e571685472eb136c26731234567"
        device_ids = ["dev-serial-1", "dev-serial-2"]

        recorded_req: urllib.request.Request | None = None

        def fake_open(req: urllib.request.Request, timeout: float = 5.0) -> FakeHTTPResponse:
            nonlocal recorded_req
            recorded_req = req
            return FakeHTTPResponse(status=204, body=b"")

        monkeypatch.setattr(client._opener, "open", fake_open)

        res = client.publish_snapshot(valid_id, device_ids)
        assert res is None
        assert recorded_req is not None
        assert recorded_req.get_method() == "PUT"
        assert recorded_req.full_url == f"https://hub.example.com/api/usb-presence/session/{valid_id}"
        assert recorded_req.headers.get("Authorization") == "Bearer valid-token"
        assert recorded_req.get_header("Content-type") == "application/json"
        assert recorded_req.data is not None
        parsed_body = json.loads(recorded_req.data.decode("utf-8"))
        assert parsed_body == {"device_ids": device_ids}

    def test_publish_snapshot_empty_device_ids_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Публикация пустого списка устройств допустима (например, при отключении всех USB)."""
        client = USBPresenceClient("https://hub.example.com", "valid-token")
        valid_id = "c9bf9e571685472eb136c26731234567"

        recorded_req: urllib.request.Request | None = None

        def fake_open(req: urllib.request.Request, timeout: float = 5.0) -> FakeHTTPResponse:
            nonlocal recorded_req
            recorded_req = req
            return FakeHTTPResponse(status=204, body=b"")

        monkeypatch.setattr(client._opener, "open", fake_open)

        client.publish_snapshot(valid_id, [])
        assert recorded_req is not None
        assert json.loads(recorded_req.data.decode("utf-8")) == {"device_ids": []}

    @pytest.mark.parametrize(
        "bad_device_ids",
        [
            "single-device-string",  # Строка является Sequence, но не разрешена
            b"bytes-sequence",
            None,
            [123],
            ["valid-id", ""],
            ["valid-id", "   "],
            ["valid-id", None],
        ],
    )
    def test_publish_snapshot_invalid_device_ids(
        self, monkeypatch: pytest.MonkeyPatch, bad_device_ids: Any
    ) -> None:
        """Проверка валидации последовательности идентификаторов устройств."""
        client = USBPresenceClient("https://hub.example.com", "valid-token")
        valid_id = "c9bf9e571685472eb136c26731234567"

        with pytest.raises(USBPresenceClientError):
            client.publish_snapshot(valid_id, bad_device_ids)

    @pytest.mark.parametrize(
        "bad_session_id",
        [
            "invalid-session",
            "C9BF9E571685472EB136C26731234567",
            "c9bf9e57-1685-472e-b136-c26731234567",
            "",
            None,
        ],
    )
    def test_publish_snapshot_invalid_session_id(self, bad_session_id: Any) -> None:
        """Публикация снимка сразу отклоняет некорректный session_id без запроса."""
        client = USBPresenceClient("https://hub.example.com", "valid-token")
        with pytest.raises(USBPresenceClientError):
            client.publish_snapshot(bad_session_id, ["device-1"])

    def test_close_session_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Успешное закрытие сессии: DELETE по пути /session/{id}, успех 204."""
        client = USBPresenceClient("https://hub.example.com", "valid-token")
        valid_id = "c9bf9e571685472eb136c26731234567"

        recorded_req: urllib.request.Request | None = None

        def fake_open(req: urllib.request.Request, timeout: float = 5.0) -> FakeHTTPResponse:
            nonlocal recorded_req
            recorded_req = req
            return FakeHTTPResponse(status=204, body=b"")

        monkeypatch.setattr(client._opener, "open", fake_open)

        res = client.close_session(valid_id)
        assert res is None
        assert recorded_req is not None
        assert recorded_req.get_method() == "DELETE"
        assert recorded_req.full_url == f"https://hub.example.com/api/usb-presence/session/{valid_id}"
        assert recorded_req.headers.get("Authorization") == "Bearer valid-token"
        assert recorded_req.data is None

    def test_close_session_invalid_session_id(self) -> None:
        """Закрытие сессии сразу отклоняет некорректный session_id."""
        client = USBPresenceClient("https://hub.example.com", "valid-token")
        with pytest.raises(USBPresenceClientError):
            client.close_session("bad_id")


# --- Тесты различия 409, 401 и сетевых ошибок ---

class TestUSBPresenceErrorHandling:
    """Тестирование четкого разграничения классов исключений и скрытия секретов."""

    def test_http_409_raises_conflict_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP 409 вызывает USBPresenceConflictError (отдельное исключение от 401 и сети)."""
        client = USBPresenceClient("https://hub.example.com", "test-token")
        valid_id = "c9bf9e571685472eb136c26731234567"

        def fake_open(req: urllib.request.Request, timeout: float = 5.0) -> Any:
            raise urllib.error.HTTPError(
                req.full_url,
                409,
                "Conflict",
                {"Content-Type": "application/json"},
                io.BytesIO(b'{"detail": "Session already occupied by another client"}'),
            )

        monkeypatch.setattr(client._opener, "open", fake_open)

        with pytest.raises(USBPresenceConflictError) as exc_info:
            client.publish_snapshot(valid_id, ["dev1"])

        exc = exc_info.value
        # Проверяем строгое различие классов
        assert isinstance(exc, USBPresenceConflictError)
        assert isinstance(exc, USBPresenceSessionConflictError)
        assert isinstance(exc, USBPresenceClientError)
        assert isinstance(exc, HubClientError)
        assert not isinstance(exc, USBPresenceAuthError)
        assert not isinstance(exc, USBPresenceNetworkError)

        # Проверяем, что тело ответа сервера и URL НЕ раскрываются
        assert "Session already occupied" not in str(exc)
        assert "https://" not in str(exc)
        assert "hub.example.com" not in str(exc)
        assert "test-token" not in str(exc)

    def test_http_401_raises_auth_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP 401 вызывает USBPresenceAuthError."""
        client = USBPresenceClient("https://hub.example.com", "test-token")

        def fake_open(req: urllib.request.Request, timeout: float = 5.0) -> Any:
            raise urllib.error.HTTPError(
                req.full_url,
                401,
                "Unauthorized",
                {},
                io.BytesIO(b'{"secret_server_info": "token invalid"}'),
            )

        monkeypatch.setattr(client._opener, "open", fake_open)

        with pytest.raises(USBPresenceAuthError) as exc_info:
            client.open_session()

        exc = exc_info.value
        assert isinstance(exc, USBPresenceAuthError)
        assert not isinstance(exc, USBPresenceConflictError)
        assert not isinstance(exc, USBPresenceNetworkError)
        assert "secret_server_info" not in str(exc)
        assert "https://" not in str(exc)

    def test_network_timeout_raises_network_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Таймаут соединения вызывает USBPresenceNetworkError."""
        client = USBPresenceClient("https://hub.example.com", "test-token", timeout=3.0)

        def fake_open(req: urllib.request.Request, timeout: float = 3.0) -> Any:
            raise socket.timeout("timed out")

        monkeypatch.setattr(client._opener, "open", fake_open)

        with pytest.raises(USBPresenceNetworkError) as exc_info:
            client.open_session()

        exc = exc_info.value
        assert isinstance(exc, USBPresenceNetworkError)
        assert not isinstance(exc, USBPresenceConflictError)
        assert not isinstance(exc, USBPresenceAuthError)
        assert "3.0" in str(exc) or "время ожидания" in str(exc)
        assert "https://" not in str(exc)

    def test_connection_refused_raises_network_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Отказ в соединении вызывает USBPresenceNetworkError."""
        client = USBPresenceClient("https://hub.example.com", "test-token")

        def fake_open(req: urllib.request.Request, timeout: float = 5.0) -> Any:
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused to https://hub.example.com"))

        monkeypatch.setattr(client._opener, "open", fake_open)

        with pytest.raises(USBPresenceNetworkError) as exc_info:
            client.open_session()

        exc = exc_info.value
        assert isinstance(exc, USBPresenceNetworkError)
        assert not isinstance(exc, USBPresenceConflictError)
        assert not isinstance(exc, USBPresenceAuthError)
        assert "https://" not in str(exc)
        assert "hub.example.com" not in str(exc)

    def test_ssl_error_raises_network_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ошибка SSL-сертификата вызывает USBPresenceNetworkError."""
        client = USBPresenceClient("https://hub.example.com", "test-token")

        def fake_open(req: urllib.request.Request, timeout: float = 5.0) -> Any:
            raise urllib.error.URLError(ssl.SSLError("CERTIFICATE_VERIFY_FAILED at https://hub.example.com"))

        monkeypatch.setattr(client._opener, "open", fake_open)

        with pytest.raises(USBPresenceNetworkError) as exc_info:
            client.open_session()

        exc = exc_info.value
        assert isinstance(exc, USBPresenceNetworkError)
        assert "https://" not in str(exc)

    def test_http_404_raises_not_found_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP 404 вызывает USBPresenceNotFoundError."""
        client = USBPresenceClient("https://hub.example.com", "test-token")
        valid_id = "c9bf9e571685472eb136c26731234567"

        def fake_open(req: urllib.request.Request, timeout: float = 5.0) -> Any:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b""))

        monkeypatch.setattr(client._opener, "open", fake_open)

        with pytest.raises(USBPresenceNotFoundError) as exc_info:
            client.close_session(valid_id)

        assert isinstance(exc_info.value, USBPresenceNotFoundError)
        assert isinstance(exc_info.value, USBPresenceClientError)
        assert not isinstance(exc_info.value, USBPresenceConflictError)

    def test_redirect_forbidden(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Запрет перенаправлений для предотвращения утечки токена."""
        client = USBPresenceClient("https://hub.example.com", "secret-token")

        def fake_open(req: urllib.request.Request, timeout: float = 5.0) -> FakeHTTPResponse:
            return FakeHTTPResponse(
                status=302,
                body=b"",
                headers={"Location": "https://attacker.com/evil"},
            )

        monkeypatch.setattr(client._opener, "open", fake_open)

        with pytest.raises(USBPresenceClientError) as exc_info:
            client.open_session()

        assert "Редиректы запрещены" in str(exc_info.value)
        assert "secret-token" not in str(exc_info.value)
        assert "attacker.com" not in str(exc_info.value)

    def test_response_size_limit_content_length(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Защита от превышения лимита размера ответа через заголовок Content-Length."""
        client = USBPresenceClient("https://hub.example.com", "token", max_response_bytes=100)

        def fake_open(req: urllib.request.Request, timeout: float = 5.0) -> FakeHTTPResponse:
            return FakeHTTPResponse(
                status=200,
                body=b"{}",
                headers={"Content-Length": "1000"},
            )

        monkeypatch.setattr(client._opener, "open", fake_open)

        with pytest.raises(USBPresenceClientError) as exc_info:
            client.open_session()
        assert "превышает допустимый лимит" in str(exc_info.value)

    def test_response_size_limit_stream_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Защита от превышения лимита при чтении потока данных."""
        client = USBPresenceClient("https://hub.example.com", "token", max_response_bytes=50)

        def fake_open(req: urllib.request.Request, timeout: float = 5.0) -> FakeHTTPResponse:
            return FakeHTTPResponse(
                status=200,
                body=b"x" * 200,
            )

        monkeypatch.setattr(client._opener, "open", fake_open)

        with pytest.raises(USBPresenceClientError) as exc_info:
            client.open_session()
        assert "превышает допустимый лимит" in str(exc_info.value)


# --- Интеграционные тесты с локальным реальным HTTPS сервером (TLS) ---

class TestUSBPresenceClientTLS:
    """Тестирование реального HTTPS-транспорта с локальным TLS-сервером."""

    def test_full_session_lifecycle_over_tls(
        self, fake_https_server: tuple[str, int, ssl.SSLContext]
    ) -> None:
        """Полный цикл сессии: open_session -> publish_snapshot -> close_session по HTTPS."""
        base_url, port, client_ctx = fake_https_server
        token = "valid-secret-token"

        client = USBPresenceClient(base_url, token, ssl_context=client_ctx)
        expected_session_id = "c9bf9e571685472eb136c26731234567"

        # 1. Открытие сессии
        FakeTLSServerHandler.response_status = 200
        FakeTLSServerHandler.response_body = json.dumps(
            {"session_id": expected_session_id}
        ).encode("utf-8")

        session_id = client.open_session()
        assert session_id == expected_session_id
        assert FakeTLSServerHandler.last_method == "POST"
        assert FakeTLSServerHandler.last_path == "/api/usb-presence/session"
        assert FakeTLSServerHandler.last_headers.get("Authorization") == f"Bearer {token}"

        # 2. Публикация снимка устройств
        FakeTLSServerHandler.response_status = 204
        FakeTLSServerHandler.response_body = b""

        devices = ["device-alpha", "device-beta"]
        client.publish_snapshot(session_id, devices)
        assert FakeTLSServerHandler.last_method == "PUT"
        assert FakeTLSServerHandler.last_path == f"/api/usb-presence/session/{expected_session_id}"
        assert json.loads(FakeTLSServerHandler.last_body.decode("utf-8")) == {"device_ids": devices}

        # 3. Закрытие сессии
        FakeTLSServerHandler.response_status = 204
        FakeTLSServerHandler.response_body = b""

        client.close_session(session_id)
        assert FakeTLSServerHandler.last_method == "DELETE"
        assert FakeTLSServerHandler.last_path == f"/api/usb-presence/session/{expected_session_id}"

    def test_tls_untrusted_certificate_rejected(
        self, fake_https_server: tuple[str, int, ssl.SSLContext]
    ) -> None:
        """Клиент со стандартным контекстом (не доверяющим самоподписанному сертификату) падает с сетевой ошибкой."""
        base_url, port, _ = fake_https_server
        # Используем default context без добавления самоподписанного сертификата
        untrusted_ctx = ssl.create_default_context()

        client = USBPresenceClient(base_url, "valid-secret-token", ssl_context=untrusted_ctx)
        with pytest.raises(USBPresenceNetworkError) as exc_info:
            client.open_session()

        assert "SSL" in str(exc_info.value)
        assert "https://" not in str(exc_info.value)

    def test_tls_401_unauthorized(
        self, fake_https_server: tuple[str, int, ssl.SSLContext]
    ) -> None:
        """Передача неверного токена серверу возвращает 401 и транслируется в USBPresenceAuthError."""
        base_url, port, client_ctx = fake_https_server
        bad_token = "wrong-token"

        client = USBPresenceClient(base_url, bad_token, ssl_context=client_ctx)
        with pytest.raises(USBPresenceAuthError) as exc_info:
            client.open_session()

        assert not isinstance(exc_info.value, USBPresenceConflictError)
        assert not isinstance(exc_info.value, USBPresenceNetworkError)
        assert bad_token not in str(exc_info.value)

    def test_tls_409_conflict(
        self, fake_https_server: tuple[str, int, ssl.SSLContext]
    ) -> None:
        """Сервер возвращает 409 при коллизии сессии, что транслируется в USBPresenceConflictError."""
        base_url, port, client_ctx = fake_https_server
        token = "valid-secret-token"

        FakeTLSServerHandler.response_status = 409
        FakeTLSServerHandler.response_body = b'{"error": "Session conflict"}'

        client = USBPresenceClient(base_url, token, ssl_context=client_ctx)
        valid_id = "c9bf9e571685472eb136c26731234567"

        with pytest.raises(USBPresenceConflictError) as exc_info:
            client.publish_snapshot(valid_id, ["device-1"])

        assert not isinstance(exc_info.value, USBPresenceAuthError)
        assert not isinstance(exc_info.value, USBPresenceNetworkError)
        assert "Session conflict" not in str(exc_info.value)
        assert token not in str(exc_info.value)
