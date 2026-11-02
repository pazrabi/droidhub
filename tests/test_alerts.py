from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from hub.alerts import AlertMonitor, TelegramNotifier, _sanitize
from hub.config import DeviceConfig, Settings
from hub.devices import actions
from hub.devices.manager import DeviceManager, DeviceStatus


@pytest.fixture
def manager() -> DeviceManager:
    dev1 = DeviceConfig(id="dev1", name="Pixel 6", host="10.0.0.1", port=5555)
    dev2 = DeviceConfig(id="dev2", name="Redmi Note", host="10.0.0.2", port=5555)
    return DeviceManager([dev1, dev2])


@pytest.fixture
def mock_power(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    p = AsyncMock()
    monkeypatch.setattr(actions, "power", p)
    return p


# 1. Пороги warn / alert / shutdown
@pytest.mark.asyncio
async def test_temperature_thresholds(manager: DeviceManager, mock_power: AsyncMock) -> None:
    settings = Settings(
        temp_warn_c=40.0,
        temp_alert_c=45.0,
        temp_shutdown_c=50.0,
        alert_cooldown_min=30.0,
    )
    notifier = AsyncMock(spec=TelegramNotifier)
    notifier.send = AsyncMock(return_value=True)
    monitor = AlertMonitor(manager, settings, notifier=notifier)

    # Нормальная температура — нет алертов
    status_ok = DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=38.5)
    await monitor.check_device(status_ok, now=1000.0)
    notifier.send.assert_not_called()
    mock_power.assert_not_called()

    # Порог warn (40.0 °C) — предупреждение без кнопок
    status_warn = DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=40.0)
    await monitor.check_device(status_warn, now=1001.0)
    assert notifier.send.call_count == 1
    call_args, call_kwargs = notifier.send.call_args
    assert "Pixel 6" in call_args[0]
    assert "40.0" in call_args[0]
    assert call_kwargs.get("buttons") is None
    mock_power.assert_not_called()
    notifier.send.reset_mock()

    # Порог alert (45.0 °C) — тревога с inline-кнопкой «Выключить Pixel 6»
    status_alert = DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=45.0)
    await monitor.check_device(status_alert, now=1002.0)
    assert notifier.send.call_count == 1
    call_args, call_kwargs = notifier.send.call_args
    assert "Pixel 6" in call_args[0]
    assert "45.0" in call_args[0]
    buttons = call_kwargs.get("buttons")
    assert buttons is not None
    assert any("Выключить Pixel 6" in b.get("text", "") for b in buttons)
    assert any(b.get("callback_data") == "off:dev1" for b in buttons)
    mock_power.assert_not_called()
    notifier.send.reset_mock()

    # Порог shutdown (50.0 °C) — авто-выключение через actions.power
    status_shutdown = DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=50.0)
    await monitor.check_device(status_shutdown, now=1003.0)
    mock_power.assert_called_once_with("dev1", "shutdown", actor="alerts")
    assert notifier.send.call_count == 1
    assert "выключается" in notifier.send.call_args[0][0]


# 2. Кулдаун и рост на 2 °C
@pytest.mark.asyncio
async def test_temperature_cooldown_and_2c_rise(manager: DeviceManager) -> None:
    settings = Settings(
        temp_warn_c=40.0,
        temp_alert_c=45.0,
        temp_shutdown_c=50.0,
        alert_cooldown_min=30.0,
    )
    notifier = AsyncMock(spec=TelegramNotifier)
    notifier.send = AsyncMock(return_value=True)
    monitor = AlertMonitor(manager, settings, notifier=notifier)

    t0 = 1000.0
    # Первое предупреждение на 40.0 °C
    s1 = DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=40.0)
    await monitor.check_device(s1, now=t0)
    assert notifier.send.call_count == 1
    notifier.send.reset_mock()

    # Внутри кулдауна (через 60 с): температура 40.5 °C (рост 0.5 < 2.0 °C) — молчит
    s2 = DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=40.5)
    await monitor.check_device(s2, now=t0 + 60.0)
    notifier.send.assert_not_called()

    # Внутри кулдауна: температура 41.5 °C (рост 1.5 < 2.0 °C) — молчит
    s3 = DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=41.5)
    await monitor.check_device(s3, now=t0 + 120.0)
    notifier.send.assert_not_called()

    # Внутри кулдауна: температура 42.0 °C (рост 2.0 >= 2.0 °C от 40.0) — НЕ МОЛЧИТ!
    s4 = DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=42.0)
    await monitor.check_device(s4, now=t0 + 180.0)
    assert notifier.send.call_count == 1
    notifier.send.reset_mock()

    # Следующий шаг: 43.0 °C (рост 1.0 от 42.0 < 2.0) — молчит
    s5 = DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=43.0)
    await monitor.check_device(s5, now=t0 + 240.0)
    notifier.send.assert_not_called()

    # Прошло больше 30 мин кулдауна (31 мин): при той же температуре 42.0 °C снова отправляет
    s6 = DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=42.0)
    await monitor.check_device(s6, now=t0 + 180.0 + 31 * 60.0)
    assert notifier.send.call_count == 1

    # Граничный случай float: 42.1 -> 44.1 (44.1 - 42.1 = 1.9999999999999991 < 2.0 без округления)
    monitor_float = AlertMonitor(manager, settings, notifier=notifier)
    notifier.send.reset_mock()
    await monitor_float.check_device(DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=42.1), now=t0)
    assert notifier.send.call_count == 1
    notifier.send.reset_mock()
    await monitor_float.check_device(DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=44.1), now=t0 + 60.0)
    assert notifier.send.call_count == 1


# 3. 100% заряда дольше N часов
@pytest.mark.asyncio
async def test_full_charge_longer_than_n_hours(manager: DeviceManager) -> None:
    settings = Settings(full_charge_hours=6.0, alert_cooldown_min=30.0)
    notifier = AsyncMock(spec=TelegramNotifier)
    notifier.send = AsyncMock(return_value=True)
    monitor = AlertMonitor(manager, settings, notifier=notifier)

    t0 = 1000.0
    # Первое обнаружение 100% заряда
    s1 = DeviceStatus(id="dev1", name="Pixel 6", online=True, battery=100)
    await monitor.check_device(s1, now=t0)
    notifier.send.assert_not_called()

    # 5.9 часов на 100% — напоминания еще нет
    s2 = DeviceStatus(id="dev1", name="Pixel 6", online=True, battery=100)
    await monitor.check_device(s2, now=t0 + 5.9 * 3600)
    notifier.send.assert_not_called()

    # 6.0 часов на 100% — напоминание отправляется!
    s3 = DeviceStatus(id="dev1", name="Pixel 6", online=True, battery=100)
    await monitor.check_device(s3, now=t0 + 6.0 * 3600)
    assert notifier.send.call_count == 1
    assert "заряд держится на 100%" in notifier.send.call_args[0][0]
    notifier.send.reset_mock()

    # Внутри кулдауна (через 5 мин) — не дублирует
    s4 = DeviceStatus(id="dev1", name="Pixel 6", online=True, battery=100)
    await monitor.check_device(s4, now=t0 + 6.0 * 3600 + 300)
    notifier.send.assert_not_called()

    # Батарея упала до 99% — сброс таймера
    s5 = DeviceStatus(id="dev1", name="Pixel 6", online=True, battery=99)
    await monitor.check_device(s5, now=t0 + 6.5 * 3600)
    notifier.send.assert_not_called()

    # Снова 100% — таймер начинается заново, через 1 час алерта нет
    s6 = DeviceStatus(id="dev1", name="Pixel 6", online=True, battery=100)
    await monitor.check_device(s6, now=t0 + 7.0 * 3600)
    await monitor.check_device(s6, now=t0 + 8.0 * 3600)
    notifier.send.assert_not_called()


# 4. Оффлайн и возврат онлайн
@pytest.mark.asyncio
async def test_offline_alert_and_recovery(manager: DeviceManager) -> None:
    settings = Settings(offline_alert_min=10.0, alert_cooldown_min=30.0)
    notifier = AsyncMock(spec=TelegramNotifier)
    notifier.send = AsyncMock(return_value=True)
    monitor = AlertMonitor(manager, settings, notifier=notifier)

    t0 = 1000.0
    # Устройство уходит в оффлайн
    s_off = DeviceStatus(id="dev1", name="Pixel 6", online=False, last_seen=t0)
    await monitor.check_device(s_off, now=t0)
    notifier.send.assert_not_called()

    # 9 минут оффлайн — алерта еще нет
    await monitor.check_device(s_off, now=t0 + 9 * 60)
    notifier.send.assert_not_called()

    # 10 минут оффлайн — алерт об оффлайне!
    await monitor.check_device(s_off, now=t0 + 10 * 60)
    assert notifier.send.call_count == 1
    assert "оффлайн более 10 мин" in notifier.send.call_args[0][0]
    notifier.send.reset_mock()

    # 15 минут оффлайн — внутри кулдауна не спамит
    await monitor.check_device(s_off, now=t0 + 15 * 60)
    notifier.send.assert_not_called()

    # Устройство снова онлайн — сообщение «снова онлайн»
    s_on = DeviceStatus(id="dev1", name="Pixel 6", online=True, last_seen=t0 + 20 * 60)
    await monitor.check_device(s_on, now=t0 + 20 * 60)
    assert notifier.send.call_count == 1
    assert "снова онлайн" in notifier.send.call_args[0][0]
    notifier.send.reset_mock()

    # Кратковременный оффлайн на 2 минуты — не должен вызывать алерта возврата
    await monitor.check_device(s_off, now=t0 + 25 * 60)
    await monitor.check_device(s_on, now=t0 + 27 * 60)
    notifier.send.assert_not_called()


# 5. Авто-выключение и отключение авто-выключения при 0
@pytest.mark.asyncio
async def test_auto_shutdown_behavior(manager: DeviceManager, mock_power: AsyncMock) -> None:
    notifier = AsyncMock(spec=TelegramNotifier)
    notifier.send = AsyncMock(return_value=True)

    # 1. temp_shutdown_c = 50.0 — автовыключение срабатывает
    settings_enabled = Settings(temp_shutdown_c=50.0, temp_alert_c=45.0, temp_warn_c=40.0)
    monitor1 = AlertMonitor(manager, settings_enabled, notifier=notifier)
    status_hot = DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=51.0)
    await monitor1.check_device(status_hot, now=1000.0)
    mock_power.assert_called_once_with("dev1", "shutdown", actor="alerts")
    assert notifier.send.call_count == 1
    assert "выключается" in notifier.send.call_args[0][0]

    mock_power.reset_mock()
    notifier.send.reset_mock()

    # 2. temp_shutdown_c = 0 — автовыключение отключено
    settings_disabled = Settings(temp_shutdown_c=0.0, temp_alert_c=45.0, temp_warn_c=40.0)
    monitor2 = AlertMonitor(manager, settings_disabled, notifier=notifier)
    status_super_hot = DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=55.0)
    await monitor2.check_device(status_super_hot, now=1000.0)
    # actions.power НЕ вызывается!
    mock_power.assert_not_called()
    # Но шлется тревога с кнопкой выключения
    assert notifier.send.call_count == 1
    assert "Тревога" in notifier.send.call_args[0][0]
    assert notifier.send.call_args[1].get("buttons") is not None

    # 3. Сбой вызова actions.power — сообщение об ошибке авто-выключения
    mock_power.reset_mock()
    notifier.send.reset_mock()
    mock_power.side_effect = RuntimeError("ADB timeout")
    status_fail = DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=52.0)
    await monitor1.check_device(status_fail, now=3000.0)
    mock_power.assert_called_once_with("dev1", "shutdown", actor="alerts")
    assert notifier.send.call_count == 1
    assert "Сбой авто-выключения" in notifier.send.call_args[0][0]
    assert "ADB timeout" in notifier.send.call_args[0][0]
    mock_power.side_effect = None


# 6. Чужой chat_id игнорируется
@pytest.mark.asyncio
async def test_unauthorized_chat_id_ignored(manager: DeviceManager, mock_power: AsyncMock) -> None:
    notifier = TelegramNotifier(token="fake_token", chat_id="12345", manager=manager)
    notifier.send = AsyncMock(return_value=True)  # type: ignore[method-assign]
    notifier.answer_callback = AsyncMock(return_value=True)  # type: ignore[method-assign]

    # Callback_query от чужого chat_id (99999)
    foreign_update = {
        "update_id": 1,
        "callback_query": {
            "id": "cb_foreign",
            "from": {"id": 99999},
            "message": {"chat": {"id": 99999}},
            "data": "confirm_off:dev1",
        },
    }
    await notifier.handle_update(foreign_update)
    mock_power.assert_not_called()
    notifier.send.assert_not_called()
    notifier.answer_callback.assert_not_called()

    # Callback_query с правильным chat_id
    authorized_update = {
        "update_id": 2,
        "callback_query": {
            "id": "cb_auth",
            "from": {"id": 12345},
            "message": {"chat": {"id": 12345}},
            "data": "confirm_off:dev1",
        },
    }
    await notifier.handle_update(authorized_update)
    mock_power.assert_called_once_with("dev1", "shutdown", actor="telegram")
    assert notifier.send.call_count == 1


# 7. Пустой токен — только лог
@pytest.mark.asyncio
async def test_empty_token_only_logs(caplog: pytest.LogCaptureFixture) -> None:
    notifier = TelegramNotifier(token="", chat_id="12345")
    with caplog.at_level(logging.INFO):
        res = await notifier.send("Тестовое сообщение", buttons=[{"text": "Кнопка", "callback_data": "btn"}])
    assert res is True
    assert "Telegram notification [token empty, log only]: Тестовое сообщение" in caplog.text


# 8. Токен нигде не логируется
def test_token_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    secret_token = "SECRET_TELEGRAM_BOT_TOKEN_12345"
    notifier = TelegramNotifier(token=secret_token, chat_id="12345")
    # Проверка строкового представления
    assert secret_token not in repr(notifier)
    assert secret_token not in str(notifier)

    # Проверка функции санитизации
    assert "SECRET" not in _sanitize(f"https://api.telegram.org/bot{secret_token}/getUpdates", secret_token)
    assert "[REDACTED]" in _sanitize(f"https://api.telegram.org/bot{secret_token}/getUpdates", secret_token)

    # Уровень логгера httpx должен быть заглушен до WARNING
    assert logging.getLogger("httpx").level >= logging.WARNING

    # Принудительно проверяем, что фильтр логгера httpx санитизирует URL с токеном
    httpx_logger = logging.getLogger("httpx")
    with caplog.at_level(logging.INFO, logger="httpx"):
        httpx_logger.info("HTTP Request: GET https://api.telegram.org/bot%s/getUpdates 200 OK", secret_token)
    assert secret_token not in caplog.text
    assert "[REDACTED]" in caplog.text


# 9. Нажатие кнопки с подтверждением (Да / Нет)
@pytest.mark.asyncio
async def test_button_confirmation_flow(manager: DeviceManager, mock_power: AsyncMock) -> None:
    notifier = TelegramNotifier(token="fake_token", chat_id="12345", manager=manager)
    notifier.send = AsyncMock(return_value=True)  # type: ignore[method-assign]
    notifier.answer_callback = AsyncMock(return_value=True)  # type: ignore[method-assign]

    # Шаг 1: Нажатие кнопки «Выключить Pixel 6» (off:dev1)
    step1_update = {
        "update_id": 10,
        "callback_query": {
            "id": "cb_step1",
            "from": {"id": 12345},
            "message": {"chat": {"id": 12345}},
            "data": "off:dev1",
        },
    }
    await notifier.handle_update(step1_update)
    notifier.answer_callback.assert_called_once_with("cb_step1")
    assert notifier.send.call_count == 1
    call_text, call_kwargs = notifier.send.call_args
    assert "Выключить Pixel 6? Да/Нет" in call_text or "Выключить Pixel 6" in call_text
    buttons = call_kwargs.get("buttons")
    assert buttons is not None
    # Должны быть кнопки Да и Нет
    flat_buttons = buttons[0] if isinstance(buttons[0], list) else buttons
    yes_btn = next((b for b in flat_buttons if b["text"] == "Да"), None)
    no_btn = next((b for b in flat_buttons if b["text"] == "Нет"), None)
    assert yes_btn is not None
    assert no_btn is not None
    assert yes_btn["callback_data"] == "confirm_off:dev1"
    assert no_btn["callback_data"] == "cancel_off:dev1"
    mock_power.assert_not_called()

    # Шаг 2А: Пользователь нажимает «Нет»
    notifier.send.reset_mock()
    notifier.answer_callback.reset_mock()
    cancel_update = {
        "update_id": 11,
        "callback_query": {
            "id": "cb_cancel",
            "from": {"id": 12345},
            "message": {"chat": {"id": 12345}},
            "data": "cancel_off:dev1",
        },
    }
    await notifier.handle_update(cancel_update)
    mock_power.assert_not_called()
    assert notifier.send.call_count == 1
    assert "отменено" in notifier.send.call_args[0][0].lower()

    # Шаг 2Б: Пользователь нажимает «Да»
    notifier.send.reset_mock()
    notifier.answer_callback.reset_mock()
    confirm_update = {
        "update_id": 12,
        "callback_query": {
            "id": "cb_confirm",
            "from": {"id": 12345},
            "message": {"chat": {"id": 12345}},
            "data": "confirm_off:dev1",
        },
    }
    await notifier.handle_update(confirm_update)
    mock_power.assert_called_once_with("dev1", "shutdown", actor="telegram")
    assert notifier.send.call_count == 1
    assert "выключается" in notifier.send.call_args[0][0].lower()

    # Шаг 2В: Неизвестный callback — подтверждается получение (answer_callback), чтобы не зависал индикатор загрузки
    notifier.send.reset_mock()
    notifier.answer_callback.reset_mock()
    unknown_update = {
        "update_id": 13,
        "callback_query": {
            "id": "cb_unknown",
            "from": {"id": 12345},
            "message": {"chat": {"id": 12345}},
            "data": "unknown_action_data",
        },
    }
    await notifier.handle_update(unknown_update)
    notifier.answer_callback.assert_called_once_with("cb_unknown")
    notifier.send.assert_not_called()


# 10. Нажатие кнопки с подтверждением через мок httpx
@pytest.mark.asyncio
async def test_button_confirmation_mock_httpx(manager: DeviceManager, mock_power: AsyncMock) -> None:
    calls = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "sendMessage" in str(request.url):
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 100}})
        if "answerCallbackQuery" in str(request.url):
            return httpx.Response(200, json={"ok": True, "result": True})
        if "getUpdates" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {
                            "update_id": 1,
                            "callback_query": {
                                "id": "cb_1",
                                "from": {"id": 12345},
                                "message": {"chat": {"id": 12345}},
                                "data": "off:dev1",
                            },
                        }
                    ],
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handle_request)
    client = httpx.AsyncClient(transport=transport)

    notifier = TelegramNotifier(
        token="test_token_123",
        chat_id="12345",
        manager=manager,
        client=client,
    )

    # 1. Отправка подтверждения при callback off:dev1
    update = {
        "update_id": 1,
        "callback_query": {
            "id": "cb_1",
            "from": {"id": 12345},
            "message": {"chat": {"id": 12345}},
            "data": "off:dev1",
        },
    }
    await notifier.handle_update(update)

    # Проверяем, что были вызовы answerCallbackQuery и sendMessage
    urls = [str(r.url) for r in calls]
    assert any("answerCallbackQuery" in u for u in urls)
    assert any("sendMessage" in u for u in urls)

    # 2. Подтверждение Да -> вызов actions.power
    confirm_update = {
        "update_id": 2,
        "callback_query": {
            "id": "cb_2",
            "from": {"id": 12345},
            "message": {"chat": {"id": 12345}},
            "data": "confirm_off:dev1",
        },
    }
    await notifier.handle_update(confirm_update)
    mock_power.assert_called_once_with("dev1", "shutdown", actor="telegram")

    await client.aclose()


# 11. Фоновый цикл AlertMonitor check_once
@pytest.mark.asyncio
async def test_alert_monitor_check_once(manager: DeviceManager) -> None:
    settings = Settings(temp_warn_c=40.0)
    notifier = AsyncMock(spec=TelegramNotifier)
    notifier.send = AsyncMock(return_value=True)

    monitor = AlertMonitor(manager, settings, notifier=notifier)
    manager._statuses["dev1"] = DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=41.0)
    manager._statuses["dev2"] = DeviceStatus(id="dev2", name="Redmi Note", online=True, temperature=35.0)

    await monitor.check_once()
    assert notifier.send.call_count == 1
    assert "Pixel 6" in notifier.send.call_args[0][0]
