"""
Тесты реестра активных USB-привязок DroidHub (USBPresenceRegistry).
"""
import pytest
from hub.devices.usb_presence import USBPresence, USBPresenceRegistry


class FakeClock:
    """Управляемые фальшивые монотонные часы для тестов времени и TTL."""

    def __init__(self, start: float = 1000.0) -> None:
        self.time = start

    def __call__(self) -> float:
        return self.time

    def advance(self, seconds: float) -> None:
        self.time += seconds


def test_basic_registration_and_resolve():
    """Проверка базовой регистрации устройства и поиска через resolve."""
    clock = FakeClock(100.0)
    registry = USBPresenceRegistry(["phone1", "phone2"], ttl=12.0, clock=clock)

    assert registry.resolve("phone1") is None

    # Сессия моста должна быть предварительно авторизована через open_session
    s_id = registry.open_session("bridge-a", "sess-1")
    assert s_id == "sess-1"

    ok = registry.apply_snapshot("bridge-a", "sess-1", ["phone1"])
    assert ok is True

    presence = registry.resolve("phone1")
    assert presence is not None
    assert presence.bridge_id == "bridge-a"
    assert presence.session_id == "sess-1"
    assert presence.device_id == "phone1"
    assert presence.generation == 1

    # Датакласс не является частичным итератором: обращение только через явные атрибуты
    with pytest.raises(TypeError):
        iter(presence)


def test_ttl_expiration_with_fake_clock():
    """Истечение TTL автоматически снимает USB-привязку и resolve возвращает None."""
    clock = FakeClock(100.0)
    registry = USBPresenceRegistry(["phone1"], ttl=12.0, clock=clock)

    registry.open_session("bridge-a", "sess-1")
    registry.apply_snapshot("bridge-a", "sess-1", ["phone1"])
    assert registry.resolve("phone1") is not None

    # Продвижение часов на 11.9 с — устройство всё ещё активно
    clock.advance(11.9)
    assert registry.resolve("phone1") is not None
    assert "phone1" in registry.active_device_ids()

    # Продвижение часов ещё на 0.2 с (итого 12.1 с) — TTL истёк
    clock.advance(0.2)
    assert registry.resolve("phone1") is None
    assert registry.active_device_ids() == set()


def test_heartbeat_extends_ttl():
    """Heartbeat продлевает срок жизни сессии и её устройств."""
    clock = FakeClock(100.0)
    registry = USBPresenceRegistry(["phone1", "phone2"], ttl=12.0, clock=clock)

    registry.open_session("bridge-a", "sess-1")
    registry.apply_snapshot("bridge-a", "sess-1", ["phone1", "phone2"])

    # Через 8 секунд шлём heartbeat
    clock.advance(8.0)
    ok = registry.heartbeat("bridge-a", "sess-1")
    assert ok is True

    # Прошло 15 секунд от старта, но только 7 секунд от heartbeat — устройства активны
    clock.advance(7.0)
    assert registry.resolve("phone1") is not None
    assert registry.resolve("phone2") is not None

    # Прошло ещё 6 секунд (всего 13 с после heartbeat) — истекли
    clock.advance(6.0)
    assert registry.resolve("phone1") is None
    assert registry.resolve("phone2") is None

    # Heartbeat на уже истекшей сессии возвращает False
    assert registry.heartbeat("bridge-a", "sess-1") is False


def test_conflict_atomically_rejects_entire_snapshot():
    """Конфликт захвата устройства другой активной сессией атомарно отвергает весь снимок."""
    clock = FakeClock(100.0)
    registry = USBPresenceRegistry(["phone1", "phone2", "phone3"], ttl=12.0, clock=clock)

    # Сессия 1 захватывает phone1
    registry.open_session("bridge-1", "sess-1")
    ok1 = registry.apply_snapshot("bridge-1", "sess-1", ["phone1"])
    assert ok1 is True
    pres1 = registry.resolve("phone1")
    assert pres1 is not None
    assert pres1.session_id == "sess-1"

    # Сессия 2 пытается захватить phone2 и конфликтный phone1
    registry.open_session("bridge-2", "sess-2")
    ok2 = registry.apply_snapshot("bridge-2", "sess-2", ["phone2", "phone1"])
    assert ok2 is False  # Весь снимок отвергнут!

    # phone2 НЕ должен быть зарегистрирован за sess-2 (атомарность)
    assert registry.resolve("phone2") is None
    # phone1 по-прежнему принадлежит sess-1
    presence1 = registry.resolve("phone1")
    assert presence1 is not None
    assert presence1.session_id == "sess-1"


def test_conflict_cleared_after_ttl_expires():
    """После истечения TTL предыдущей сессии устройство может быть захвачено новой сессией."""
    clock = FakeClock(100.0)
    registry = USBPresenceRegistry(["phone1"], ttl=12.0, clock=clock)

    registry.open_session("bridge-1", "sess-1")
    registry.apply_snapshot("bridge-1", "sess-1", ["phone1"])
    pres1 = registry.resolve("phone1")
    assert pres1 is not None
    assert pres1.session_id == "sess-1"

    # Проходит 13 секунд (TTL sess-1 истёк)
    clock.advance(13.0)
    assert registry.resolve("phone1") is None

    # Теперь sess-2 может успешно захватить phone1
    registry.open_session("bridge-2", "sess-2")
    ok = registry.apply_snapshot("bridge-2", "sess-2", ["phone1"])
    assert ok is True
    pres2 = registry.resolve("phone1")
    assert pres2 is not None
    assert pres2.session_id == "sess-2"


def test_disappearance_and_empty_snapshot():
    """Пропадание устройства из снимка или пустой снимок освобождает записи."""
    clock = FakeClock(100.0)
    registry = USBPresenceRegistry(["phone1", "phone2"], ttl=12.0, clock=clock)

    registry.open_session("bridge-a", "sess-1")
    registry.apply_snapshot("bridge-a", "sess-1", ["phone1", "phone2"])
    assert registry.resolve("phone1") is not None
    assert registry.resolve("phone2") is not None

    # phone2 отключили от USB — снимок содержит только phone1
    registry.apply_snapshot("bridge-a", "sess-1", ["phone1"])
    assert registry.resolve("phone1") is not None
    assert registry.resolve("phone2") is None

    # Пустой снимок освобождает все устройства сессии
    registry.apply_snapshot("bridge-a", "sess-1", [])
    assert registry.resolve("phone1") is None
    assert registry.resolve("phone2") is None


def test_close_session_releases_devices():
    """Явное закрытие сессии освобождает все её записи."""
    clock = FakeClock(100.0)
    registry = USBPresenceRegistry(["phone1", "phone2"], ttl=12.0, clock=clock)

    registry.open_session("bridge-a", "sess-1")
    registry.apply_snapshot("bridge-a", "sess-1", ["phone1", "phone2"])
    assert registry.resolve("phone1") is not None

    ok = registry.close_session("bridge-a", "sess-1")
    assert ok is True
    assert registry.resolve("phone1") is None
    assert registry.resolve("phone2") is None

    # Повторное закрытие уже закрытой сессии
    assert registry.close_session("bridge-a", "sess-1") is False


def test_reconnection_revokes_old_session_and_increments_generation():
    """Повторное открытие с тем же bridge_id отзывает старую сессию и инкрементирует поколение."""
    clock = FakeClock(100.0)
    registry = USBPresenceRegistry(["phone1", "phone2"], ttl=12.0, clock=clock)

    # Первое подключение
    s1 = registry.open_session("bridge-1", "sess-1")
    assert s1 == "sess-1"
    assert registry.get_generation("bridge-1") == 1
    registry.apply_snapshot("bridge-1", "sess-1", ["phone1"])
    pres1 = registry.resolve("phone1")
    assert pres1 is not None
    assert pres1.session_id == "sess-1"

    # Переподключение того же моста с новой сессией
    s2 = registry.open_session("bridge-1", "sess-2")
    assert s2 == "sess-2"
    assert registry.get_generation("bridge-1") == 2

    # Устройства старой сессии освобождены до нового снимка
    assert registry.resolve("phone1") is None

    # Новая сессия регистрирует phone2
    registry.apply_snapshot("bridge-1", "sess-2", ["phone2"])
    presence2 = registry.resolve("phone2")
    assert presence2 is not None
    assert presence2.session_id == "sess-2"
    assert presence2.generation == 2

    # Запоздалый снимок от старой сессии sess-1 отклоняется
    late_ok = registry.apply_snapshot("bridge-1", "sess-1", ["phone1"])
    assert late_ok is False
    assert registry.resolve("phone1") is None

    # Запоздалое закрытие от старой сессии sess-1 не трогает записи sess-2
    late_close = registry.close_session("bridge-1", "sess-1")
    assert late_close is False
    assert registry.resolve("phone2") is not None
    assert registry.resolve("phone2").session_id == "sess-2"

    # Запоздалый heartbeat от sess-1 отклоняется
    late_hb = registry.heartbeat("bridge-1", "sess-1")
    assert late_hb is False


def test_explicit_session_revocation():
    """Явный отзыв сессии освобождает устройства и блокирует дальнейшие операции."""
    clock = FakeClock(100.0)
    registry = USBPresenceRegistry(["phone1"], ttl=12.0, clock=clock)

    registry.open_session("bridge-1", "sess-1")
    registry.apply_snapshot("bridge-1", "sess-1", ["phone1"])
    assert registry.resolve("phone1") is not None

    ok = registry.revoke_session("sess-1")
    assert ok is True
    assert registry.resolve("phone1") is None

    # Любой дальнейший снимок от отозванной сессии отвергается
    assert registry.apply_snapshot("bridge-1", "sess-1", ["phone1"]) is False


def test_allowlist_filters_unknown_devices():
    """Реестр хранит только устройства из начального allowlist."""
    clock = FakeClock(100.0)
    registry = USBPresenceRegistry(["phone1"], ttl=12.0, clock=clock)

    assert registry.is_allowed("phone1") is True
    assert registry.is_allowed("unknown_usb_gadget") is False

    registry.open_session("bridge-1", "sess-1")
    # Снимок содержит разрешённое устройство и неизвестный гаджет
    ok = registry.apply_snapshot("bridge-1", "sess-1", ["phone1", "unknown_usb_gadget"])
    assert ok is True

    # Разрешённое устройство зарегистрировано
    assert registry.resolve("phone1") is not None
    # Неизвестное устройство не сохраняется в реестре
    assert registry.resolve("unknown_usb_gadget") is None


def test_cleanup_expired_limits_storage():
    """Очистка просроченных записей ограничивает хранение в памяти."""
    clock = FakeClock(100.0)
    registry = USBPresenceRegistry(["phone1", "phone2"], ttl=12.0, clock=clock)

    registry.open_session("bridge-1", "sess-1")
    registry.open_session("bridge-2", "sess-2")
    registry.apply_snapshot("bridge-1", "sess-1", ["phone1"])
    registry.apply_snapshot("bridge-2", "sess-2", ["phone2"])

    assert len(registry.active_device_ids()) == 2

    clock.advance(15.0)  # Оба устройства просрочены
    removed = registry.cleanup_expired()
    assert removed == 2
    assert len(registry.active_device_ids()) == 0


def test_same_session_updates_without_conflict():
    """Повторный снимок от той же сессии не вызывает конфликта с самой собой и обновляет TTL."""
    clock = FakeClock(100.0)
    registry = USBPresenceRegistry(["phone1", "phone2"], ttl=12.0, clock=clock)

    registry.open_session("bridge-1", "sess-1")
    registry.apply_snapshot("bridge-1", "sess-1", ["phone1"])

    clock.advance(6.0)
    # Та же сессия расширяет снимок до phone1 и phone2
    ok = registry.apply_snapshot("bridge-1", "sess-1", ["phone1", "phone2"])
    assert ok is True

    clock.advance(8.0)  # Прошло 14 с от старта, но только 8 с от второго снимка
    assert registry.resolve("phone1") is not None
    assert registry.resolve("phone2") is not None


def test_apply_snapshot_without_open_session_rejected():
    """Снимок от неизвестной или неоткрытой сессии отвергается и не создаёт сессию."""
    clock = FakeClock(100.0)
    registry = USBPresenceRegistry(["phone1"], ttl=12.0, clock=clock)

    # Попытка отправить снимок без предварительного open_session
    assert registry.apply_snapshot("bridge-1", "unopened-sess", ["phone1"]) is False
    assert registry.resolve("phone1") is None
    assert registry.get_active_session("bridge-1") is None

    # Открытие сессии, отправка снимка, затем истечение TTL
    registry.open_session("bridge-1", "sess-1")
    assert registry.apply_snapshot("bridge-1", "sess-1", ["phone1"]) is True
    assert registry.resolve("phone1") is not None

    clock.advance(13.0)
    # После истечения сессии повторный снимок не должен воскрешать сессию
    assert registry.apply_snapshot("bridge-1", "sess-1", ["phone1"]) is False
    assert registry.resolve("phone1") is None


def test_cleanup_clears_bridge_to_session_on_revocation():
    """cleanup_expired() корректно очищает active_session моста при отзыве сессии."""
    clock = FakeClock(100.0)
    registry = USBPresenceRegistry(["phone1"], ttl=12.0, clock=clock)

    registry.open_session("bridge-1", "sess-1")
    registry.apply_snapshot("bridge-1", "sess-1", ["phone1"])
    assert registry.get_active_session("bridge-1") == "sess-1"

    # Отзываем сессию до истечения TTL
    registry.revoke_session("sess-1")
    assert registry.get_active_session("bridge-1") is None


def test_reconnect_memory_cleanup_no_leak():
    """Повторные переподключения моста не приводят к утечке памяти."""
    clock = FakeClock(100.0)
    registry = USBPresenceRegistry(["phone1"], ttl=12.0, clock=clock)

    for i in range(100):
        registry.open_session("bridge-1", f"sess-{i}")

    # В активных сессиях остаётся только последняя сессия
    assert len(registry._sessions) == 1
    assert registry.get_generation("bridge-1") == 100
    assert registry.get_active_session("bridge-1") == "sess-99"


def test_no_redundant_aliases_or_guessing():
    """Проверка отсутствия избыточных псевдонимов и полиморфизма."""
    registry = USBPresenceRegistry(["phone1"])
    assert not hasattr(registry, "snapshot")
    assert not hasattr(registry, "update_snapshot")
    assert not hasattr(registry, "close")
    assert not hasattr(registry, "get")
    assert not hasattr(registry, "_normalize_bridge_and_session")
