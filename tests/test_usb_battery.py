"""
Тесты для USBBatteryBroker (брокер чтения заряда через авторизованный USB-мост).
"""
import asyncio
import pytest
from hub.devices.usb_battery import USBBatteryBroker, USBBatteryUnavailable
from hub.devices.usb_presence import USBPresenceRegistry


class FakeClock:
    """Управляемые монотонные часы для тестов времени и TTL."""

    def __init__(self, start: float = 1000.0) -> None:
        self.time = start

    def __call__(self) -> float:
        return self.time

    def advance(self, seconds: float) -> None:
        self.time += seconds


async def test_normal_cycle():
    """Штатный цикл: чтение -> опрос -> успешное завершение -> выдача результата."""
    registry = USBPresenceRegistry(["phone1"])
    session_id = registry.open_session("bridge-1")
    registry.apply_snapshot("bridge-1", session_id, ["phone1"])

    broker = USBBatteryBroker(registry, check_interval=0.01)

    read_task = asyncio.create_task(broker.read("phone1", timeout=5.0))
    await asyncio.sleep(0.005)

    assert broker.is_pending("phone1") is True

    # Опрос (poll)
    poll_result = broker.poll("bridge-1", session_id)
    assert poll_result is not None
    assert set(poll_result.keys()) == {"request_id", "device_id"}
    assert poll_result["device_id"] == "phone1"
    req_id = poll_result["request_id"]
    assert isinstance(req_id, str) and len(req_id) == 32

    # Выдача максимум один раз: повторный poll не возвращает этот же запрос
    assert broker.poll("bridge-1", session_id) is None

    # Завершение (complete)
    battery_output = "Current Battery Service state:\n  level: 87\n  scale: 100\n"
    completed = broker.complete(
        "bridge-1", session_id, req_id, output=battery_output
    )
    assert completed is True

    # Проверка получения вывода dumpsys battery
    result = await read_task
    assert result == battery_output
    assert broker.is_pending("phone1") is False

    # Повторный complete для уже завершённого запроса отклоняется
    assert (
        broker.complete("bridge-1", session_id, req_id, output=battery_output)
        is False
    )

    # После завершения можно запустить новый запрос для того же устройства
    read_task2 = asyncio.create_task(broker.read("phone1", timeout=5.0))
    await asyncio.sleep(0.005)
    poll_result2 = broker.poll("bridge-1", session_id)
    assert poll_result2 is not None
    assert poll_result2["request_id"] != req_id
    broker.complete(
        "bridge-1", session_id, poll_result2["request_id"], output="level: 100"
    )
    assert await read_task2 == "level: 100"


async def test_command_failure():
    """Отказ команды (output=None) завершает read исключением USBBatteryUnavailable."""
    registry = USBPresenceRegistry(["phone1"])
    session_id = registry.open_session("bridge-1")
    registry.apply_snapshot("bridge-1", session_id, ["phone1"])

    broker = USBBatteryBroker(registry, check_interval=0.01)

    read_task = asyncio.create_task(broker.read("phone1", timeout=5.0))
    await asyncio.sleep(0.005)

    poll_result = broker.poll("bridge-1", session_id)
    assert poll_result is not None

    # None output означает неудачу выполнения
    ok = broker.complete(
        "bridge-1", session_id, poll_result["request_id"], output=None
    )
    assert ok is True

    with pytest.raises(USBBatteryUnavailable) as exc_info:
        await read_task

    assert "не выполнена" in str(exc_info.value).lower() or "ошибк" in str(exc_info.value).lower()
    assert broker.is_pending("phone1") is False


async def test_conflict_single_pending_request_per_phone():
    """Конфликт: только один незавершённый запрос на телефон."""
    registry = USBPresenceRegistry(["phone1"])
    session_id = registry.open_session("bridge-1")
    registry.apply_snapshot("bridge-1", session_id, ["phone1"])

    broker = USBBatteryBroker(registry, check_interval=0.01)

    task1 = asyncio.create_task(broker.read("phone1", timeout=5.0))
    await asyncio.sleep(0.005)

    # Вторая попытка для того же телефона должна немедленно вызвать USBBatteryUnavailable
    with pytest.raises(USBBatteryUnavailable) as exc_info:
        await broker.read("phone1", timeout=5.0)

    assert "уже выполняется" in str(exc_info.value).lower() or "запрос" in str(exc_info.value).lower()

    # Первый запрос остаётся активным и может штатно завершиться
    poll_result = broker.poll("bridge-1", session_id)
    assert poll_result is not None
    broker.complete(
        "bridge-1", session_id, poll_result["request_id"], output="level: 50"
    )
    assert await task1 == "level: 50"


async def test_expiration_timeout():
    """Истечение таймаута read() освобождает pending, а запоздалый результат отклоняется."""
    registry = USBPresenceRegistry(["phone1"])
    session_id = registry.open_session("bridge-1")
    registry.apply_snapshot("bridge-1", session_id, ["phone1"])

    broker = USBBatteryBroker(registry, check_interval=0.01)

    task = asyncio.create_task(broker.read("phone1", timeout=0.04))
    await asyncio.sleep(0.005)

    poll_result = broker.poll("bridge-1", session_id)
    assert poll_result is not None
    req_id = poll_result["request_id"]

    # Ждём истечения таймаута
    with pytest.raises(USBBatteryUnavailable) as exc_info:
        await task

    assert "превышено время ожидания" in str(exc_info.value).lower() or "таймаут" in str(exc_info.value).lower()
    assert broker.is_pending("phone1") is False

    # Запоздалый результат от моста отклоняется
    late_ok = broker.complete(
        "bridge-1", session_id, req_id, output="level: 100"
    )
    assert late_ok is False


async def test_expiration_registry_ttl():
    """Истечение TTL в реестре приводит к отмене запроса при периодической проверке."""
    clock = FakeClock(100.0)
    registry = USBPresenceRegistry(["phone1"], ttl=10.0, clock=clock)
    session_id = registry.open_session("bridge-1")
    registry.apply_snapshot("bridge-1", session_id, ["phone1"])

    broker = USBBatteryBroker(registry, check_interval=0.01)

    task = asyncio.create_task(broker.read("phone1", timeout=5.0))
    await asyncio.sleep(0.005)

    # Продвигаем часы на 11 секунд (TTL истёк)
    clock.advance(11.0)

    with pytest.raises(USBBatteryUnavailable) as exc_info:
        await task

    assert "разорвана" in str(exc_info.value).lower() or "заменена" in str(exc_info.value).lower() or "недоступно" in str(exc_info.value).lower()
    assert broker.is_pending("phone1") is False


async def test_session_replacement():
    """Замена сессии (переподключение моста) инвалидирует запрос и не переносит его на новую сессию."""
    registry = USBPresenceRegistry(["phone1"])
    sess_1 = registry.open_session("bridge-1", "sess-1")
    registry.apply_snapshot("bridge-1", sess_1, ["phone1"])

    broker = USBBatteryBroker(registry, check_interval=0.01)

    task = asyncio.create_task(broker.read("phone1", timeout=5.0))
    await asyncio.sleep(0.005)

    poll_result = broker.poll("bridge-1", "sess-1")
    assert poll_result is not None
    req_id = poll_result["request_id"]

    # Мост переподключается с новой сессией
    sess_2 = registry.open_session("bridge-1", "sess-2")
    registry.apply_snapshot("bridge-1", sess_2, ["phone1"])

    # Чтение быстро завершается ошибкой из-за смены сессии
    with pytest.raises(USBBatteryUnavailable) as exc_info:
        await task

    assert "разорвана" in str(exc_info.value).lower() or "заменена" in str(exc_info.value).lower()

    # Запрос не перенесён на новую сессию sess-2
    assert broker.poll("bridge-1", "sess-2") is None

    # Запоздалый результат от sess-1 отклоняется
    assert (
        broker.complete("bridge-1", "sess-1", req_id, output="level: 100")
        is False
    )

    # Результат с sess-2 для старого запроса также отклоняется
    assert (
        broker.complete("bridge-1", "sess-2", req_id, output="level: 100")
        is False
    )


async def test_cancellation():
    """Отмена корутины read() освобождает pending, а late result отклоняется."""
    registry = USBPresenceRegistry(["phone1"])
    session_id = registry.open_session("bridge-1")
    registry.apply_snapshot("bridge-1", session_id, ["phone1"])

    broker = USBBatteryBroker(registry, check_interval=0.01)

    task = asyncio.create_task(broker.read("phone1", timeout=5.0))
    await asyncio.sleep(0.005)
    assert broker.is_pending("phone1") is True

    poll_result = broker.poll("bridge-1", session_id)
    assert poll_result is not None
    req_id = poll_result["request_id"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert broker.is_pending("phone1") is False

    # Запоздалый ответ отклоняется
    assert (
        broker.complete("bridge-1", session_id, req_id, output="level: 80")
        is False
    )

    # Телефон свободен для нового запроса
    task2 = asyncio.create_task(broker.read("phone1", timeout=5.0))
    await asyncio.sleep(0.005)
    poll2 = broker.poll("bridge-1", session_id)
    assert poll2 is not None
    broker.complete(
        "bridge-1", session_id, poll2["request_id"], output="level: 99"
    )
    assert await task2 == "level: 99"


async def test_foreign_result():
    """Чужой результат (другой мост, другая сессия, неизвестный request_id) отклоняется."""
    registry = USBPresenceRegistry(["phone1"])
    session_id = registry.open_session("bridge-1", "sess-1")
    registry.apply_snapshot("bridge-1", "sess-1", ["phone1"])

    broker = USBBatteryBroker(registry, check_interval=0.01)

    task = asyncio.create_task(broker.read("phone1", timeout=5.0))
    await asyncio.sleep(0.005)

    poll_result = broker.poll("bridge-1", "sess-1")
    assert poll_result is not None
    req_id = poll_result["request_id"]

    # Чужой мост
    assert (
        broker.complete(
            "bridge-2", "sess-1", req_id, output="level: 100"
        )
        is False
    )

    # Чужая сессия
    assert (
        broker.complete(
            "bridge-1", "other-sess", req_id, output="level: 100"
        )
        is False
    )

    # Неизвестный request_id
    assert (
        broker.complete(
            "bridge-1", "sess-1", "00000000000000000000000000000000", output="level: 100"
        )
        is False
    )

    # Исходный запрос не пострадал и завершается корректным вызовом
    assert (
        broker.complete("bridge-1", "sess-1", req_id, output="level: 42")
        is True
    )
    assert await task == "level: 42"


async def test_device_unavailable():
    """Запрос для неподключенного устройства сразу вызывает USBBatteryUnavailable."""
    registry = USBPresenceRegistry(["phone1"])
    broker = USBBatteryBroker(registry)

    with pytest.raises(USBBatteryUnavailable) as exc_info:
        await broker.read("phone1")
    assert "недоступно" in str(exc_info.value).lower()

    with pytest.raises(USBBatteryUnavailable) as exc_info:
        await broker.read("unknown_phone")
    assert "недоступно" in str(exc_info.value).lower()


async def test_close_broker():
    """Закрытие брокера отменяет все незавершённые запросы и освобождает pending."""
    registry = USBPresenceRegistry(["phone1", "phone2"])
    session_id = registry.open_session("bridge-1")
    registry.apply_snapshot("bridge-1", session_id, ["phone1", "phone2"])

    broker = USBBatteryBroker(registry, check_interval=0.01)

    task1 = asyncio.create_task(broker.read("phone1", timeout=5.0))
    task2 = asyncio.create_task(broker.read("phone2", timeout=5.0))
    await asyncio.sleep(0.005)

    assert broker.is_pending("phone1") is True
    assert broker.is_pending("phone2") is True

    broker.close()
    assert broker.is_closed is True
    assert broker.is_pending("phone1") is False
    assert broker.is_pending("phone2") is False

    with pytest.raises(USBBatteryUnavailable):
        await task1
    with pytest.raises(USBBatteryUnavailable):
        await task2

    # После закрытия новые операции отклоняются
    with pytest.raises(USBBatteryUnavailable):
        await broker.read("phone1")

    assert broker.poll("bridge-1", session_id) is None
    assert broker.complete("bridge-1", session_id, "some-id", output="ok") is False


async def test_poll_cleans_up_broken_session():
    """poll() обнаруживает разорванную сессию в реестре и отменяет запрос."""
    registry = USBPresenceRegistry(["phone1"])
    session_id = registry.open_session("bridge-1")
    registry.apply_snapshot("bridge-1", session_id, ["phone1"])

    broker = USBBatteryBroker(registry, check_interval=0.01)

    task = asyncio.create_task(broker.read("phone1", timeout=5.0))
    await asyncio.sleep(0.005)

    # Закрываем сессию в реестре до вызова poll
    registry.close_session("bridge-1", session_id)

    # poll видит, что сессия недействительна в registry.resolve
    assert broker.poll("bridge-1", session_id) is None

    with pytest.raises(USBBatteryUnavailable):
        await task


async def test_multiple_devices_fifo_poll():
    """Несколько устройств на одном мосту опрашиваются по очереди."""
    registry = USBPresenceRegistry(["phone1", "phone2"])
    session_id = registry.open_session("bridge-1")
    registry.apply_snapshot("bridge-1", session_id, ["phone1", "phone2"])

    broker = USBBatteryBroker(registry, check_interval=0.01)

    t1 = asyncio.create_task(broker.read("phone1", timeout=5.0))
    t2 = asyncio.create_task(broker.read("phone2", timeout=5.0))
    await asyncio.sleep(0.005)

    p1 = broker.poll("bridge-1", session_id)
    assert p1 is not None and p1["device_id"] == "phone1"

    p2 = broker.poll("bridge-1", session_id)
    assert p2 is not None and p2["device_id"] == "phone2"

    assert broker.poll("bridge-1", session_id) is None

    broker.complete("bridge-1", session_id, p1["request_id"], output="level: 10")
    broker.complete("bridge-1", session_id, p2["request_id"], output="level: 20")

    assert await t1 == "level: 10"
    assert await t2 == "level: 20"
