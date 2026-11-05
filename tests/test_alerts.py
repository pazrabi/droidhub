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
        public_url="https://hub.example.com",
    )
    notifier = AsyncMock(spec=TelegramNotifier)
    notifier.send = AsyncMock(return_value=True)
    monitor = AlertMonitor(manager, settings, notifier=notifier)

    # Нормальная температура — нет алертов
    status_ok = DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=38.5)
    await monitor.check_device(status_ok, now=1000.0)
    notifier.send.assert_not_called()
    mock_power.assert_not_called()

    # Порог warn (40.0 °C) — предупреждение без кнопок и без фразы о зарядке
    status_warn = DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=40.0)
    await monitor.check_device(status_warn, now=1001.0)
    assert notifier.send.call_count == 1
    call_args, call_kwargs = notifier.send.call_args
    assert "[droidhub]" in call_args[0]
    assert "Pixel 6" in call_args[0]
    assert "40.0" in call_args[0]
    assert "Зарядка не остановлена" not in call_args[0]
    assert call_kwargs.get("buttons") is None
    mock_power.assert_not_called()
    notifier.send.reset_mock()

    # Порог alert (45.0 °C) — тревога с inline-кнопкой «Открыть панель» и фразой о зарядке
    status_alert = DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=45.0)
    await monitor.check_device(status_alert, now=1002.0)
    assert notifier.send.call_count == 1
    call_args, call_kwargs = notifier.send.call_args
    assert "[droidhub]" in call_args[0]
    assert "Pixel 6" in call_args[0]
    assert "45.0" in call_args[0]
    assert "Зарядка не остановлена: при подключённом кабеле батарея продолжает заряжаться, отключи питание физически" in call_args[0]
    buttons = call_kwargs.get("buttons")
    assert buttons is not None
    assert buttons == [{"text": "Открыть панель", "url": "https://hub.example.com/device/dev1"}]
    mock_power.assert_not_called()
    notifier.send.reset_mock()

    # Порог shutdown (50.0 °C) — авто-выключение через actions.power и фраза о зарядке
    status_shutdown = DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=50.0)
    await monitor.check_device(status_shutdown, now=1003.0)
    mock_power.assert_called_once_with("dev1", "shutdown", actor="alerts")
    assert notifier.send.call_count == 1
    call_text = notifier.send.call_args[0][0]
    assert "[droidhub]" in call_text
    assert "выключается" in call_text
    assert "Зарядка не остановлена: при подключённом кабеле батарея продолжает заряжаться, отключи питание физически" in call_text


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


@pytest.mark.asyncio
async def test_full_charge_alert_disabled_by_zero_hours(manager: DeviceManager) -> None:
    settings = Settings(full_charge_hours=0.0, alert_cooldown_min=30.0)
    notifier = AsyncMock(spec=TelegramNotifier)
    notifier.send = AsyncMock(return_value=True)
    monitor = AlertMonitor(manager, settings, notifier=notifier)

    status = DeviceStatus(id="dev1", name="Pixel 6", online=True, battery=100)
    await monitor.check_device(status, now=1000.0)
    await monitor.check_device(status, now=1000.0 + 48 * 3600)
    notifier.send.assert_not_called()


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
    msg1 = notifier.send.call_args[0][0]
    assert "выключается" in msg1
    assert "Зарядка не остановлена: при подключённом кабеле батарея продолжает заряжаться, отключи питание физически" in msg1

    mock_power.reset_mock()
    notifier.send.reset_mock()

    # 2. temp_shutdown_c = 0 — автовыключение отключено
    settings_disabled = Settings(
        temp_shutdown_c=0.0,
        temp_alert_c=45.0,
        temp_warn_c=40.0,
        public_url="https://hub.example.com",
    )
    monitor2 = AlertMonitor(manager, settings_disabled, notifier=notifier)
    status_super_hot = DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=55.0)
    await monitor2.check_device(status_super_hot, now=1000.0)
    # actions.power НЕ вызывается!
    mock_power.assert_not_called()
    # Но шлется тревога с кнопкой «Открыть панель»
    assert notifier.send.call_count == 1
    msg2 = notifier.send.call_args[0][0]
    assert "Тревога" in msg2
    assert "Зарядка не остановлена: при подключённом кабеле батарея продолжает заряжаться, отключи питание физически" in msg2
    buttons = notifier.send.call_args[1].get("buttons")
    assert buttons == [{"text": "Открыть панель", "url": "https://hub.example.com/device/dev1"}]

    # 3. Сбой вызова actions.power — сообщение об ошибке авто-выключения
    mock_power.reset_mock()
    notifier.send.reset_mock()
    mock_power.side_effect = RuntimeError("ADB timeout")
    status_fail = DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=52.0)
    await monitor1.check_device(status_fail, now=3000.0)
    mock_power.assert_called_once_with("dev1", "shutdown", actor="alerts")
    assert notifier.send.call_count == 1
    msg3 = notifier.send.call_args[0][0]
    assert "Сбой авто-выключения" in msg3
    assert "ADB timeout" in msg3
    assert "Зарядка не остановлена: при подключённом кабеле батарея продолжает заряжаться, отключи питание физически" in msg3
    mock_power.side_effect = None


# 6. Пустой токен — только лог с префиксом
@pytest.mark.asyncio
async def test_empty_token_only_logs(caplog: pytest.LogCaptureFixture) -> None:
    notifier = TelegramNotifier(token="", chat_id="12345")
    with caplog.at_level(logging.INFO):
        res = await notifier.send("Тестовое сообщение", buttons=[{"text": "Открыть панель", "url": "https://example.com"}])
    assert res is True
    assert "Telegram notification [token empty, log only]: [droidhub] Тестовое сообщение" in caplog.text


# 7. Токен нигде не логируется
def test_token_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    secret_token = "SECRET_TELEGRAM_BOT_TOKEN_12345"
    notifier = TelegramNotifier(token=secret_token, chat_id="12345")
    # Проверка строкового представления
    assert secret_token not in repr(notifier)
    assert secret_token not in str(notifier)

    # Проверка функции санитизации
    assert "SECRET" not in _sanitize(f"https://api.telegram.org/bot{secret_token}/sendMessage", secret_token)
    assert "[REDACTED]" in _sanitize(f"https://api.telegram.org/bot{secret_token}/sendMessage", secret_token)

    # Уровень логгера httpx должен быть заглушен до WARNING
    assert logging.getLogger("httpx").level >= logging.WARNING

    # Принудительно проверяем, что фильтр логгера httpx санитизирует URL с токеном
    httpx_logger = logging.getLogger("httpx")
    with caplog.at_level(logging.INFO, logger="httpx"):
        httpx_logger.info("HTTP Request: POST https://api.telegram.org/bot%s/sendMessage 200 OK", secret_token)
    assert secret_token not in caplog.text
    assert "[REDACTED]" in caplog.text


# 8. Префикс [<alert_tag>] во всех отправляемых сообщениях
@pytest.mark.asyncio
async def test_alert_tag_prefix_behavior(manager: DeviceManager) -> None:
    # 8.1. TelegramNotifier с дефолтным alert_tag ("droidhub")
    logged_calls = []

    def handle_req(request: httpx.Request) -> httpx.Response:
        import json
        payload = json.loads(request.content)
        logged_calls.append(payload)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle_req))
    notifier_default = TelegramNotifier(token="tok", chat_id="123", client=client)
    await notifier_default.send("Привет")
    assert logged_calls[-1]["text"] == "[droidhub] Привет"

    # Не дублирует префикс, если он уже есть
    await notifier_default.send("[droidhub] Уже с префиксом")
    assert logged_calls[-1]["text"] == "[droidhub] Уже с префиксом"

    # 8.2. TelegramNotifier с кастомным alert_tag
    notifier_custom = TelegramNotifier(token="tok", chat_id="123", client=client, alert_tag="myfarm")
    await notifier_custom.send("Сбой питания")
    assert logged_calls[-1]["text"] == "[myfarm] Сбой питания"

    await client.aclose()

    # 8.3. AlertMonitor форматирует все типы алертов с префиксом
    settings = Settings(
        alert_tag="farm2",
        temp_warn_c=40.0,
        temp_alert_c=45.0,
        temp_shutdown_c=50.0,
        offline_alert_min=5.0,
        full_charge_hours=1.0,
    )
    mock_notifier = AsyncMock(spec=TelegramNotifier)
    mock_notifier.send = AsyncMock(return_value=True)
    monitor = AlertMonitor(manager, settings, notifier=mock_notifier)

    # Предупреждение
    await monitor.check_device(DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=40.5), now=1000.0)
    assert mock_notifier.send.call_args[0][0].startswith("[farm2] Предупреждение:")
    mock_notifier.send.reset_mock()

    # Тревога
    await monitor.check_device(DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=46.0), now=2000.0)
    assert mock_notifier.send.call_args[0][0].startswith("[farm2] Тревога:")
    mock_notifier.send.reset_mock()

    # Оффлайн
    await monitor.check_device(DeviceStatus(id="dev1", name="Pixel 6", online=False, last_seen=3000.0), now=3000.0 + 360.0)
    assert mock_notifier.send.call_args[0][0].startswith("[farm2] Устройство Pixel 6 оффлайн")
    mock_notifier.send.reset_mock()

    # Возврат онлайн
    await monitor.check_device(DeviceStatus(id="dev1", name="Pixel 6", online=True, last_seen=3500.0), now=3500.0)
    assert mock_notifier.send.call_args[0][0] == "[farm2] Устройство Pixel 6 снова онлайн"


# 9. Кнопка с URL: наличие, текст «Открыть панель», формирование URL и отсутствие при пустом public_url
@pytest.mark.asyncio
async def test_url_button_behavior(manager: DeviceManager) -> None:
    mock_notifier = AsyncMock(spec=TelegramNotifier)
    mock_notifier.send = AsyncMock(return_value=True)

    # 9.1. public_url задан без слэша
    settings_with_url = Settings(temp_alert_c=45.0, public_url="https://hub.example.com")
    monitor = AlertMonitor(manager, settings_with_url, notifier=mock_notifier)
    status = DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=45.5)
    await monitor.check_device(status, now=1000.0)

    call_args, call_kwargs = mock_notifier.send.call_args
    buttons = call_kwargs.get("buttons")
    assert buttons == [{"text": "Открыть панель", "url": "https://hub.example.com/device/dev1"}]
    mock_notifier.send.reset_mock()

    # 9.2. public_url задан со слэшем на конце — слэш отсекается, URL корректен
    settings_slash = Settings(temp_alert_c=45.0, public_url="https://hub.example.com/")
    monitor_slash = AlertMonitor(manager, settings_slash, notifier=mock_notifier)
    await monitor_slash.check_device(status, now=2000.0)

    call_args, call_kwargs = mock_notifier.send.call_args
    buttons = call_kwargs.get("buttons")
    assert buttons == [{"text": "Открыть панель", "url": "https://hub.example.com/device/dev1"}]
    mock_notifier.send.reset_mock()

    # 9.3. public_url пуст — кнопки нет
    settings_empty = Settings(temp_alert_c=45.0, public_url="")
    monitor_empty = AlertMonitor(manager, settings_empty, notifier=mock_notifier)
    await monitor_empty.check_device(status, now=3000.0)

    call_args, call_kwargs = mock_notifier.send.call_args
    assert call_kwargs.get("buttons") is None


# 10. Отсутствие запросов к getUpdates / answerCallbackQuery (мок httpx)
@pytest.mark.asyncio
async def test_no_get_updates_or_callback_requests(manager: DeviceManager) -> None:
    calls = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        calls.append(url_str)
        if "getUpdates" in url_str:
            pytest.fail("TelegramNotifier must NEVER call getUpdates!")
        if "answerCallbackQuery" in url_str:
            pytest.fail("TelegramNotifier must NEVER call answerCallbackQuery!")
        if "sendMessage" in url_str:
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 100}})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle_request))

    notifier = TelegramNotifier(
        token="test_token_123",
        chat_id="12345",
        manager=manager,
        client=client,
    )

    # Проверка, что polling/callback методы полностью удалены
    assert not hasattr(notifier, "_poll_loop")
    assert not hasattr(notifier, "handle_update")
    assert not hasattr(notifier, "_handle_callback_query")
    assert not hasattr(notifier, "answer_callback")

    # start/stop не запускают задач и не делают вызовов getUpdates
    await notifier.start()
    ok = await notifier.send("Тест", buttons=[{"text": "Открыть панель", "url": "https://hub.example.com/device/dev1"}])
    assert ok is True
    await notifier.stop()

    assert len(calls) == 1
    assert "sendMessage" in calls[0]
    assert not any("getUpdates" in u for u in calls)
    assert not any("answerCallbackQuery" in u for u in calls)

    # Проверка вызова через AlertMonitor
    settings = Settings(
        telegram_token="test_token_123",
        telegram_chat_id="12345",
        temp_warn_c=40.0,
    )
    monitor = AlertMonitor(manager, settings, notifier=notifier)
    await monitor.start()
    await monitor.check_device(DeviceStatus(id="dev1", name="Pixel 6", online=True, temperature=41.0), now=1000.0)
    await monitor.stop()

    assert not any("getUpdates" in u for u in calls)
    assert not any("answerCallbackQuery" in u for u in calls)

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
