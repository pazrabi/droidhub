"""
Тесты контракта и изоляции FastAPI-роутера USB-присутствия (hub.api.usb_presence).
"""
from __future__ import annotations

import json
import logging
from typing import Iterator

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from hub.api.usb_presence import (
    MAX_BODY_BYTES,
    MAX_DEVICE_ID_LENGTH,
    MAX_DEVICE_IDS_COUNT,
    MAX_SESSION_ID_LENGTH,
    build_usb_presence_router,
)
from hub.devices.usb_presence import USBPresenceRegistry


class FakeClock:
    """Управляемые часы для тестов TTL и истечения сессий."""

    def __init__(self, start: float = 1000.0) -> None:
        self.time = start

    def __call__(self) -> float:
        return self.time

    def advance(self, seconds: float) -> None:
        self.time += seconds


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(start=500.0)


@pytest.fixture
def allowed_devices() -> list[str]:
    return [f"dev_{i}" for i in range(1, 10)] + ["phone1", "phone2", "phone3"]


@pytest.fixture
def registry(allowed_devices: list[str], fake_clock: FakeClock) -> USBPresenceRegistry:
    return USBPresenceRegistry(allowed_devices, ttl=10.0, clock=fake_clock)


@pytest.fixture
def bridge_tokens() -> dict[str, str]:
    return {
        "bridge-alpha": "secret-token-alpha-12345",
        "bridge-beta": "secret-token-beta-67890",
    }


@pytest.fixture
def app(registry: USBPresenceRegistry, bridge_tokens: dict[str, str]) -> FastAPI:
    application = FastAPI(title="DroidHub Test")
    router = build_usb_presence_router(registry, bridge_tokens)
    application.include_router(router)
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# --- 1. Тесты базового контракта (POST, PUT, DELETE) ---


def test_post_open_session_contract(client: TestClient, registry: USBPresenceRegistry) -> None:
    """POST /api/usb-presence/session без тела открывает сессию и возвращает {"session_id": str}."""
    resp = client.post(
        "/api/usb-presence/session",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "session_id" in data
    session_id = data["session_id"]
    assert isinstance(session_id, str) and len(session_id) > 0
    # Сессия зафиксирована в реестре
    assert registry.get_active_session("bridge-alpha") == session_id
    assert registry.get_generation("bridge-alpha") == 1


def test_post_ignores_user_provided_id(client: TestClient, registry: USBPresenceRegistry) -> None:
    """POST не принимает пользовательский ID сессии, вызывая open_session только с bridge_id."""
    malicious_body = json.dumps({"session_id": "hacked-session-id", "user_id": "attacker"})
    resp = client.post(
        "/api/usb-presence/session",
        headers={
            "Authorization": "Bearer secret-token-alpha-12345",
            "Content-Type": "application/json",
        },
        content=malicious_body,
    )
    assert resp.status_code == 200
    session_id = resp.json()["session_id"]
    assert session_id != "hacked-session-id"
    assert registry.get_active_session("bridge-alpha") == session_id


def test_post_subsequent_call_increments_generation(
    client: TestClient, registry: USBPresenceRegistry
) -> None:
    """Повторный POST для того же моста увеличивает generation и обновляет сессию."""
    resp1 = client.post(
        "/api/usb-presence/session",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
    )
    s1 = resp1.json()["session_id"]
    assert registry.get_generation("bridge-alpha") == 1

    resp2 = client.post(
        "/api/usb-presence/session",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
    )
    s2 = resp2.json()["session_id"]
    assert s1 != s2
    assert registry.get_generation("bridge-alpha") == 2
    assert registry.get_active_session("bridge-alpha") == s2


def test_put_snapshot_success(client: TestClient, registry: USBPresenceRegistry) -> None:
    """PUT /api/usb-presence/session/{session_id} со снимком разрешённых ID возвращает 204."""
    # Открываем сессию
    open_resp = client.post(
        "/api/usb-presence/session",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
    )
    session_id = open_resp.json()["session_id"]

    # Отправляем снимок
    put_resp = client.put(
        f"/api/usb-presence/session/{session_id}",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
        json={"device_ids": ["phone1", "phone2"]},
    )
    assert put_resp.status_code == 204
    assert put_resp.content == b""

    # Проверяем, что устройства присутствуют в реестре
    p1 = registry.resolve("phone1")
    assert p1 is not None
    assert p1.bridge_id == "bridge-alpha"
    assert p1.session_id == session_id

    p2 = registry.resolve("phone2")
    assert p2 is not None
    assert p2.bridge_id == "bridge-alpha"


def test_put_valid_empty_snapshot_clears_records(
    client: TestClient, registry: USBPresenceRegistry
) -> None:
    """Валидный пустой снимок {"device_ids": []} очищает записи устройств сессии."""
    open_resp = client.post(
        "/api/usb-presence/session",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
    )
    session_id = open_resp.json()["session_id"]

    # Регистрируем устройства
    client.put(
        f"/api/usb-presence/session/{session_id}",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
        json={"device_ids": ["phone1", "phone2"]},
    )
    assert registry.resolve("phone1") is not None
    assert registry.resolve("phone2") is not None

    # Отправляем пустой снимок
    put_resp = client.put(
        f"/api/usb-presence/session/{session_id}",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
        json={"device_ids": []},
    )
    assert put_resp.status_code == 204
    assert registry.resolve("phone1") is None
    assert registry.resolve("phone2") is None


def test_delete_session_contract_and_idempotency(
    client: TestClient, registry: USBPresenceRegistry
) -> None:
    """DELETE /api/usb-presence/session/{session_id} возвращает 204 и идемпотентен."""
    open_resp = client.post(
        "/api/usb-presence/session",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
    )
    session_id = open_resp.json()["session_id"]

    client.put(
        f"/api/usb-presence/session/{session_id}",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
        json={"device_ids": ["phone1"]},
    )
    assert registry.resolve("phone1") is not None

    # Первый DELETE закрывает сессию
    del_resp1 = client.delete(
        f"/api/usb-presence/session/{session_id}",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
    )
    assert del_resp1.status_code == 204
    assert registry.resolve("phone1") is None
    assert registry.get_active_session("bridge-alpha") is None

    # Повторный DELETE той же сессии также возвращает 204 (идемпотентность)
    del_resp2 = client.delete(
        f"/api/usb-presence/session/{session_id}",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
    )
    assert del_resp2.status_code == 204

    # DELETE для несуществующей сессии возвращает 204
    del_resp3 = client.delete(
        "/api/usb-presence/session/non-existent-session-id",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
    )
    assert del_resp3.status_code == 204


# --- 2. Тесты авторизации, изоляции и безопасности ---


def test_auth_missing_header_rejected(client: TestClient) -> None:
    """Отсутствие заголовка Authorization возвращает 401."""
    resp = client.post("/api/usb-presence/session")
    assert resp.status_code == 401
    assert "Bearer" in resp.headers.get("WWW-Authenticate", "")


def test_auth_invalid_scheme_rejected(client: TestClient) -> None:
    """Схема отличная от Bearer (например Basic) возвращает 401."""
    resp = client.post(
        "/api/usb-presence/session",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert resp.status_code == 401


def test_auth_empty_bearer_rejected(client: TestClient) -> None:
    """Пустой Bearer токен возвращает 401."""
    resp = client.post(
        "/api/usb-presence/session",
        headers={"Authorization": "Bearer "},
    )
    assert resp.status_code == 401


def test_auth_unknown_token_rejected(client: TestClient) -> None:
    """Неизвестный токен возвращает 401."""
    resp = client.post(
        "/api/usb-presence/session",
        headers={"Authorization": "Bearer wrong-unauthorized-token"},
    )
    assert resp.status_code == 401


def test_auth_no_cookie_fallback(client: TestClient) -> None:
    """Нет cookie fallback: сессионные и SSO cookies без заголовка Bearer возвращают 401."""
    # Пытаемся передать cookie сессии и SSO
    client.cookies.set("session", "admin-session-cookie")
    client.cookies.set("molokohub_sso", "valid-sso-token")
    resp = client.post("/api/usb-presence/session")
    assert resp.status_code == 401


def test_auth_empty_configuration_rejects_all(registry: USBPresenceRegistry) -> None:
    """Пустая конфигурация токенов не допускает ни одного обращения (401)."""
    empty_app = FastAPI()
    router = build_usb_presence_router(registry, bridge_tokens={})
    empty_app.include_router(router)
    empty_client = TestClient(empty_app)

    resp_post = empty_client.post(
        "/api/usb-presence/session",
        headers={"Authorization": "Bearer any-token"},
    )
    assert resp_post.status_code == 401

    resp_put = empty_client.put(
        "/api/usb-presence/session/some-session",
        headers={"Authorization": "Bearer any-token"},
        json={"device_ids": []},
    )
    assert resp_put.status_code == 401

    resp_del = empty_client.delete(
        "/api/usb-presence/session/some-session",
        headers={"Authorization": "Bearer any-token"},
    )
    assert resp_del.status_code == 401


def test_bridge_id_derived_strictly_from_token_not_client(
    client: TestClient, registry: USBPresenceRegistry
) -> None:
    """Bridge ID выводится только из токена, клиент не может подменить bridge_id через заголовки или query."""
    # Токен принадлежит bridge-alpha, но клиент пытается выдать себя за bridge-beta
    resp = client.post(
        "/api/usb-presence/session?bridge_id=bridge-beta",
        headers={
            "Authorization": "Bearer secret-token-alpha-12345",
            "X-Bridge-ID": "bridge-beta",
        },
    )
    assert resp.status_code == 200
    session_id = resp.json()["session_id"]
    # В реестре зарегистрирован строго bridge-alpha!
    assert registry.get_active_session("bridge-alpha") == session_id
    assert registry.get_active_session("bridge-beta") is None


def test_bridge_cross_session_hijack_prevention(
    client: TestClient, registry: USBPresenceRegistry
) -> None:
    """Мост bridge-beta не может применить снимок к сессии bridge-alpha (409)."""
    # Мост alpha открывает сессию
    open_alpha = client.post(
        "/api/usb-presence/session",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
    )
    alpha_session = open_alpha.json()["session_id"]

    # Мост beta пытается обновить снимок для сессии alpha
    put_beta = client.put(
        f"/api/usb-presence/session/{alpha_session}",
        headers={"Authorization": "Bearer secret-token-beta-67890"},
        json={"device_ids": ["phone1"]},
    )
    assert put_beta.status_code == 409
    assert registry.resolve("phone1") is None


def test_bridge_cannot_close_other_bridge_session(
    client: TestClient, registry: USBPresenceRegistry
) -> None:
    """Мост beta не может закрыть активную сессию моста alpha."""
    open_alpha = client.post(
        "/api/usb-presence/session",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
    )
    alpha_session = open_alpha.json()["session_id"]

    client.put(
        f"/api/usb-presence/session/{alpha_session}",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
        json={"device_ids": ["phone1"]},
    )
    assert registry.resolve("phone1") is not None

    # Мост beta отправляет DELETE для сессии alpha
    del_resp = client.delete(
        f"/api/usb-presence/session/{alpha_session}",
        headers={"Authorization": "Bearer secret-token-beta-67890"},
    )
    assert del_resp.status_code == 204

    # Сессия alpha и её устройства не затронуты и остаются активными
    assert registry.resolve("phone1") is not None
    assert registry.get_active_session("bridge-alpha") == alpha_session


def test_no_secrets_in_logs(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Bearer-токены и секреты никогда не попадают в логи."""
    secret_token = "secret-token-alpha-12345"
    with caplog.at_level(logging.DEBUG):
        client.post(
            "/api/usb-presence/session",
            headers={"Authorization": f"Bearer {secret_token}"},
        )
        client.post(
            "/api/usb-presence/session",
            headers={"Authorization": "Bearer invalid-secret-token"},
        )

    for record in caplog.records:
        assert secret_token not in record.message
        assert "invalid-secret-token" not in record.message


# --- 3. Валидация входных данных, PUT allowlist и ошибки 422 / 409 ---


def test_put_unknown_device_rejected_with_422(
    client: TestClient, registry: USBPresenceRegistry
) -> None:
    """Неизвестный ID устройства (не в allowlist) целиком отклоняет снимок с кодом 422 до изменений."""
    open_resp = client.post(
        "/api/usb-presence/session",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
    )
    session_id = open_resp.json()["session_id"]

    # phone1 разрешён, unknown_gadget_xyz — не разрешён
    resp = client.put(
        f"/api/usb-presence/session/{session_id}",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
        json={"device_ids": ["phone1", "unknown_gadget_xyz"]},
    )
    assert resp.status_code == 422
    # Реестр остался нетронутым: даже phone1 не зарегистрирован
    assert registry.resolve("phone1") is None
    assert registry.resolve("unknown_gadget_xyz") is None


def test_put_malformed_json_rejected_with_422(client: TestClient) -> None:
    """Некорректный JSON в PUT возвращает 422."""
    resp = client.put(
        "/api/usb-presence/session/some-session",
        headers={
            "Authorization": "Bearer secret-token-alpha-12345",
            "Content-Type": "application/json",
        },
        content=b"{not-valid-json",
    )
    assert resp.status_code == 422


def test_put_empty_body_rejected_with_422(client: TestClient) -> None:
    """Пустое тело в PUT возвращает 422."""
    resp = client.put(
        "/api/usb-presence/session/some-session",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
        content=b"",
    )
    assert resp.status_code == 422


def test_put_missing_device_ids_key_rejected_with_422(client: TestClient) -> None:
    """Отсутствие поля 'device_ids' в PUT возвращает 422."""
    resp = client.put(
        "/api/usb-presence/session/some-session",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
        json={"wrong_field": ["phone1"]},
    )
    assert resp.status_code == 422


def test_put_device_ids_not_list_rejected_with_422(client: TestClient) -> None:
    """Поле 'device_ids', не являющееся списком, возвращает 422."""
    resp = client.put(
        "/api/usb-presence/session/some-session",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
        json={"device_ids": "phone1"},
    )
    assert resp.status_code == 422


def test_put_duplicate_device_ids_rejected_with_422(client: TestClient) -> None:
    """Дублирующиеся ID устройств в снимке возвращают 422."""
    open_resp = client.post(
        "/api/usb-presence/session",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
    )
    session_id = open_resp.json()["session_id"]

    resp = client.put(
        f"/api/usb-presence/session/{session_id}",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
        json={"device_ids": ["phone1", "phone1"]},
    )
    assert resp.status_code == 422


def test_put_non_string_device_id_rejected_with_422(client: TestClient) -> None:
    """ID устройства не строкового типа возвращает 422."""
    open_resp = client.post(
        "/api/usb-presence/session",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
    )
    session_id = open_resp.json()["session_id"]

    resp = client.put(
        f"/api/usb-presence/session/{session_id}",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
        json={"device_ids": [12345]},
    )
    assert resp.status_code == 422


def test_put_device_id_length_and_characters_validation(client: TestClient) -> None:
    """Превышение длины ID устройства или недопустимые символы возвращают 422."""
    open_resp = client.post(
        "/api/usb-presence/session",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
    )
    session_id = open_resp.json()["session_id"]

    # Слишком длинный ID
    long_id = "a" * (MAX_DEVICE_ID_LENGTH + 1)
    resp_long = client.put(
        f"/api/usb-presence/session/{session_id}",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
        json={"device_ids": [long_id]},
    )
    assert resp_long.status_code == 422

    # Пустая строка в ID
    resp_empty_id = client.put(
        f"/api/usb-presence/session/{session_id}",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
        json={"device_ids": [""]},
    )
    assert resp_empty_id.status_code == 422

    # Пробелы и управляющие символы в ID
    resp_space = client.put(
        f"/api/usb-presence/session/{session_id}",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
        json={"device_ids": ["phone 1\n"]},
    )
    assert resp_space.status_code == 422


def test_put_extra_keys_rejected_with_422(client: TestClient) -> None:
    """Лишние неизвестные поля в JSON PUT возвращают 422."""
    resp = client.put(
        "/api/usb-presence/session/some-session",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
        json={"device_ids": ["phone1"], "extra_field": "disallowed"},
    )
    assert resp.status_code == 422


def test_put_array_length_limit_exceeded_with_422(client: TestClient) -> None:
    """Превышение максимального количества устройств в массиве возвращает 422."""
    too_many = [f"dev_{i}" for i in range(MAX_DEVICE_IDS_COUNT + 1)]
    resp = client.put(
        "/api/usb-presence/session/some-session",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
        json={"device_ids": too_many},
    )
    assert resp.status_code == 422


def test_session_id_validation_put_and_delete(client: TestClient) -> None:
    """Недопустимый session_id (слишком длинный или с плохими символами) возвращает 422."""
    too_long_session = "s" * (MAX_SESSION_ID_LENGTH + 1)
    bad_chars_session = "sess$#@!"

    for bad_sess in [too_long_session, bad_chars_session]:
        resp_put = client.put(
            f"/api/usb-presence/session/{bad_sess}",
            headers={"Authorization": "Bearer secret-token-alpha-12345"},
            json={"device_ids": ["phone1"]},
        )
        assert resp_put.status_code == 422

        resp_del = client.delete(
            f"/api/usb-presence/session/{bad_sess}",
            headers={"Authorization": "Bearer secret-token-alpha-12345"},
        )
        assert resp_del.status_code == 422


def test_put_outdated_or_unopened_session_returns_409(client: TestClient) -> None:
    """Попытка отправить снимок в неоткрытую сессию возвращает 409."""
    resp = client.put(
        "/api/usb-presence/session/unopened-session-id",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
        json={"device_ids": ["phone1"]},
    )
    assert resp.status_code == 409


def test_put_superseded_session_returns_409(client: TestClient) -> None:
    """Запоздалый снимок устаревшей сессии после переподключения моста возвращает 409."""
    # Первая сессия
    s1 = client.post(
        "/api/usb-presence/session",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
    ).json()["session_id"]

    # Вторая сессия того же моста (вытесняет первую)
    s2 = client.post(
        "/api/usb-presence/session",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
    ).json()["session_id"]
    assert s1 != s2

    # Запоздалый снимок от s1
    late_resp = client.put(
        f"/api/usb-presence/session/{s1}",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
        json={"device_ids": ["phone1"]},
    )
    assert late_resp.status_code == 409

    # Снимок от актуальной s2 успешен
    ok_resp = client.put(
        f"/api/usb-presence/session/{s2}",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
        json={"device_ids": ["phone1"]},
    )
    assert ok_resp.status_code == 204


def test_put_device_conflict_returns_409(client: TestClient) -> None:
    """Попытка захватить устройство, уже занятое другой активной сессией, возвращает 409."""
    # bridge-alpha захватывает phone1
    s_alpha = client.post(
        "/api/usb-presence/session",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
    ).json()["session_id"]
    client.put(
        f"/api/usb-presence/session/{s_alpha}",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
        json={"device_ids": ["phone1"]},
    )

    # bridge-beta открывает сессию и пытается захватить phone2 и конфликтный phone1
    s_beta = client.post(
        "/api/usb-presence/session",
        headers={"Authorization": "Bearer secret-token-beta-67890"},
    ).json()["session_id"]
    conflict_resp = client.put(
        f"/api/usb-presence/session/{s_beta}",
        headers={"Authorization": "Bearer secret-token-beta-67890"},
        json={"device_ids": ["phone2", "phone1"]},
    )
    assert conflict_resp.status_code == 409


def test_put_ttl_expiration_conflict_cleared(
    client: TestClient, fake_clock: FakeClock, registry: USBPresenceRegistry
) -> None:
    """После истечения TTL предыдущей сессии устройство может быть захвачено другой сессией."""
    s_alpha = client.post(
        "/api/usb-presence/session",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
    ).json()["session_id"]
    client.put(
        f"/api/usb-presence/session/{s_alpha}",
        headers={"Authorization": "Bearer secret-token-alpha-12345"},
        json={"device_ids": ["phone1"]},
    )

    # Прошло 12 секунд (TTL=10)
    fake_clock.advance(12.0)

    # Теперь мост beta может успешно открыть сессию и захватить phone1
    s_beta = client.post(
        "/api/usb-presence/session",
        headers={"Authorization": "Bearer secret-token-beta-67890"},
    ).json()["session_id"]
    resp_beta = client.put(
        f"/api/usb-presence/session/{s_beta}",
        headers={"Authorization": "Bearer secret-token-beta-67890"},
        json={"device_ids": ["phone1"]},
    )
    assert resp_beta.status_code == 204
    p = registry.resolve("phone1")
    assert p is not None
    assert p.bridge_id == "bridge-beta"


# --- 4. Тесты ограничения размера тела запроса (64 КиБ / 413) ---


def test_body_size_limit_with_content_length_exceeded(client: TestClient) -> None:
    """Тело запроса более 64 КиБ с Content-Length возвращает 413."""
    oversized = b"x" * (MAX_BODY_BYTES + 100)
    resp = client.put(
        "/api/usb-presence/session/some-session",
        headers={
            "Authorization": "Bearer secret-token-alpha-12345",
            "Content-Type": "application/json",
            "Content-Length": str(len(oversized)),
        },
        content=oversized,
    )
    assert resp.status_code == 413


def test_body_size_limit_without_content_length_chunked_exceeded(client: TestClient) -> None:
    """Тело запроса более 64 КиБ без Content-Length (потоковая передача чанками) возвращает 413."""
    def chunk_generator() -> Iterator[bytes]:
        # Отправляем 70 чанков по 1024 байта = 71680 байт (> 65536)
        for _ in range(70):
            yield b"a" * 1024

    resp = client.put(
        "/api/usb-presence/session/some-session",
        headers={
            "Authorization": "Bearer secret-token-alpha-12345",
            "Content-Type": "application/octet-stream",
        },
        content=chunk_generator(),
    )
    assert resp.status_code == 413


def test_post_body_size_limit_exceeded(client: TestClient) -> None:
    """POST запрос с телом более 64 КиБ возвращает 413."""
    oversized = b"x" * (MAX_BODY_BYTES + 50)
    resp = client.post(
        "/api/usb-presence/session",
        headers={
            "Authorization": "Bearer secret-token-alpha-12345",
            "Content-Length": str(len(oversized)),
        },
        content=oversized,
    )
    assert resp.status_code == 413
