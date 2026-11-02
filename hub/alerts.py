from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from hub.config import Settings
from hub.devices import actions
from hub.devices.manager import DeviceManager, DeviceStatus

logger = logging.getLogger(__name__)

# actions.power разрабатывается параллельно другим разработчиком
if not hasattr(actions, "power"):
    async def _stub_power(device_id: str, action: str, *, actor: str = "system") -> None:
        pass
    actions.power = _stub_power  # type: ignore[attr-defined]


def _sanitize(text: str, token: str) -> str:
    """Удаляет секретный Telegram-токен из строк логов."""
    if token and token in text:
        return text.replace(token, "[REDACTED]")
    return text


class TelegramNotifier:
    """
    Уведомитель в Telegram:
    - отправка сообщений через httpx к api.telegram.org (токен нигде не логируется)
    - при пустом токене работает в режиме только логирования в лог
    - long-polling getUpdates для обработки нажатий inline-кнопок
    - подтверждение при нажатии off:<device_id> («Выключить? Да/Нет»)
    - игнорирование чужих chat_id
    """

    def __init__(
        self,
        token: str = "",
        chat_id: str = "",
        manager: DeviceManager | None = None,
        client: httpx.AsyncClient | None = None,
        poll_timeout: float = 20.0,
    ) -> None:
        self.token = str(token).strip() if token else ""
        self.chat_id = str(chat_id).strip() if chat_id else ""
        self.manager = manager
        self.poll_timeout = poll_timeout
        self._client = client
        self._own_client = client is None
        self._running = False
        self._poll_task: asyncio.Task | None = None
        self._offset: int = 0

    def __repr__(self) -> str:
        return f"TelegramNotifier(chat_id={self.chat_id!r}, token='[REDACTED]')"

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
            self._own_client = True
        return self._client

    async def send(
        self,
        text: str,
        buttons: list[dict[str, Any]] | list[list[dict[str, Any]]] | None = None,
    ) -> bool:
        """
        Отправляет текстовое сообщение с опциональными inline-кнопками.
        Если токен пуст — пишет только в лог.
        """
        if not self.token:
            logger.info("Telegram notification [token empty, log only]: %s (buttons=%s)", text, buttons)
            return True

        if not self.chat_id:
            logger.warning("Telegram notification skipped: chat_id is empty")
            return False

        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
        }
        if buttons:
            if isinstance(buttons, list) and len(buttons) > 0:
                if isinstance(buttons[0], dict):
                    payload["reply_markup"] = {"inline_keyboard": [buttons]}
                elif isinstance(buttons[0], list):
                    payload["reply_markup"] = {"inline_keyboard": buttons}

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            client = await self._get_client()
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                logger.warning("Telegram sendMessage returned HTTP %s", resp.status_code)
                return False
            return True
        except Exception as exc:
            logger.warning("Telegram sendMessage error: %s", type(exc).__name__)
            return False

    async def answer_callback(self, callback_query_id: str, text: str | None = None) -> bool:
        """Подтверждает callback_query для Telegram."""
        if not self.token or not callback_query_id:
            return True

        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text

        url = f"https://api.telegram.org/bot{self.token}/answerCallbackQuery"
        try:
            client = await self._get_client()
            await client.post(url, json=payload)
            return True
        except Exception as exc:
            logger.warning("Telegram answerCallbackQuery error: %s", type(exc).__name__)
            return False

    async def handle_update(self, update: dict[str, Any]) -> None:
        """Обрабатывает одно входящее обновление Telegram."""
        cb = update.get("callback_query")
        if cb is not None:
            await self._handle_callback_query(cb)
            return

        msg = update.get("message")
        if msg is not None:
            chat = msg.get("chat") or {}
            chat_id = str(chat.get("id", ""))
            if self.chat_id and chat_id != str(self.chat_id):
                logger.debug("Ignoring message from unexpected chat_id: %s", chat_id)
                return

    async def _handle_callback_query(self, cb: dict[str, Any]) -> None:
        """Обработка нажатия inline-кнопки с проверкой chat_id и подтверждением."""
        cb_id = str(cb.get("id", ""))
        data = str(cb.get("data", "")).strip()
        msg = cb.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        user_id = str(cb.get("from", {}).get("id", ""))

        # Игнорировать чужие chat_id
        if self.chat_id:
            expected = str(self.chat_id)
            if chat_id != expected and user_id != expected:
                logger.warning(
                    "Ignoring callback_query from unauthorized chat/user: %s / %s (expected %s)",
                    chat_id,
                    user_id,
                    expected,
                )
                return
        else:
            logger.warning("Ignoring callback_query because telegram_chat_id is not configured")
            return

        if data.startswith("off:"):
            device_id = data.split("off:", 1)[1].strip()
            await self.answer_callback(cb_id)
            dev_name = device_id
            if self.manager is not None:
                try:
                    dev_name = self.manager.get(device_id).name
                except Exception:
                    pass

            confirm_text = f"Выключить {dev_name}? Да/Нет"
            buttons = [
                [
                    {"text": "Да", "callback_data": f"confirm_off:{device_id}"},
                    {"text": "Нет", "callback_data": f"cancel_off:{device_id}"},
                ]
            ]
            await self.send(confirm_text, buttons=buttons)

        elif any(data.startswith(p) for p in ("confirm_off:", "confirm:off:", "yes_off:", "yes:off:")):
            device_id = ""
            for p in ("confirm_off:", "confirm:off:", "yes_off:", "yes:off:"):
                if data.startswith(p):
                    device_id = data[len(p):].strip()
                    break

            await self.answer_callback(cb_id, text="Выключение...")
            dev_name = device_id
            if self.manager is not None:
                try:
                    dev_name = self.manager.get(device_id).name
                except Exception:
                    pass

            try:
                await actions.power(device_id, "shutdown", actor="telegram")
                await self.send(f"Устройство {dev_name} выключается")
            except Exception as exc:
                logger.error("Failed to shutdown device %s via telegram: %s", device_id, exc)
                await self.send(f"Ошибка выключения {dev_name}: {exc}")

        elif any(data.startswith(p) for p in ("cancel_off:", "cancel:off:", "no_off:", "no:off:")):
            await self.answer_callback(cb_id, text="Отменено")
            await self.send("Выключение отменено")

    async def _poll_loop(self) -> None:
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        while self._running:
            try:
                client = await self._get_client()
                resp = await client.get(
                    url,
                    params={"offset": self._offset, "timeout": int(self.poll_timeout)},
                    timeout=self.poll_timeout + 10.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    updates = data.get("result", [])
                    if isinstance(updates, list):
                        for update in updates:
                            upd_id = update.get("update_id")
                            if upd_id is not None:
                                self._offset = max(self._offset, int(upd_id) + 1)
                            await self.handle_update(update)
                else:
                    logger.warning("Telegram getUpdates returned HTTP %s", resp.status_code)
                    await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Telegram getUpdates error: %s", type(exc).__name__)
                try:
                    await asyncio.sleep(2.0)
                except asyncio.CancelledError:
                    break

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self.token:
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        if self._own_client and self._client is not None:
            await self._client.aclose()
            self._client = None


class AlertMonitor:
    """
    Монитор безопасности устройств:
    - предупреждение при температуре >= temp_warn_c
    - тревога с inline-кнопкой «Выключить <имя>» при температуре >= temp_alert_c
    - авто-выключение при температуре >= temp_shutdown_c (если > 0)
    - напоминание при заряде 100% дольше full_charge_hours
    - оповещение о выходе в оффлайн дольше offline_alert_min и оповещение «снова онлайн» при возврате
    - кулдаун alert_cooldown_min на каждое правило и устройство, температурные тревоги не молчат при росте на >= 2 °C
    """

    def __init__(
        self,
        manager: DeviceManager,
        settings: Settings,
        notifier: TelegramNotifier | None = None,
        interval: float = 30.0,
    ) -> None:
        self.manager = manager
        self.settings = settings
        if notifier is not None:
            self.notifier = notifier
            if getattr(self.notifier, "manager", None) is None:
                self.notifier.manager = manager
        else:
            self.notifier = TelegramNotifier(
                token=settings.telegram_token,
                chat_id=settings.telegram_chat_id,
                manager=manager,
            )
        self.interval = interval
        self._running = False
        self._task: asyncio.Task | None = None

        self._last_alert_time: dict[tuple[str, str], float] = {}
        self._last_alert_temp: dict[tuple[str, str], float] = {}
        self._offline_since: dict[str, float] = {}
        self._offline_alerted: set[str] = set()
        self._full_charge_since: dict[str, float] = {}

    def _can_alert(
        self,
        device_id: str,
        rule: str,
        temp: float | None = None,
        now: float | None = None,
    ) -> bool:
        if now is None:
            now = time.time()
        key = (device_id, rule)
        last_time = self._last_alert_time.get(key)
        cooldown_sec = max(0.0, self.settings.alert_cooldown_min * 60.0)

        # Если алерта еще не было или кулдаун прошел
        if last_time is None or (now - last_time) >= cooldown_sec:
            return True

        # Внутри кулдауна температурные тревоги не молчат при росте на >= 2 °C
        if temp is not None:
            last_temp = self._last_alert_temp.get(key)
            if last_temp is not None and (temp - last_temp) >= 2.0:
                return True

        return False

    def _record_alert(
        self,
        device_id: str,
        rule: str,
        now: float | None = None,
        temp: float | None = None,
    ) -> None:
        if now is None:
            now = time.time()
        key = (device_id, rule)
        self._last_alert_time[key] = now
        if temp is not None:
            self._last_alert_temp[key] = temp

    async def check_device(self, status: DeviceStatus, now: float | None = None) -> None:
        if now is None:
            now = time.time()
        dev_id = status.id
        dev_name = status.name or dev_id

        # 1. Проверка оффлайн / возврат в онлайн
        if not status.online:
            self._full_charge_since.pop(dev_id, None)
            if dev_id not in self._offline_since:
                self._offline_since[dev_id] = (
                    status.last_seen if (status.last_seen and status.last_seen <= now) else now
                )
            offline_duration_min = (now - self._offline_since[dev_id]) / 60.0
            if offline_duration_min >= self.settings.offline_alert_min:
                if self._can_alert(dev_id, "offline", now=now):
                    msg = f"Устройство {dev_name} оффлайн более {self.settings.offline_alert_min:.0f} мин"
                    await self.notifier.send(msg)
                    self._record_alert(dev_id, "offline", now=now)
                    self._offline_alerted.add(dev_id)
            return
        else:
            if dev_id in self._offline_alerted:
                msg = f"Устройство {dev_name} снова онлайн"
                await self.notifier.send(msg)
                self._offline_alerted.discard(dev_id)
            self._offline_since.pop(dev_id, None)

        # 2. Проверка 100% заряда
        if status.battery is not None:
            if status.battery >= 100:
                if dev_id not in self._full_charge_since:
                    self._full_charge_since[dev_id] = now
                duration_hours = (now - self._full_charge_since[dev_id]) / 3600.0
                if duration_hours >= self.settings.full_charge_hours:
                    if self._can_alert(dev_id, "full_charge", now=now):
                        msg = f"Устройство {dev_name}: заряд держится на 100%"
                        await self.notifier.send(msg)
                        self._record_alert(dev_id, "full_charge", now=now)
            else:
                self._full_charge_since.pop(dev_id, None)

        # 3. Проверка температуры (warn / alert / shutdown)
        if status.temperature is not None:
            temp = status.temperature
            is_shutdown = (
                self.settings.temp_shutdown_c > 0 and temp >= self.settings.temp_shutdown_c
            )
            is_alert = (temp >= self.settings.temp_alert_c)
            is_warn = (temp >= self.settings.temp_warn_c)

            if is_shutdown:
                if self._can_alert(dev_id, "temp_shutdown", temp=temp, now=now):
                    msg = (
                        f"Критический перегрев {dev_name}: {temp:.1f} °C. "
                        f"Устройство автоматически выключается!"
                    )
                    try:
                        await actions.power(dev_id, "shutdown", actor="alerts")
                    except Exception as exc:
                        logger.error("Error shutting down device %s on overheat: %s", dev_id, exc)
                    await self.notifier.send(msg)
                    self._record_alert(dev_id, "temp_shutdown", now=now, temp=temp)
            elif is_alert:
                if self._can_alert(dev_id, "temp_alert", temp=temp, now=now):
                    msg = f"Тревога: перегрев {dev_name}: {temp:.1f} °C"
                    buttons = [{"text": f"Выключить {dev_name}", "callback_data": f"off:{dev_id}"}]
                    await self.notifier.send(msg, buttons=buttons)
                    self._record_alert(dev_id, "temp_alert", now=now, temp=temp)
            elif is_warn:
                if self._can_alert(dev_id, "temp_warn", temp=temp, now=now):
                    msg = f"Предупреждение: нагрев {dev_name}: {temp:.1f} °C"
                    await self.notifier.send(msg)
                    self._record_alert(dev_id, "temp_warn", now=now, temp=temp)

    async def check_once(self, now: float | None = None) -> None:
        """Выполняет один проход по статусам всех устройств."""
        if now is None:
            now = time.time()
        statuses = self.manager.statuses()
        for status in statuses:
            try:
                await self.check_device(status, now=now)
            except Exception as exc:
                logger.exception("Error checking alerts for device %s: %s", status.id, exc)

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Error in alert monitor loop: %s", exc)

            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                break

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if hasattr(self.notifier, "start"):
            await self.notifier.start()
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if hasattr(self.notifier, "stop"):
            await self.notifier.stop()


__all__ = ["AlertMonitor", "TelegramNotifier"]
