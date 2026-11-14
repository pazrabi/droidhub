"""Синхронный стандартно-библиотечный HTTPS-клиент обмена статусами USB-присутствия."""

from __future__ import annotations

from collections.abc import Sequence
import json
import re
import socket
import ssl
from typing import Any, NoReturn
import urllib.error
import urllib.parse
import urllib.request

from desktop.hub_client import DEFAULT_MAX_RESPONSE_BYTES, HubClient, HubClientError

DEFAULT_TIMEOUT: float = 5.0

_SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class USBPresenceClientError(HubClientError):
    """Базовое исключение для ошибок клиента USB-присутствия."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


class USBPresenceAuthError(USBPresenceClientError):
    """Ошибка аутентификации или авторизации (HTTP 401)."""


class USBPresenceConflictError(USBPresenceClientError):
    """Конфликт сессии USB-присутствия (HTTP 409)."""


# Синоним для совместимости
USBPresenceSessionConflictError = USBPresenceConflictError


class USBPresenceNotFoundError(USBPresenceClientError):
    """Сессия или эндпоинт USB-присутствия не найден (HTTP 404)."""


class USBPresenceNetworkError(USBPresenceClientError):
    """Сетевая ошибка при обращении к серверу (таймаут, отказ в соединении, SSL, DNS)."""


def _validate_session_id(session_id: Any) -> str:
    """Проверяет корректность идентификатора сессии.

    Разрешён только непрозрачный lowercase hex из 32 символов (формат uuid4.hex).
    """
    if not isinstance(session_id, str):
        raise USBPresenceClientError(
            f"Идентификатор сессии должен быть строкой, получен {type(session_id).__name__}"
        )
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise USBPresenceClientError(
            "Идентификатор сессии должен состоять ровно из 32 шестнадцатеричных символов в нижнем регистре (hex)"
        )
    return session_id


class USBPresenceClient:
    """Синхронный HTTPS-клиент для обмена статусами USB-присутствия с сервером DroidHub."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        ssl_context: ssl.SSLContext | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise USBPresenceClientError("Базовый URL должен быть непустой строкой")

        # Строгая проверка протокола HTTPS
        parsed_raw = urllib.parse.urlsplit(base_url.strip())
        scheme = (parsed_raw.scheme or "").lower()
        if scheme != "https":
            raise USBPresenceClientError(
                f"Недопустимый протокол '{parsed_raw.scheme}'. Разрешён строго протокол HTTPS"
            )

        # Переиспользуем валидацию базового URL, токена и таймаута из HubClient
        try:
            hub = HubClient(
                base_url=base_url,
                token=token,
                timeout=timeout,
                ssl_context=ssl_context,
                max_response_bytes=max_response_bytes,
            )
        except HubClientError as exc:
            raise USBPresenceClientError(str(exc)) from None

        self._base_url = hub.base_url
        self._token = token.strip()
        self._timeout = hub.timeout
        self._ssl_context = ssl_context
        self._max_response_bytes = int(max_response_bytes)

        # Вычисляем полный URL эндпоинта сессии с учетом deployment prefix
        self._session_url = self._build_session_url(hub.devices_url, base_url.strip())

        # Переиспользуем настроенный HTTPS opener HubClient (без редиректов и с нужным SSLContext)
        self._opener = hub._opener

    @staticmethod
    def _build_session_url(devices_url: str, raw_base_url: str) -> str:
        """Формирует URL эндпоинта /api/usb-presence/session с учетом префиксов."""
        parsed_raw = urllib.parse.urlsplit(raw_base_url)
        raw_path = parsed_raw.path.rstrip("/")
        if raw_path.endswith("/api/usb-presence/session"):
            return urllib.parse.urlunsplit(
                (parsed_raw.scheme, parsed_raw.netloc, raw_path, "", "")
            )
        if raw_path.endswith("/api/usb-presence"):
            return urllib.parse.urlunsplit(
                (parsed_raw.scheme, parsed_raw.netloc, f"{raw_path}/session", "", "")
            )

        parsed_dev = urllib.parse.urlsplit(devices_url)
        dev_path = parsed_dev.path
        if dev_path.endswith("/api/devices"):
            prefix = dev_path[:-len("/api/devices")]
            session_path = f"{prefix}/api/usb-presence/session"
        else:
            session_path = f"{dev_path}/api/usb-presence/session"

        return urllib.parse.urlunsplit(
            (parsed_dev.scheme, parsed_dev.netloc, session_path, "", "")
        )

    @property
    def base_url(self) -> str:
        """Нормализованный базовый URL сервера."""
        return self._base_url

    @property
    def session_url(self) -> str:
        """Полный URL эндпоинта /api/usb-presence/session."""
        return self._session_url

    @property
    def timeout(self) -> float:
        """Таймаут запросов в секундах."""
        return self._timeout

    @property
    def max_response_bytes(self) -> int:
        """Максимально допустимый размер ответа сервера в байтах."""
        return self._max_response_bytes

    def _sanitize(self, message: str) -> str:
        """Маскирует секретный токен в строке сообщения об ошибке."""
        if self._token and self._token in message:
            return message.replace(self._token, "***")
        return message

    def __repr__(self) -> str:
        return f"USBPresenceClient(base_url={self._base_url!r}, token='***', timeout={self._timeout})"

    def __str__(self) -> str:
        return f"USBPresenceClient(base_url={self._base_url!r}, token='***')"

    def __enter__(self) -> USBPresenceClient:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """Закрывает клиент (резервный метод для контекстных менеджеров)."""
        pass

    def _request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        """Выполняет защищенный HTTPS-запрос без редиректов и с контролем размера."""
        req_headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "User-Agent": "DroidHub-Desktop/1.0",
        }
        if body is not None:
            req_headers["Content-Type"] = "application/json"
        if headers:
            req_headers.update(headers)

        req = urllib.request.Request(
            url,
            data=body,
            headers=req_headers,
            method=method,
        )

        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                status_code = getattr(resp, "status", getattr(resp, "code", 200))
                if 300 <= status_code < 400 and status_code != 304:
                    raise USBPresenceClientError(
                        f"Редиректы запрещены во избежание утечки токена авторизации (HTTP {status_code})"
                    )

                headers_obj = getattr(resp, "headers", None)
                content_length = None
                if headers_obj is not None and hasattr(headers_obj, "get"):
                    content_length = headers_obj.get("Content-Length")

                if content_length:
                    try:
                        cl_val = int(content_length)
                        if cl_val > self._max_response_bytes:
                            raise USBPresenceClientError(
                                f"Размер ответа сервера ({cl_val} байт) превышает допустимый лимит "
                                f"({self._max_response_bytes} байт)"
                            )
                    except ValueError:
                        pass

                raw_bytes = resp.read(self._max_response_bytes + 1)
                if len(raw_bytes) > self._max_response_bytes:
                    raise USBPresenceClientError(
                        f"Размер ответа сервера превышает допустимый лимит ({self._max_response_bytes} байт)"
                    )

                resp_headers: dict[str, str] = {}
                if headers_obj is not None and hasattr(headers_obj, "items"):
                    resp_headers = {str(k).lower(): str(v) for k, v in headers_obj.items()}
                return status_code, raw_bytes, resp_headers

        except USBPresenceClientError:
            raise
        except HubClientError as exc:
            # Срабатывание _DisallowRedirectHandler или внутренней ошибки HubClient
            raise USBPresenceClientError(self._sanitize(str(exc))) from None
        except urllib.error.HTTPError as exc:
            self._handle_http_error(exc)
        except urllib.error.URLError as exc:
            self._handle_url_error(exc)
        except (socket.timeout, TimeoutError):
            raise USBPresenceNetworkError(
                f"Превышено время ожидания ответа сервера ({self._timeout} с)"
            ) from None
        except Exception as exc:
            raise USBPresenceClientError(
                f"Непредвиденная ошибка при обращении к серверу: {type(exc).__name__}"
            ) from None

    def _handle_http_error(self, exc: urllib.error.HTTPError) -> NoReturn:
        """Обрабатывает ошибки протокола HTTP, не допуская утечки тела ответа или URL."""
        code = exc.code
        if code == 401:
            raise USBPresenceAuthError(
                "Ошибка авторизации: неверный или недействительный токен (HTTP 401)"
            ) from None
        if code == 409:
            raise USBPresenceConflictError(
                "Конфликт сессии USB-присутствия: сессия устарела или уже занята (HTTP 409)"
            ) from None
        if code == 403:
            raise USBPresenceClientError(
                "Доступ запрещён: недостаточно прав для выполнения запроса (HTTP 403)"
            ) from None
        if code == 404:
            raise USBPresenceNotFoundError(
                "Эндпоинт или сессия USB-присутствия не найдена (HTTP 404)"
            ) from None
        if code == 429:
            raise USBPresenceClientError(
                "Превышен лимит запросов к серверу DroidHub (HTTP 429)"
            ) from None
        if code == 500:
            raise USBPresenceClientError(
                "Внутренняя ошибка сервера DroidHub (HTTP 500)"
            ) from None
        if code == 502:
            raise USBPresenceClientError(
                "Ошибка шлюза сервера DroidHub (HTTP 502)"
            ) from None
        if code == 503:
            raise USBPresenceClientError(
                "Сервер DroidHub временно недоступен (HTTP 503)"
            ) from None
        if code >= 500:
            raise USBPresenceClientError(
                f"Внутренняя ошибка сервера DroidHub (HTTP {code})"
            ) from None
        raise USBPresenceClientError(f"Ошибка сервера: HTTP {code}") from None

    def _handle_url_error(self, exc: urllib.error.URLError) -> NoReturn:
        """Обрабатывает ошибки сетевого уровня URLError, не допуская утечки URL."""
        reason = exc.reason
        if isinstance(reason, ssl.SSLError):
            raise USBPresenceNetworkError(
                f"Ошибка проверки SSL-сертификата сервера: {type(reason).__name__}"
            ) from None
        if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason).lower():
            raise USBPresenceNetworkError(
                f"Превышено время ожидания ответа сервера ({self._timeout} с)"
            ) from None
        if isinstance(reason, ConnectionRefusedError) or "connection refused" in str(reason).lower():
            raise USBPresenceNetworkError(
                "Не удалось подключиться к серверу DroidHub: соединение отклонено"
            ) from None
        if isinstance(reason, socket.gaierror):
            raise USBPresenceNetworkError(
                "Ошибка разрешения доменного имени сервера"
            ) from None
        raise USBPresenceNetworkError(
            "Сетевая ошибка при обращении к серверу DroidHub"
        ) from None

    def open_session(self) -> str:
        """Открывает новую сессию USB-присутствия (POST /api/usb-presence/session).

        Возвращает непрозрачный 32-символьный hex-идентификатор сессии.
        """
        status_code, raw_bytes, _ = self._request("POST", self._session_url, body=None)
        if status_code not in (200, 201):
            raise USBPresenceClientError(
                f"Неожиданный статус ответа при открытии сессии: HTTP {status_code}"
            )

        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise USBPresenceClientError("Ошибка декодирования ответа сервера как UTF-8") from None

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise USBPresenceClientError("Некорректный JSON в ответе сервера") from None

        if not isinstance(data, dict):
            raise USBPresenceClientError("Ожидался JSON-объект в ответе сервера")

        session_id = data.get("session_id")
        if session_id is None:
            raise USBPresenceClientError("В ответе сервера отсутствует обязательное поле 'session_id'")

        return _validate_session_id(session_id)

    def publish_snapshot(self, session_id: str, device_ids: Sequence[str]) -> None:
        """Публикует снимок подключенных USB-устройств (PUT /api/usb-presence/session/{session_id}).

        Ожидает код ответа 204 No Content.
        """
        valid_session_id = _validate_session_id(session_id)
        if isinstance(device_ids, (str, bytes)) or not isinstance(device_ids, Sequence):
            raise USBPresenceClientError("Параметр device_ids должен быть последовательностью строк")

        clean_ids: list[str] = []
        for idx, item in enumerate(device_ids):
            if not isinstance(item, str):
                raise USBPresenceClientError(
                    f"Идентификатор устройства #{idx} должен быть строкой, получен {type(item).__name__}"
                )
            stripped = item.strip()
            if not stripped:
                raise USBPresenceClientError(f"Идентификатор устройства #{idx} не может быть пустым")
            clean_ids.append(stripped)

        payload = json.dumps({"device_ids": clean_ids}).encode("utf-8")
        url = f"{self._session_url}/{valid_session_id}"
        status_code, _, _ = self._request("PUT", url, body=payload)
        if status_code not in (200, 204):
            raise USBPresenceClientError(
                f"Неожиданный статус ответа при публикации снимка: HTTP {status_code}"
            )

    def close_session(self, session_id: str) -> None:
        """Закрывает сессию USB-присутствия (DELETE /api/usb-presence/session/{session_id}).

        Ожидает код ответа 204 No Content.
        """
        valid_session_id = _validate_session_id(session_id)
        url = f"{self._session_url}/{valid_session_id}"
        status_code, _, _ = self._request("DELETE", url, body=None)
        if status_code not in (200, 204):
            raise USBPresenceClientError(
                f"Неожиданный статус ответа при закрытии сессии: HTTP {status_code}"
            )


__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_TIMEOUT",
    "USBPresenceAuthError",
    "USBPresenceClient",
    "USBPresenceClientError",
    "USBPresenceConflictError",
    "USBPresenceNetworkError",
    "USBPresenceNotFoundError",
    "USBPresenceSessionConflictError",
]
