"""Автономный read-only HTTP-клиент для удалённого сервера DroidHub."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
import json
import socket
import ssl
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


# Максимальный размер ответа (2 МБ для защиты от переполнения памяти)
DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024

# Локальные хосты, для которых допустим plain HTTP в целях отладки/тестирования
ALLOWED_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class HubClientError(Exception):
    """Базовое исключение для ошибок HTTP-клиента DroidHub."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


class _DisallowRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Блокирует любые перенаправления для исключения утечки Bearer-токена."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None

    def http_error_301(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
    ) -> Any:
        raise HubClientError(
            f"Редиректы запрещены во избежание утечки токена авторизации (HTTP {code})"
        )

    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301


@dataclass
class HubDevice:
    """Информация об удаленном устройстве с сервера DroidHub.

    Поддерживает известные поля (id, name, online, battery, temperature, screen_on, charging)
    и сохраняет любые дополнительные свойства без ошибок.
    """

    id: str
    name: str
    online: bool = False
    battery: int | None = None
    temperature: float | None = None
    screen_on: bool | None = None
    charging: bool | None = None
    last_seen: float | None = None
    error: str | None = None
    busy: bool | None = None
    activity: Any = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __init__(
        self,
        id: str,
        name: str = "",
        online: bool = False,
        battery: int | None = None,
        temperature: float | None = None,
        screen_on: bool | None = None,
        charging: bool | None = None,
        last_seen: float | None = None,
        error: str | None = None,
        busy: bool | None = None,
        activity: Any = None,
        extra: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.id = str(id)
        self.name = str(name) if name else self.id
        self.online = bool(online)

        if battery is not None:
            try:
                self.battery = int(battery)
            except (ValueError, TypeError):
                self.battery = None
        else:
            self.battery = None

        if temperature is not None:
            try:
                self.temperature = float(temperature)
            except (ValueError, TypeError):
                self.temperature = None
        else:
            self.temperature = None

        self.screen_on = bool(screen_on) if screen_on is not None else None
        self.charging = bool(charging) if charging is not None else None

        if last_seen is not None:
            try:
                self.last_seen = float(last_seen)
            except (ValueError, TypeError):
                self.last_seen = None
        else:
            self.last_seen = None

        self.error = str(error) if error is not None else None
        self.busy = bool(busy) if busy is not None else None
        self.activity = activity

        self.extra = dict(extra or {})
        if kwargs:
            self.extra.update(kwargs)

    def __getattr__(self, name: str) -> Any:
        if "extra" in self.__dict__ and name in self.extra:
            return self.extra[name]
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    @property
    def display_name(self) -> str:
        """Понятное имя устройства для интерфейса."""
        return self.name or self.id

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HubDevice:
        """Создает HubDevice из словаря ответа API."""
        if not isinstance(data, dict):
            raise HubClientError(
                f"Некорректный элемент в ответе сервера: ожидался словарь, получен {type(data).__name__}"
            )
        if "id" not in data or not data["id"]:
            raise HubClientError("В элементе списка устройств отсутствует обязательное поле 'id'")

        return cls(**data)


class HubClient:
    """Автономный read-only HTTP-клиент для DroidHub (/api/devices)."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 5.0,
        *,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if not isinstance(token, str) or not token.strip():
            raise HubClientError("Токен авторизации должен быть непустой строкой")
        if any(c in token for c in ("\r", "\n")):
            raise HubClientError("Токен авторизации содержит недопустимые символы перевода строки")
        self._token = token.strip()

        try:
            self._timeout = float(timeout)
        except (ValueError, TypeError) as exc:
            raise HubClientError(f"Некорректное значение таймаута: {timeout}") from exc
        if self._timeout <= 0:
            raise HubClientError("Таймаут должен быть положительным числом")

        self._max_response_bytes = int(max_response_bytes)
        self._ssl_context = ssl_context
        self._base_url = self._validate_and_normalize_url(base_url)
        self._devices_url = self._build_devices_url(self._base_url)
        self._opener = self._create_opener()

    @property
    def base_url(self) -> str:
        """Нормализованный базовый URL сервера."""
        return self._base_url

    @property
    def timeout(self) -> float:
        """Таймаут запросов в секундах."""
        return self._timeout

    @property
    def devices_url(self) -> str:
        """Полный URL эндпоинта /api/devices."""
        return self._devices_url

    def _sanitize(self, message: str) -> str:
        """Маскирует секретный токен в строке сообщения об ошибке."""
        if self._token and self._token in message:
            return message.replace(self._token, "***")
        return message

    def __repr__(self) -> str:
        return f"HubClient(base_url={self._base_url!r}, token='***', timeout={self._timeout})"

    def __str__(self) -> str:
        return f"HubClient(base_url={self._base_url!r}, token='***')"

    def __enter__(self) -> HubClient:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """Закрывает клиент (резервный метод для контекстных менеджеров)."""
        pass

    def _validate_and_normalize_url(self, raw_url: str) -> str:
        if not isinstance(raw_url, str) or not raw_url.strip():
            raise HubClientError("Базовый URL должен быть непустой строкой")

        url = raw_url.strip()
        parsed = urllib.parse.urlsplit(url)

        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https"):
            raise HubClientError(
                f"Недопустимый протокол '{parsed.scheme}'. Разрешены только HTTPS и HTTP (для localhost)"
            )

        if parsed.username is not None or parsed.password is not None:
            raise HubClientError("Учётные данные в URL запрещены во избежание утечки")

        if parsed.query:
            raise HubClientError("Параметры запроса (query) в base_url запрещены")

        if parsed.fragment:
            raise HubClientError("Фрагменты (fragment) в base_url запрещены")

        hostname = (parsed.hostname or "").lower().strip("[]")
        if not hostname:
            raise HubClientError("В base_url не указан хост")

        if scheme == "http" and hostname not in ALLOWED_HTTP_HOSTS:
            raise HubClientError(
                f"Протокол HTTP разрешён только для localhost, 127.0.0.1 и ::1 (указан '{hostname}'). "
                f"Для внешних серверов требуется HTTPS"
            )

        return url

    def _build_devices_url(self, base_url: str) -> str:
        parsed = urllib.parse.urlsplit(base_url)
        path = parsed.path.rstrip("/")
        if path.endswith("/api/devices"):
            final_path = path
        elif path.endswith("/api"):
            final_path = f"{path}/devices"
        else:
            final_path = f"{path}/api/devices" if path else "/api/devices"

        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, final_path, "", ""))

    def _create_opener(self) -> urllib.request.OpenerDirector:
        handlers: list[urllib.request.BaseHandler] = [_DisallowRedirectHandler()]
        parsed = urllib.parse.urlsplit(self._devices_url)
        hostname = (parsed.hostname or "").lower().strip("[]")

        # Для локальных адресов отключаем системные прокси, чтобы не было конфликтов с HTTPS_PROXY
        if hostname in ALLOWED_HTTP_HOSTS:
            handlers.append(urllib.request.ProxyHandler({}))

        if self._ssl_context is not None or parsed.scheme == "https":
            ctx = self._ssl_context or ssl.create_default_context()
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        return urllib.request.build_opener(*handlers)

    def list_devices(self) -> list[HubDevice]:
        """Запрашивает список устройств с сервера DroidHub (GET /api/devices)."""
        req = urllib.request.Request(
            self._devices_url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "User-Agent": "DroidHub-Desktop/1.0",
            },
            method="GET",
        )

        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                status_code = getattr(resp, "status", getattr(resp, "code", 200))
                if 300 <= status_code < 400 and status_code != 304:
                    raise HubClientError(
                        f"Редиректы запрещены во избежание утечки токена авторизации (HTTP {status_code})"
                    )

                content_length = resp.headers.get("Content-Length")
                if content_length:
                    try:
                        cl_val = int(content_length)
                        if cl_val > self._max_response_bytes:
                            raise HubClientError(
                                f"Размер ответа сервера ({cl_val} байт) превышает допустимый лимит "
                                f"({self._max_response_bytes} байт)"
                            )
                    except ValueError:
                        pass

                raw_bytes = resp.read(self._max_response_bytes + 1)
                if len(raw_bytes) > self._max_response_bytes:
                    raise HubClientError(
                        f"Размер ответа сервера превышает допустимый лимит ({self._max_response_bytes} байт)"
                    )

                charset = resp.headers.get_content_charset() or "utf-8"
                try:
                    text = raw_bytes.decode(charset)
                except (LookupError, UnicodeDecodeError) as exc:
                    raise HubClientError(
                        f"Ошибка декодирования ответа сервера как {charset}: {exc}"
                    ) from exc

        except HubClientError:
            raise
        except urllib.error.HTTPError as exc:
            msg = self._handle_http_error(exc)
            raise HubClientError(self._sanitize(msg)) from exc
        except urllib.error.URLError as exc:
            msg = self._handle_url_error(exc)
            raise HubClientError(self._sanitize(msg)) from exc
        except (socket.timeout, TimeoutError) as exc:
            msg = f"Превышено время ожидания ответа сервера ({self._timeout} с)"
            raise HubClientError(self._sanitize(msg)) from exc
        except Exception as exc:
            msg = f"Непредвиденная ошибка при обращении к серверу: {exc}"
            raise HubClientError(self._sanitize(msg)) from exc

        return self._parse_response(text)

    def _handle_http_error(self, exc: urllib.error.HTTPError) -> str:
        if exc.code == 401:
            return "Ошибка авторизации: неверный или недействительный токен (HTTP 401)"
        if exc.code == 403:
            return "Доступ запрещён: недостаточно прав для выполнения запроса (HTTP 403)"
        if exc.code == 404:
            return "Эндпоинт /api/devices не найден на сервере (HTTP 404)"
        if exc.code == 429:
            return "Превышен лимит запросов к серверу DroidHub (HTTP 429)"
        if exc.code == 500:
            return "Внутренняя ошибка сервера DroidHub (HTTP 500)"
        if exc.code == 502:
            return "Ошибка шлюза или сетевого адаптера сервера DroidHub (HTTP 502)"
        if exc.code == 503:
            return "Сервер DroidHub временно недоступен (HTTP 503)"
        return f"Ошибка сервера: HTTP {exc.code} ({exc.reason or 'неизвестная ошибка'})"

    def _handle_url_error(self, exc: urllib.error.URLError) -> str:
        reason = exc.reason
        if isinstance(reason, ssl.SSLError):
            return f"Ошибка проверки SSL-сертификата сервера: {reason}"
        if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason).lower():
            return f"Превышено время ожидания ответа сервера ({self._timeout} с)"
        if isinstance(reason, ConnectionRefusedError) or "connection refused" in str(reason).lower():
            return "Не удалось подключиться к серверу DroidHub: соединение отклонено"
        return f"Сетевая ошибка при обращении к серверу DroidHub: {reason}"

    def _parse_response(self, text: str) -> list[HubDevice]:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise HubClientError(f"Некорректный JSON в ответе сервера: {exc}") from exc

        if not isinstance(data, list):
            raise HubClientError(
                f"Некорректный формат ответа сервера: ожидался список устройств, получен {type(data).__name__}"
            )

        devices: list[HubDevice] = []
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                raise HubClientError(
                    f"Некорректный элемент #{index} в списке устройств: ожидался словарь, получен {type(item).__name__}"
                )
            if "id" not in item or not item["id"]:
                raise HubClientError(
                    f"В элементе #{index} списка устройств отсутствует обязательное поле 'id'"
                )
            devices.append(HubDevice.from_dict(item))

        return devices
