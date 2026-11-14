"""
Брокер чтения заряда батареи через авторизованный USB-мост.

Обеспечивает асинхронный интерфейс для запроса данных dumpsys battery
через локальные USB-мосты с привязкой к активным сессиям и защитой от конфликтов.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hub.devices.usb_presence import USBPresenceRegistry

__all__ = [
    "USBBatteryBroker",
    "USBBatteryUnavailable",
]


class USBBatteryUnavailable(Exception):
    """
    Ошибка доступности батареи через USB-мост.

    Возникает при отсутствии устройства на USB, таймауте ожидания ответа,
    разрыве или смене сессии, отказе выполнения команды или закрытии брокера.
    """


@dataclass
class _BatteryRequest:
    """Внутренняя запись о незавершённом запросе чтения батареи."""

    request_id: str
    device_id: str
    bridge_id: str
    session_id: str
    future: asyncio.Future[str]
    polled: bool = False


class USBBatteryBroker:
    """
    Брокер чтения заряда через авторизованный USB-мост.

    Работает в едином asyncio event loop без блокировок (lock-free),
    поддерживает максимум один незавершённый запрос на устройство,
    привязывает запрос к сессии моста на момент вызова read() и
    выдаёт каждый запрос в poll() не более одного раза.
    """

    def __init__(
        self,
        registry: USBPresenceRegistry,
        *,
        check_interval: float = 0.02,
    ) -> None:
        self._registry = registry
        self._check_interval = max(0.001, float(check_interval))
        # device_id -> _BatteryRequest (строго один незавершённый запрос на телефон)
        self._pending_by_device: dict[str, _BatteryRequest] = {}
        # request_id -> _BatteryRequest
        self._pending_by_id: dict[str, _BatteryRequest] = {}
        self._closed: bool = False

    @property
    def registry(self) -> USBPresenceRegistry:
        """Реестр активных USB-привязок."""
        return self._registry

    @property
    def is_closed(self) -> bool:
        """Закрыт ли брокер."""
        return self._closed

    def is_pending(self, device_id: str) -> bool:
        """Проверяет, есть ли незавершённый запрос для указанного устройства."""
        return device_id in self._pending_by_device

    async def read(self, device_id: str, *, timeout: float = 8.0) -> str:
        """
        Асинхронно запрашивает вывод dumpsys battery для указанного устройства.

        :param device_id: Идентификатор устройства.
        :param timeout: Время ожидания в секундах (по умолчанию 8.0).
        :return: Строка с выводом dumpsys battery.
        :raises USBBatteryUnavailable: При недоступности устройства, конфликте,
                                       таймауте, разрыве сессии или ошибке команды.
        """
        if self._closed:
            raise USBBatteryUnavailable("Брокер USB-батареи закрыт")

        if not isinstance(device_id, str) or not device_id:
            raise USBBatteryUnavailable("Некорректный идентификатор устройства")

        if not isinstance(timeout, (int, float)):
            raise USBBatteryUnavailable("Некорректное значение таймаута")

        if timeout <= 0:
            raise USBBatteryUnavailable(
                f"Превышено время ожидания ответа для устройства {device_id} ({timeout} с)"
            )

        # Проверка конфликта: только один незавершённый запрос на телефон
        if device_id in self._pending_by_device:
            raise USBBatteryUnavailable(
                f"Для устройства {device_id} уже выполняется запрос"
            )

        # Проверка актуального присутствия устройства в реестре
        presence = self._registry.resolve(device_id)
        if presence is None:
            raise USBBatteryUnavailable(
                f"Устройство {device_id} недоступно по USB"
            )

        bridge_id = presence.bridge_id
        session_id = presence.session_id

        # Формируем запрос и регистрируем в очередях
        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()

        req = _BatteryRequest(
            request_id=request_id,
            device_id=device_id,
            bridge_id=bridge_id,
            session_id=session_id,
            future=future,
            polled=False,
        )
        self._pending_by_device[device_id] = req
        self._pending_by_id[request_id] = req

        deadline = loop.time() + timeout
        try:
            while True:
                if future.done():
                    return future.result()

                if self._closed:
                    raise USBBatteryUnavailable("Брокер USB-батареи закрыт")

                # Периодическая проверка актуальности registry.resolve при ожидании
                current_pres = self._registry.resolve(device_id)
                if (
                    current_pres is None
                    or current_pres.bridge_id != bridge_id
                    or current_pres.session_id != session_id
                ):
                    raise USBBatteryUnavailable(
                        f"Сессия USB для устройства {device_id} разорвана или заменена"
                    )

                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise USBBatteryUnavailable(
                        f"Превышено время ожидания ответа для устройства {device_id} ({timeout} с)"
                    )

                sleep_time = min(remaining, self._check_interval)
                done, _ = await asyncio.wait([future], timeout=sleep_time)
                if future in done:
                    return future.result()
        finally:
            # Очистка состояния: таймаут, отмена и исключения освобождают pending
            if self._pending_by_device.get(device_id) is req:
                del self._pending_by_device[device_id]
            if self._pending_by_id.get(request_id) is req:
                del self._pending_by_id[request_id]
            if not future.done():
                future.cancel()

    def poll(self, bridge_id: str, session_id: str) -> dict[str, str] | None:
        """
        Синхронно опрашивает наличие запроса для указанного моста и сессии.

        Каждый запрос выдаётся максимум один раз. Перед выдачей проверяется
        актуальный registry.resolve устройства.

        :param bridge_id: Идентификатор моста.
        :param session_id: Идентификатор активной сессии моста.
        :return: Словарь ровно {"request_id": uuid4.hex, "device_id": id} или None.
        """
        if self._closed:
            return None

        if (
            not isinstance(bridge_id, str)
            or not bridge_id
            or not isinstance(session_id, str)
            or not session_id
        ):
            return None

        for req in list(self._pending_by_id.values()):
            if req.bridge_id != bridge_id or req.session_id != session_id:
                continue
            if req.polled or req.future.done():
                continue

            # Проверяем актуальный registry.resolve при выдаче
            current_pres = self._registry.resolve(req.device_id)
            if (
                current_pres is None
                or current_pres.bridge_id != bridge_id
                or current_pres.session_id != session_id
            ):
                # Сессия моста разорвана или устройство больше не на этом мосту/сессии
                self._pending_by_id.pop(req.request_id, None)
                self._pending_by_device.pop(req.device_id, None)
                if not req.future.done():
                    req.future.set_exception(
                        USBBatteryUnavailable(
                            f"Сессия USB для устройства {req.device_id} разорвана или недействительна"
                        )
                    )
                continue

            # Запрос валиден: помечаем как выданный и возвращаем ровно два ключа
            req.polled = True
            return {
                "request_id": req.request_id,
                "device_id": req.device_id,
            }

        return None

    def complete(
        self,
        bridge_id: str,
        session_id: str,
        request_id: str,
        *,
        output: str | None = None,
    ) -> bool:
        """
        Синхронно передаёт результат выполнения команды мостом.

        :param bridge_id: Идентификатор моста.
        :param session_id: Идентификатор сессии моста.
        :param request_id: Идентификатор запроса.
        :param output: Вывод команды или None, если выполнение не удалось.
        :return: True, если результат успешно принят; False, если запоздалый/чужой результат.
        """
        if self._closed:
            return False

        if (
            not isinstance(bridge_id, str)
            or not bridge_id
            or not isinstance(session_id, str)
            or not session_id
            or not isinstance(request_id, str)
            or not request_id
        ):
            return False

        req = self._pending_by_id.get(request_id)
        if req is None or req.future.done():
            # Запрос не найден, уже завершён, либо отменён/истёк по таймауту (запоздалый результат)
            return False

        # Проверка соответствия моста и сессии (чужой результат отклоняется)
        if req.bridge_id != bridge_id or req.session_id != session_id:
            return False

        # Проверяем актуальный registry.resolve при принятии результата
        current_pres = self._registry.resolve(req.device_id)
        if (
            current_pres is None
            or current_pres.bridge_id != bridge_id
            or current_pres.session_id != session_id
        ):
            # Сессия уже разорвана или заменена в реестре: результат отклоняется
            self._pending_by_id.pop(request_id, None)
            self._pending_by_device.pop(req.device_id, None)
            if not req.future.done():
                req.future.set_exception(
                    USBBatteryUnavailable(
                        f"Сессия USB для устройства {req.device_id} разорвана или заменена"
                    )
                )
            return False

        # Принимаем результат и удаляем из очереди ожидания
        self._pending_by_id.pop(request_id, None)
        self._pending_by_device.pop(req.device_id, None)

        if output is None:
            # None output значит выполнение не удалось
            if not req.future.done():
                req.future.set_exception(
                    USBBatteryUnavailable(
                        f"Команда чтения батареи не выполнена на устройстве {req.device_id}"
                    )
                )
            return True

        if not isinstance(output, str):
            output = str(output)

        if not req.future.done():
            req.future.set_result(output)
        return True

    def close(self) -> None:
        """Закрывает брокер и освобождает все незавершённые запросы."""
        if self._closed:
            return
        self._closed = True
        for req in list(self._pending_by_id.values()):
            if not req.future.done():
                req.future.set_exception(
                    USBBatteryUnavailable("Брокер USB-батареи закрыт")
                )
        self._pending_by_id.clear()
        self._pending_by_device.clear()

    def __enter__(self) -> USBBatteryBroker:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    async def __aenter__(self) -> USBBatteryBroker:
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.close()
