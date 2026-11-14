"""Тесты главного контроллера: неблокирующее обновление, фильтрация сетевых устройств."""

from __future__ import annotations

import queue
import threading
import time
from unittest.mock import MagicMock, patch

from desktop.adb_client import DeviceInfo
from desktop.binaries import BinaryInfo
from desktop.controller import AppController
from desktop.hub_client import HubClientError, HubDevice


def make_device(
    serial: str,
    state: str = "device",
    is_network: bool = False,
    model: str = "Pixel",
) -> DeviceInfo:
    """Вспомогательная функция для создания тестового DeviceInfo."""
    return DeviceInfo(
        serial=serial,
        state=state,
        state_display="Готов к работе" if state == "device" else "Не авторизован",
        is_network=is_network,
        connection_type="Сетевое (ADB TCP/IP)" if is_network else "USB",
        is_ready=(state == "device"),
        model=model,
    )


def test_controller_initialization_with_binaries():
    """Проверка инициализации контроллера и обнаружения путей."""
    fake_adb = BinaryInfo(name="adb", found=True, path="/bin/adb", version="1.0")
    fake_scrcpy = BinaryInfo(name="scrcpy", found=True, path="/bin/scrcpy", version="2.0")

    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=fake_scrcpy):
            ctrl = AppController()
            assert ctrl.adb_info.found is True
            assert ctrl.scrcpy_info.found is True
            assert ctrl.adb_client.adb_path == "/bin/adb"
            assert ctrl.scrcpy_manager.scrcpy_path == "/bin/scrcpy"


def test_controller_refresh_sync_success():
    """Проверка синхронного обновления списка устройств."""
    fake_adb = BinaryInfo(name="adb", found=True, path="/bin/adb")
    fake_scrcpy = BinaryInfo(name="scrcpy", found=True, path="/bin/scrcpy")

    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=fake_scrcpy):
            ctrl = AppController()

            dev1 = make_device("USB_PHONE_1")
            dev2 = make_device("USB_PHONE_2")
            ctrl.adb_client.list_devices = MagicMock(return_value=[dev1, dev2])

            devices, err = ctrl.refresh_devices_sync()
            assert err is None
            assert len(devices) == 2
            assert ctrl.get_device("USB_PHONE_1") is not None


def test_controller_refresh_sync_no_adb():
    """Проверка сообщения об ошибке при отсутствии ADB."""
    fake_adb = BinaryInfo(name="adb", found=False, error="ADB не найден")
    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=BinaryInfo(name="scrcpy", found=True)):
            ctrl = AppController()
            devices, err = ctrl.refresh_devices_sync()
            assert len(devices) == 0
            assert "ADB не найдена" in (err or "")


def test_controller_refresh_async_non_blocking():
    """Проверка неблокирующего обновления в фоновом потоке."""
    fake_adb = BinaryInfo(name="adb", found=True, path="/bin/adb")
    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=BinaryInfo(name="scrcpy", found=True)):
            ctrl = AppController()
            ctrl.adb_client.list_devices = MagicMock(return_value=[make_device("DEV1")])

            results: list[list[DeviceInfo]] = []

            def on_success(devs: list[DeviceInfo]) -> None:
                results.append(devs)

            started = ctrl.refresh_devices_async(on_success=on_success)
            assert started is True

            # Ждем завершения фонового потока
            for _ in range(20):
                if results:
                    break
                time.sleep(0.05)

            assert len(results) == 1
            assert results[0][0].serial == "DEV1"


def test_controller_refuse_unauthorized_and_offline():
    """Контроллер запрещает запуск scrcpy для неавторизованных или офлайн телефонов."""
    fake_adb = BinaryInfo(name="adb", found=True, path="/bin/adb")
    fake_scrcpy = BinaryInfo(name="scrcpy", found=True, path="/bin/scrcpy")

    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=fake_scrcpy):
            ctrl = AppController()

            unauth = make_device("UNAUTH_DEV", state="unauthorized")
            offline = make_device("OFFLINE_DEV", state="offline")

            ctrl._devices = [unauth, offline]

            # Попытка запуска для неавторизованного
            ok1, msg1 = ctrl.start_device_scrcpy("UNAUTH_DEV")
            assert ok1 is False
            assert "не авторизовано" in msg1
            assert "Разрешить отладку по USB" in msg1

            # Попытка запуска для офлайн устройства
            ok2, msg2 = ctrl.start_device_scrcpy("OFFLINE_DEV")
            assert ok2 is False
            assert "офлайне" in msg2


def test_controller_start_selected_usb_strictly_skips_network_devices():
    """КРИТИЧЕСКИЙ ТЕСТ: сетевые ADB-устройства никогда не открываются автоматически!"""
    fake_adb = BinaryInfo(name="adb", found=True, path="/bin/adb")
    fake_scrcpy = BinaryInfo(name="scrcpy", found=True, path="/bin/scrcpy")

    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=fake_scrcpy):
            ctrl = AppController()

            usb_ready = make_device("USB_DEV_OK", state="device", is_network=False)
            net_ready = make_device("192.168.1.55:5555", state="device", is_network=True)
            usb_unauth = make_device("USB_UNAUTH", state="unauthorized", is_network=False)

            ctrl._devices = [usb_ready, net_ready, usb_unauth]

            # Мокаем запуск в ScrcpyManager
            ctrl.scrcpy_manager.start = MagicMock(return_value=(True, None))

            # Передаем все 3 серийника в запуск выбранных
            started_count, messages = ctrl.start_selected_usb_devices(
                ["USB_DEV_OK", "192.168.1.55:5555", "USB_UNAUTH"]
            )

            # Только 1 (USB_DEV_OK) должен быть запущен!
            assert started_count == 1
            ctrl.scrcpy_manager.start.assert_called_once_with("USB_DEV_OK", title="Pixel")

            # Проверяем сообщения
            msg_text = " ".join(messages)
            assert "сетевые ADB-устройства не открываются автоматически" in msg_text
            assert "не готов" in msg_text or "статус" in msg_text
            assert "Запущено окно для" in msg_text


def test_controller_shutdown():
    """Проверка корректного закрытия всех процессов при вызове shutdown."""
    ctrl = AppController()
    ctrl.scrcpy_manager.stop_all = MagicMock(return_value=3)
    ctrl.shutdown()
    ctrl.scrcpy_manager.stop_all.assert_called_once()


def test_controller_hub_fetch_sync_success():
    """Проверка успешного синхронного получения устройств с удалённого сервера DroidHub."""
    fake_devices = [
        HubDevice(
            id="hub_dev_1",
            name="Pixel 8 Pro",
            online=True,
            battery=88,
            temperature=32.5,
            screen_on=True,
            charging=False,
        ),
        HubDevice(
            id="hub_dev_2",
            name="Samsung S23",
            online=False,
            battery=45,
            temperature=26.0,
            screen_on=False,
            charging=True,
        ),
    ]

    ctrl = AppController()
    with patch("desktop.controller.HubClient") as mock_client_cls:
        mock_instance = mock_client_cls.return_value
        mock_instance.list_devices.return_value = fake_devices

        devices, err = ctrl.fetch_hub_devices_sync("https://hub.example.com", "secret-token-123")

        mock_client_cls.assert_called_once_with(
            base_url="https://hub.example.com",
            token="secret-token-123",
            timeout=5.0,
        )
        assert err is None
        assert len(devices) == 2
        assert devices[0].id == "hub_dev_1"
        assert devices[0].battery == 88
        assert ctrl.hub_devices == fake_devices
        assert ctrl.get_hub_device("hub_dev_1") == fake_devices[0]
        assert ctrl.get_hub_device("non_existent") is None


def test_controller_hub_fetch_sync_error_and_token_not_exposed(caplog):
    """Проверка обработки ошибок сервера и защита от раскрытия токена в сообщении и логах."""
    import logging
    ctrl = AppController()
    secret_token = "ultra-secret-bearer-token-456"

    with caplog.at_level(logging.DEBUG):
        # 1. Ошибка HubClientError
        with patch("desktop.controller.HubClient") as mock_client_cls:
            mock_instance = mock_client_cls.return_value
            mock_instance.list_devices.side_effect = HubClientError(
                f"Ошибка авторизации с токеном {secret_token}: 401 Unauthorized"
            )

            devices, err = ctrl.fetch_hub_devices_sync("https://hub.example.com", secret_token)

            assert len(devices) == 0
            assert err is not None
            assert secret_token not in err
            assert "***" in err
            assert secret_token not in caplog.text

        caplog.clear()

        # 2. Непредвиденное исключение
        with patch("desktop.controller.HubClient") as mock_client_cls:
            mock_instance = mock_client_cls.return_value
            mock_instance.list_devices.side_effect = RuntimeError(
                f"Сетевой сбой при отправке {secret_token}"
            )

            devices, err = ctrl.fetch_hub_devices_sync("https://hub.example.com", secret_token)

            assert len(devices) == 0
            assert err is not None
            assert secret_token not in err
            assert "***" in err
            # Секретный токен не должен попадать в логи даже при RuntimeError
            assert secret_token not in caplog.text


def test_controller_hub_fetch_empty_url_or_token():
    """Проверка валидации: при пустом URL или токене запросы не выполняются."""
    ctrl = AppController()

    with patch("desktop.controller.HubClient") as mock_client_cls:
        # Пустой URL
        devs1, err1 = ctrl.fetch_hub_devices_sync("", "token")
        assert len(devs1) == 0
        assert "Укажите URL" in (err1 or "")
        mock_client_cls.assert_not_called()

        # Пустой токен
        devs2, err2 = ctrl.fetch_hub_devices_sync("https://hub.example.com", "   ")
        assert len(devs2) == 0
        assert "Укажите токен" in (err2 or "")
        mock_client_cls.assert_not_called()


def test_controller_hub_separation_from_usb_devices():
    """КРИТИЧЕСКИЙ ТЕСТ: локальные USB и сетевые устройства хаба не смешиваются и не объединяются по имени."""
    ctrl = AppController()

    # Добавляем локальное USB-устройство
    usb_dev = make_device("SAME_ID_999", state="device", is_network=False, model="Pixel Phone")
    ctrl._devices = [usb_dev]

    # Добавляем устройство хаба с тем же ID и похожим именем
    hub_dev = HubDevice(
        id="SAME_ID_999",
        name="Pixel Phone",
        online=True,
        battery=95,
        temperature=30.0,
        screen_on=True,
    )
    with patch("desktop.controller.HubClient") as mock_client_cls:
        mock_instance = mock_client_cls.return_value
        mock_instance.list_devices.return_value = [hub_dev]
        ctrl.fetch_hub_devices_sync("https://hub.example.com", "token123")

    # Списки строго разделены
    assert len(ctrl.devices) == 1
    assert isinstance(ctrl.devices[0], DeviceInfo)
    assert ctrl.devices[0].serial == "SAME_ID_999"

    assert len(ctrl.hub_devices) == 1
    assert isinstance(ctrl.hub_devices[0], HubDevice)
    assert ctrl.hub_devices[0].id == "SAME_ID_999"

    # get_device ищет только среди USB
    found_usb = ctrl.get_device("SAME_ID_999")
    assert isinstance(found_usb, DeviceInfo)
    assert found_usb.model == "Pixel Phone"

    # get_hub_device ищет только среди хаба
    found_hub = ctrl.get_hub_device("SAME_ID_999")
    assert isinstance(found_hub, HubDevice)
    assert found_hub.battery == 95

    # Устройство, существующее только в хабе, не видно в USB
    only_hub = HubDevice(id="HUB_ONLY_ID", name="Remote Pixel")
    with patch("desktop.controller.HubClient") as mock_client_cls:
        mock_instance = mock_client_cls.return_value
        mock_instance.list_devices.return_value = [hub_dev, only_hub]
        ctrl.fetch_hub_devices_sync("https://hub.example.com", "token123")

    assert ctrl.get_device("HUB_ONLY_ID") is None
    # Запуск USB scrcpy не находит устройства, существующие только в хабе
    started, msgs = ctrl.start_selected_usb_devices(["HUB_ONLY_ID"])
    assert started == 0
    assert any("не найдено" in m for m in msgs)


def test_controller_hub_fetch_async_non_blocking_and_queue():
    """Проверка асинхронного фонового обновления хаба через очередь и коллбэки."""
    ctrl = AppController()
    fake_devices = [
        HubDevice(id="async_dev", name="Async Phone", online=True, battery=60, temperature=28.5, screen_on=False)
    ]

    with patch("desktop.controller.HubClient") as mock_client_cls:
        mock_instance = mock_client_cls.return_value
        mock_instance.list_devices.return_value = fake_devices

        q = queue.Queue()
        success_calls = []

        def on_success(devs, rid):
            success_calls.append((devs, rid))

        req_id = ctrl.fetch_hub_devices_async(
            base_url="https://hub.example.com",
            token="token123",
            result_queue=q,
            on_success=on_success,
        )

        assert req_id > 0

        # Ждем результат из очереди
        item = q.get(timeout=2.0)
        res_rid, res_devs, res_err = item
        assert res_rid == req_id
        assert len(res_devs) == 1
        assert res_err is None

        # Проверяем вызов коллбэка
        for _ in range(20):
            if success_calls:
                break
            time.sleep(0.05)
        assert len(success_calls) == 1
        assert success_calls[0][1] == req_id


def test_controller_hub_fetch_async_ignores_stale_requests():
    """Устаревшие фоновые запросы должны игнорироваться и не перетирать актуальные данные."""
    ctrl = AppController()

    ready_event = threading.Event()
    finish_event = threading.Event()

    def slow_list_devices():
        ready_event.set()
        finish_event.wait(timeout=2.0)
        return [HubDevice(id="stale_dev", name="Stale")]

    with patch("desktop.controller.HubClient") as mock_client_cls:
        mock_instance = mock_client_cls.return_value
        mock_instance.list_devices.side_effect = slow_list_devices

        q = queue.Queue()
        stale_calls = []

        # Запускаем медленный запрос #1
        rid1 = ctrl.fetch_hub_devices_async(
            "https://hub.example.com",
            "token1",
            result_queue=q,
            on_success=lambda devs, rid: stale_calls.append(rid),
        )

        assert ready_event.wait(timeout=1.0)

        # Симулируем новый запрос #2 (он увеличивает request_id)
        with ctrl._hub_refresh_lock:
            ctrl._hub_request_id += 1

        # Разрешаем запросу #1 завершиться
        finish_event.set()
        time.sleep(0.2)

        # Очередь должна быть пуста, коллбэк запроса #1 не вызван
        assert q.empty()
        assert len(stale_calls) == 0
        # КРИТИЧНО: состояние контроллера НЕ должно обновиться данными от отменённого запроса!
        assert len(ctrl.hub_devices) == 0
        assert ctrl.get_hub_device("stale_dev") is None


def test_controller_hub_shutdown_invalidates_in_flight():
    """При вызове shutdown запросы в полете признаются устаревшими."""
    ctrl = AppController()
    ctrl.scrcpy_manager.stop_all = MagicMock(return_value=0)

    ready_event = threading.Event()
    finish_event = threading.Event()

    def slow_list():
        ready_event.set()
        finish_event.wait(timeout=2.0)
        return [HubDevice(id="dev_shutdown", name="Shutdown Dev")]

    with patch("desktop.controller.HubClient") as mock_client_cls:
        mock_instance = mock_client_cls.return_value
        mock_instance.list_devices.side_effect = slow_list

        q = queue.Queue()
        ctrl.fetch_hub_devices_async("https://hub.example.com", "token", result_queue=q)

        assert ready_event.wait(timeout=1.0)
        # Вызываем shutdown
        ctrl.shutdown()
        assert ctrl.scrcpy_manager.stop_all.called

        # Разрешаем завершиться потоку
        finish_event.set()
        time.sleep(0.2)

        # Результат отброшен, состояние контроллера не загрязнено
        assert q.empty()
        assert len(ctrl.hub_devices) == 0
        assert ctrl.get_hub_device("dev_shutdown") is None


def test_controller_hub_fetch_async_stale_preserves_previous_active_devices():
    """Устаревший фоновый запрос не затирает ранее успешно полученные устройства."""
    ctrl = AppController()
    old_dev = HubDevice(id="active_dev", name="Active Phone")
    ctrl._hub_devices = [old_dev]

    ready_event = threading.Event()
    finish_event = threading.Event()

    def slow_list():
        ready_event.set()
        finish_event.wait(timeout=2.0)
        return [HubDevice(id="stale_dev", name="Stale Phone")]

    with patch("desktop.controller.HubClient") as mock_client_cls:
        mock_instance = mock_client_cls.return_value
        mock_instance.list_devices.side_effect = slow_list

        q = queue.Queue()
        rid = ctrl.fetch_hub_devices_async("https://hub.example.com", "token", result_queue=q)
        assert ready_event.wait(timeout=1.0)

        # Новый запрос или отмена
        with ctrl._hub_refresh_lock:
            ctrl._hub_request_id += 1

        finish_event.set()
        time.sleep(0.2)

        assert q.empty()
        # Старые активные устройства сохранены!
        assert ctrl.hub_devices == [old_dev]
