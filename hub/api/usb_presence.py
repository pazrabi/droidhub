"""
FastAPI-роутер для HTTPS-обмена статусами USB-присутствия устройств с локальными мостами DroidHub.

Обеспечивает защищённый точечный API для настольных USB-мостов:
- Авторизация моста исключительно по уникальному выделенному Bearer-токену в constant-time без раннего выхода.
- Строгая изоляция: нет доступа через обычные API/MCP токены или сессионные cookie.
- Валидация снимков разрешённых устройств целиком до изменения реестра.
- Лимит на размер тела запроса (64 КиБ) с обработкой потока без Content-Length (413).
"""
from __future__ import annotations

import json
import logging
import secrets
from typing import Mapping

from fastapi import APIRouter, HTTPException, Request, Response, status

from hub.devices.usb_presence import USBPresenceRegistry

logger = logging.getLogger(__name__)

__all__ = ["build_usb_presence_router"]

HTTP_204_NO_CONTENT: int = 204
HTTP_401_UNAUTHORIZED: int = 401
HTTP_409_CONFLICT: int = 409
HTTP_413_CONTENT_TOO_LARGE: int = 413
HTTP_422_UNPROCESSABLE_ENTITY: int = 422
MAX_BODY_BYTES: int = 64 * 1024  # 64 КиБ = 65536 байт
MAX_DEVICE_IDS_COUNT: int = 256  # разумный максимум устройств на один мост
MAX_DEVICE_ID_LENGTH: int = 128  # разумный максимум длины серийного номера / ID
MAX_SESSION_ID_LENGTH: int = 128  # разумный максимум длины ID сессии


async def _read_limited_body(request: Request, max_size: int = MAX_BODY_BYTES) -> bytes:
    """
    Считывает тело запроса с ограничением размера до max_size байт.

    Проверяет как заголовок Content-Length, так и фактический поток чанков (chunked transfer),
    вызывая HTTPException 413 при превышении допустимого размера.
    """
    cl_header = request.headers.get("content-length")
    if cl_header is not None:
        try:
            cl = int(cl_header)
            if cl > max_size:
                raise HTTPException(
                    status_code=HTTP_413_CONTENT_TOO_LARGE,
                    detail="Размер тела запроса превышает допустимый предел 64 КиБ",
                )
        except ValueError:
            raise HTTPException(
                status_code=HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Некорректный заголовок Content-Length",
            )

    chunks: list[bytes] = []
    total_size = 0
    async for chunk in request.stream():
        total_size += len(chunk)
        if total_size > max_size:
            raise HTTPException(
                status_code=HTTP_413_CONTENT_TOO_LARGE,
                detail="Размер тела запроса превышает допустимый предел 64 КиБ",
            )
        chunks.append(chunk)

    return b"".join(chunks)


def _authenticate_bridge(request: Request, bridge_tokens: Mapping[str, str]) -> str:
    """
    Аутентифицирует локальный USB-мост по выделенному Bearer-токену.

    Bridge ID выводится исключительно из сопоставленного Bearer-токена.
    Пустая конфигурация токенов не допускает ни одного обращения.
    Сравнение всех токенов выполняется в constant-time без раннего выхода (no early exit).
    Секреты никогда не логируются.
    """
    if not bridge_tokens:
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Конфигурация токенов мостов пуста или не настроена",
            headers={"WWW-Authenticate": "Bearer"},
        )

    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Требуется заголовок Authorization с Bearer-токеном",
            headers={"WWW-Authenticate": "Bearer"},
        )

    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Требуется схема авторизации Bearer",
            headers={"WWW-Authenticate": "Bearer"},
        )

    incoming_token = token.strip()
    matched_bridge_id: str | None = None

    # Постоянное время сравнения без раннего выхода для защиты от атак по времени
    for b_id, b_token in bridge_tokens.items():
        if not isinstance(b_token, str) or not b_token or not isinstance(b_id, str) or not b_id:
            continue
        if secrets.compare_digest(b_token, incoming_token):
            if matched_bridge_id is None:
                matched_bridge_id = b_id

    if matched_bridge_id is None:
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Недействительный токен авторизации моста",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return matched_bridge_id


def _validate_session_id(session_id: str) -> None:
    """Проверяет корректность идентификатора сессии."""
    if (
        not isinstance(session_id, str)
        or not session_id
        or len(session_id) > MAX_SESSION_ID_LENGTH
    ):
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Длина session_id должна быть от 1 до {MAX_SESSION_ID_LENGTH} символов",
        )
    if not all(c.isalnum() or c in "-_" for c in session_id):
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Идентификатор сессии содержит недопустимые символы",
        )


def build_usb_presence_router(
    registry: USBPresenceRegistry,
    bridge_tokens: Mapping[str, str],
) -> APIRouter:
    """
    Фабрика FastAPI APIRouter для защищённого HTTPS-обмена USB-статусами.

    :param registry: Текущий реестр USBPresenceRegistry.
    :param bridge_tokens: Отображение bridge_id -> выделенный уникальный bearer-токен.
    :return: Сконфигурированный APIRouter с префиксом /api/usb-presence.
    """
    router = APIRouter(prefix="/api/usb-presence", tags=["usb-presence"])

    @router.post("/session")
    async def open_session(request: Request) -> dict[str, str]:
        """
        Открывает новую сессию для авторизованного USB-моста.

        Тело запроса не требуется (игнорируется, если передано, без чтения пользовательских ID).
        Вызывает registry.open_session(bridge_id) только с вычисленным bridge_id.
        """
        bridge_id = _authenticate_bridge(request, bridge_tokens)
        await _read_limited_body(request)
        session_id = registry.open_session(bridge_id)
        logger.info("Открыта новая USB-сессия для моста '%s'", bridge_id)
        return {"session_id": session_id}

    @router.put("/session/{session_id}", status_code=HTTP_204_NO_CONTENT)
    async def update_session(session_id: str, request: Request) -> Response:
        """
        Атомарно обновляет полный снимок USB-устройств для указанной сессии.

        Проверяет разрешённые ID устройств целиком до внесения изменений в реестр.
        Возвращает:
        - 204 при успешном обновлении или очистке.
        - 422 при неизвестных или некорректных данных.
        - 409 при устаревшей сессии или конфликте привязки с другой сессией.
        """
        bridge_id = _authenticate_bridge(request, bridge_tokens)
        body_bytes = await _read_limited_body(request)
        _validate_session_id(session_id)

        if not body_bytes:
            raise HTTPException(
                status_code=HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Тело запроса отсутствует",
            )

        try:
            payload = json.loads(body_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(
                status_code=HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Некорректный JSON в теле запроса",
            )

        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Тело запроса должно быть JSON-объектом",
            )

        if "device_ids" not in payload:
            raise HTTPException(
                status_code=HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Поле 'device_ids' обязательно",
            )

        if set(payload.keys()) - {"device_ids"}:
            raise HTTPException(
                status_code=HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Неизвестные дополнительные поля в теле запроса",
            )

        raw_device_ids = payload["device_ids"]
        if not isinstance(raw_device_ids, list):
            raise HTTPException(
                status_code=HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Поле 'device_ids' должно быть списком",
            )

        if len(raw_device_ids) > MAX_DEVICE_IDS_COUNT:
            raise HTTPException(
                status_code=HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Количество устройств превышает предел ({MAX_DEVICE_IDS_COUNT})",
            )

        validated_ids: list[str] = []
        seen_ids: set[str] = set()

        for dev_id in raw_device_ids:
            if not isinstance(dev_id, str):
                raise HTTPException(
                    status_code=HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Идентификатор устройства должен быть строкой",
                )
            if not dev_id or len(dev_id) > MAX_DEVICE_ID_LENGTH:
                raise HTTPException(
                    status_code=HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Длина ID устройства должна быть от 1 до {MAX_DEVICE_ID_LENGTH} символов",
                )
            if any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in dev_id):
                raise HTTPException(
                    status_code=HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Недопустимые символы в идентификаторе устройства: '{dev_id}'",
                )
            if dev_id in seen_ids:
                raise HTTPException(
                    status_code=HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Дублирующийся идентификатор устройства: '{dev_id}'",
                )
            seen_ids.add(dev_id)
            validated_ids.append(dev_id)

        # Проверяем все разрешённые ID целиком до изменения реестра
        for dev_id in validated_ids:
            if not registry.is_allowed(dev_id):
                raise HTTPException(
                    status_code=HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Неизвестное или неразрешённое устройство: '{dev_id}'",
                )

        # Применяем снимок в реестре (атомарно)
        applied = registry.apply_snapshot(bridge_id, session_id, validated_ids)
        if not applied:
            raise HTTPException(
                status_code=HTTP_409_CONFLICT,
                detail="Конфликт сессии моста или устройство уже занято другой сессией",
            )

        logger.info(
            "Обновлён снимок USB-устройств для моста '%s', сессия '%s' (устройств: %d)",
            bridge_id,
            session_id,
            len(validated_ids),
        )
        return Response(status_code=HTTP_204_NO_CONTENT)

    @router.delete("/session/{session_id}", status_code=HTTP_204_NO_CONTENT)
    async def delete_session(session_id: str, request: Request) -> Response:
        """
        Закрывает активную сессию моста и освобождает её устройства (идемпотентно).

        Вызывает registry.close_session с вычисленным bridge_id.
        Повторные вызовы для уже закрытой или истекшей сессии возвращают 204.
        """
        bridge_id = _authenticate_bridge(request, bridge_tokens)
        await _read_limited_body(request)
        _validate_session_id(session_id)

        registry.close_session(bridge_id, session_id)
        logger.info("Закрыта USB-сессия '%s' для моста '%s'", session_id, bridge_id)
        return Response(status_code=HTTP_204_NO_CONTENT)

    return router
