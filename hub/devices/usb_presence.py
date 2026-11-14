"""
Реестр активных USB-привязок устройств DroidHub.

In-memory учёт присутствия устройств, подключённых к локальным USB-мостам.
Используется сервером для маршрутизации и выбора транспорта (USB-мост vs прямое подключение).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Callable, Iterable

__all__ = [
    "USBPresence",
    "USBPresenceRegistry",
]


@dataclass(frozen=True)
class USBPresence:
    """Активная USB-привязка устройства к локальному мосту."""

    bridge_id: str
    session_id: str
    device_id: str = ""
    generation: int = 1


@dataclass
class _DeviceRecord:
    """Внутренняя запись о привязке устройства."""

    device_id: str
    bridge_id: str
    session_id: str
    generation: int
    expires_at: float


@dataclass
class _SessionRecord:
    """Внутренняя запись об авторизованной сессии моста."""

    bridge_id: str
    session_id: str
    generation: int
    expires_at: float
    revoked: bool = False


class USBPresenceRegistry:
    """
    Компактный in-memory реестр активных USB-привязок устройств.

    Хранит только разрешённые ID устройств (allowlist), привязанные к bridge_id и opaque session_id.
    Поддерживает инъекцию монотонных часов и TTL, атомарные снимки, разрешение конфликтов,
    продление через heartbeat и защиту от устаревших сессий при переподключении.
    """

    def __init__(
        self,
        allowed_device_ids: Iterable[str],
        *,
        ttl: float = 12.0,
        clock: Callable[[], float] | None = None,
        time_func: Callable[[], float] | None = None,
    ) -> None:
        self._allowed_device_ids: frozenset[str] = frozenset(allowed_device_ids)
        self._ttl: float = float(ttl)
        self._clock: Callable[[], float] = clock or time_func or time.monotonic

        # device_id -> _DeviceRecord
        self._devices: dict[str, _DeviceRecord] = {}
        # session_id -> _SessionRecord
        self._sessions: dict[str, _SessionRecord] = {}
        # bridge_id -> active session_id
        self._bridge_to_session: dict[str, str] = {}
        # bridge_id -> последнее поколение (int)
        self._bridge_generations: dict[str, int] = {}

    @property
    def ttl(self) -> float:
        """Время жизни записи (в секундах)."""
        return self._ttl

    @property
    def allowed_device_ids(self) -> frozenset[str]:
        """Разрешённые ID устройств."""
        return self._allowed_device_ids

    def is_allowed(self, device_id: str) -> bool:
        """Проверяет, входит ли ID устройства в allowlist."""
        return device_id in self._allowed_device_ids

    def open_session(self, bridge_id: str, session_id: str | None = None) -> str:
        """
        Открывает новую сессию моста bridge_id.

        Если для bridge_id уже была активная сессия с другим session_id,
        старая сессия явно отзывается, её устройства освобождаются,
        а поколение моста инкрементируется.
        """
        now = self._clock()
        self.cleanup_expired()

        if session_id is None:
            session_id = uuid.uuid4().hex

        # Если session_id уже использовался другим мостом, освобождаем его
        existing_sess = self._sessions.get(session_id)
        if existing_sess is not None and existing_sess.bridge_id != bridge_id:
            self._free_session_devices(session_id)
            if self._bridge_to_session.get(existing_sess.bridge_id) == session_id:
                self._bridge_to_session.pop(existing_sess.bridge_id, None)
            existing_sess.revoked = True
            self._sessions.pop(session_id, None)

        old_session_id = self._bridge_to_session.get(bridge_id)
        if old_session_id is not None and old_session_id != session_id:
            # Отзываем старую сессию и освобождаем её устройства
            old_sess = self._sessions.get(old_session_id)
            if old_sess is not None:
                old_sess.revoked = True
                self._sessions.pop(old_session_id, None)
            self._free_session_devices(old_session_id)

        current_gen = self._bridge_generations.get(bridge_id, 0)
        # Если сессия новая для этого моста — увеличиваем поколение
        if old_session_id != session_id:
            current_gen += 1
            self._bridge_generations[bridge_id] = current_gen

        self._bridge_to_session[bridge_id] = session_id
        self._sessions[session_id] = _SessionRecord(
            bridge_id=bridge_id,
            session_id=session_id,
            generation=current_gen,
            expires_at=now + self._ttl,
            revoked=False,
        )
        return session_id

    def apply_snapshot(
        self,
        bridge_id: str,
        session_id: str,
        device_ids: Iterable[str],
    ) -> bool:
        """
        Атомарно применяет полный снимок активных устройств сессии.

        Если любой из ID устройств уже занят другой активной сессией,
        возникает конфликт и весь снимок атомарно отвергается (возвращает False).
        Пустой снимок освобождает все устройства сессии.
        Запоздалый снимок устаревшей сессии отклоняется.
        Сессия должна быть предварительно авторизована через open_session.
        """
        now = self._clock()
        self.cleanup_expired()

        session = self._sessions.get(session_id)
        if session is None or session.revoked or session.bridge_id != bridge_id:
            return False

        active_session = self._bridge_to_session.get(bridge_id)
        if active_session != session_id:
            # Запоздалый снимок старой сессии: мост уже открыл другую сессию
            return False

        if session.generation < self._bridge_generations.get(bridge_id, 0):
            return False

        # Фильтруем устройства: сохраняются только входящие в allowlist
        target_ids = {d for d in device_ids if d in self._allowed_device_ids}

        # Проверка конфликтов с другими активными сессиями
        for dev_id in target_ids:
            rec = self._devices.get(dev_id)
            if rec is not None:
                # Если устройство привязано к другой сессии и TTL не истёк — конфликт!
                if rec.session_id != session_id and rec.expires_at > now:
                    return False

        # Атомарное обновление: снимок полный, поэтому снимаем устройства сессии,
        # отсутствующие в новом снимке
        current_session_devs = [
            d for d, r in self._devices.items() if r.session_id == session_id
        ]
        for dev_id in current_session_devs:
            if dev_id not in target_ids:
                del self._devices[dev_id]

        new_expires_at = now + self._ttl
        for dev_id in target_ids:
            self._devices[dev_id] = _DeviceRecord(
                device_id=dev_id,
                bridge_id=bridge_id,
                session_id=session_id,
                generation=session.generation,
                expires_at=new_expires_at,
            )

        session.expires_at = new_expires_at
        return True

    def heartbeat(self, bridge_id: str, session_id: str) -> bool:
        """
        Продлевает TTL сессии и всех её зарегистрированных устройств.

        Возвращает True, если сессия активна и продлена; False, если сессия
        не найдена, отозвана, устарела или уже истекла по TTL.
        """
        now = self._clock()
        self.cleanup_expired()

        session = self._sessions.get(session_id)
        if session is None or session.revoked or session.bridge_id != bridge_id:
            return False

        if self._bridge_to_session.get(bridge_id) != session_id:
            return False

        new_expires_at = now + self._ttl
        session.expires_at = new_expires_at
        for rec in self._devices.values():
            if rec.session_id == session_id:
                rec.expires_at = new_expires_at

        return True

    def close_session(self, bridge_id: str, session_id: str) -> bool:
        """
        Закрывает сессию моста и освобождает все её устройства.

        Запоздалое закрытие старой сессии отклоняется и не меняет записи новой сессии.
        """
        self.cleanup_expired()

        session = self._sessions.get(session_id)
        if session is None or session.revoked or session.bridge_id != bridge_id:
            return False

        if self._bridge_to_session.get(bridge_id) != session_id:
            # Запоздалое закрытие устаревшей сессии: не трогаем текущие записи
            return False

        self._bridge_to_session.pop(bridge_id, None)
        session.revoked = True
        self._sessions.pop(session_id, None)
        self._free_session_devices(session_id)
        return True

    def revoke_session(self, session_id: str) -> bool:
        """Явно отзывает сессию по её session_id и освобождает устройства."""
        self.cleanup_expired()
        session = self._sessions.get(session_id)
        if session is None or session.revoked:
            return False

        session.revoked = True
        if self._bridge_to_session.get(session.bridge_id) == session_id:
            self._bridge_to_session.pop(session.bridge_id, None)

        self._sessions.pop(session_id, None)
        self._free_session_devices(session_id)
        return True

    def resolve(self, device_id: str) -> USBPresence | None:
        """
        Возвращает активную USB-привязку для устройства или None,
        если устройство не на USB, истекло или не зарегистрировано.
        """
        if device_id not in self._allowed_device_ids:
            return None

        rec = self._devices.get(device_id)
        if rec is None:
            return None

        now = self._clock()
        if now >= rec.expires_at:
            # Истекло по TTL — снимаем USB
            del self._devices[device_id]
            return None

        session = self._sessions.get(rec.session_id)
        if session is None or session.revoked or now >= session.expires_at:
            del self._devices[device_id]
            return None

        return USBPresence(
            bridge_id=rec.bridge_id,
            session_id=rec.session_id,
            device_id=rec.device_id,
            generation=rec.generation,
        )

    def cleanup_expired(self) -> int:
        """
        Удаляет истекшие записи устройств и сессий, ограничивая хранение.

        Возвращает количество удалённых просроченных привязок устройств.
        """
        now = self._clock()

        # Очистка истекших или отозванных сессий
        expired_sessions = [
            s_id for s_id, s in self._sessions.items()
            if now >= s.expires_at or s.revoked
        ]
        for s_id in expired_sessions:
            s = self._sessions[s_id]
            if self._bridge_to_session.get(s.bridge_id) == s_id:
                self._bridge_to_session.pop(s.bridge_id, None)
            self._sessions.pop(s_id, None)

        # Очистка истекших или осиротевших устройств
        expired_devices = [
            d for d, r in self._devices.items()
            if now >= r.expires_at or r.session_id not in self._sessions
        ]
        for d in expired_devices:
            del self._devices[d]

        return len(expired_devices)

    def active_device_ids(self) -> set[str]:
        """Возвращает множество ID устройств, активных на USB в данный момент."""
        self.cleanup_expired()
        now = self._clock()
        result = set()
        for d, rec in list(self._devices.items()):
            if rec.expires_at > now:
                sess = self._sessions.get(rec.session_id)
                if sess is not None and not sess.revoked and sess.expires_at > now:
                    result.add(d)
        return result

    def get_active_session(self, bridge_id: str) -> str | None:
        """Возвращает текущую активную сессию моста или None."""
        self.cleanup_expired()
        return self._bridge_to_session.get(bridge_id)

    def get_generation(self, bridge_id: str) -> int:
        """Возвращает текущее поколение моста (начиная с 1)."""
        return self._bridge_generations.get(bridge_id, 0)

    def _free_session_devices(self, session_id: str) -> None:
        """Освобождает все устройства указанной сессии."""
        to_delete = [
            d for d, rec in self._devices.items() if rec.session_id == session_id
        ]
        for d in to_delete:
            del self._devices[d]
