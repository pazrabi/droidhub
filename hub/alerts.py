from __future__ import annotations

import asyncio
import logging
import re
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


_TOKENS_TO_SANITIZE: set[str] = set()
_BOT_URL_PATTERN = re.compile(r"/bot[^/\s]+")


def _sanitize(text: str, token: str = "") -> str:
    """Удаляет секретный Telegram-токен из строк логов."""
    if token and token in text:
        text = text.replace(token, "[REDACTED]")
    for t in _TOKENS_TO_SANITIZE:
        if t and t in text:
            text = text.replace(t, "[REDACTED]")
    return _BOT_URL_PATTERN.sub("/bot[REDACTED]", text)


class _TelegramTokenFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = _sanitize(record.getMessage())
            record.args = ()
        except Exception:
            if isinstance(record.msg, str):
                record.msg = _sanitize(record.msg)
        return True


# Заглушаем вывод информационных логов httpx, чтобы токен бота в URL не логировался
_httpx_logger = logging.getLogger("httpx")
_httpx_logger.setLevel(logging.WARNING)
_httpx_logger.addFilter(_TelegramTokenFilter())
logger.addFilter(_TelegramTokenFilter())


class TelegramNotifier:
    """
    Уведомитель в Telegram:
    - отправка сообщений через httpx к api.telegram.org (токен нигде не логируется)
    - при пустом токене работает в режиме только логирования в лог
    - хаб только отправляет сообщения (sendMessage) с префиксом [<alert_tag>]
    - никогда не вызывает getUpdates и не обрабатывает callback_query
    """

    def __init__(
        self,
        token: str = "",
        chat_id: str = "",
        manager: DeviceManager | None = None,
        client: httpx.AsyncClient | None = None,
        poll_timeout: float = 20.0,
        alert_tag: str = "droidhub",
    ) -> None:
        self.token = str(token).strip() if token else ""
        if self.token:
            _TOKENS_TO_SANITIZE.add(self.token)
        self.chat_id = str(chat_id).strip() if chat_id else ""
        self.manager = manager
        self.poll_timeout = poll_timeout
        self.alert_tag = str(alert_tag).strip() if alert_tag else "droidhub"
        self._client = client
        self._own_client = client is None

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
        Все отправляемые сообщения получают префикс [<alert_tag>].
        Если токен пуст — пишет только в лог.
        """
        if self.alert_tag:
            prefix = f"[{self.alert_tag}] "
            if not text.startswith(f"[{self.alert_tag}]"):
                text = f"{prefix}{text}"

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

    async def start(self) -> None:
        """Совместимый метод: long polling удалён, никаких фоновых задач не запускается."""
        pass

    async def stop(self) -> None:
        """Совместимый метод: освобождает собственный HTTP-клиент."""
        if self._own_client and self._client is not None:
            await self._client.aclose()
            self._client = None


class AlertMonitor:
    """
    Монитор безопасности устройств:
    - предупреждение при температуре >= temp_warn_c
    - тревога при температуре >= temp_alert_c с inline-кнопкой «Открыть панель» (url=<public_url>/device/<id>)
    - авто-выключение при температуре >= temp_shutdown_c (если > 0)
    - фраза «Зарядка не остановлена...» при тревоге и автовыключении
    - напоминание при заряде 100% дольше full_charge_hours
    - оповещение о выходе в оффлайн дольше offline_alert_min и оповещение «снова онлайн» при возврате
    - кулдаун alert_cooldown_min на каждое правило и устройство, температурные тревоги не молчат при росте на >= 2 °C
    - префикс [<alert_tag>] во всех отправляемых сообщениях
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
                alert_tag=settings.alert_tag,
            )
        self.interval = interval
        self._running = False
        self._task: asyncio.Task | None = None

        self._last_alert_time: dict[tuple[str, str], float] = {}
        self._last_alert_temp: dict[tuple[str, str], float] = {}
        self._offline_since: dict[str, float] = {}
        self._offline_alerted: set[str] = set()
        self._full_charge_since: dict[str, float] = {}

    def _format_msg(self, text: str) -> str:
        tag = self.settings.alert_tag.strip() if self.settings.alert_tag else ""
        if tag:
            prefix = f"[{tag}] "
            if not text.startswith(f"[{tag}]"):
                return f"{prefix}{text}"
        return text

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
            if last_temp is not None and round(temp - last_temp, 5) >= 2.0:
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
            # одно сообщение на один период оффлайна; следующее только после возврата в онлайн
            if offline_duration_min >= self.settings.offline_alert_min and dev_id not in self._offline_alerted:
                if self._can_alert(dev_id, "offline", now=now):
                    msg = self._format_msg(
                        f"Устройство {dev_name} оффлайн более {self.settings.offline_alert_min:.0f} мин"
                    )
                    await self.notifier.send(msg)
                    self._record_alert(dev_id, "offline", now=now)
                    self._offline_alerted.add(dev_id)
            return
        else:
            if dev_id in self._offline_alerted:
                msg = self._format_msg(f"Устройство {dev_name} снова онлайн")
                await self.notifier.send(msg)
                self._offline_alerted.discard(dev_id)
            self._offline_since.pop(dev_id, None)

        # 2. Проверка 100% заряда (full_charge_hours = 0 отключает напоминание)
        if status.battery is not None and self.settings.full_charge_hours > 0:
            if status.battery >= 100:
                if dev_id not in self._full_charge_since:
                    self._full_charge_since[dev_id] = now
                duration_hours = (now - self._full_charge_since[dev_id]) / 3600.0
                if duration_hours >= self.settings.full_charge_hours:
                    if self._can_alert(dev_id, "full_charge", now=now):
                        msg = self._format_msg(f"Устройство {dev_name}: заряд держится на 100%")
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

            charging_warning = (
                "Зарядка не остановлена: при подключённом кабеле батарея продолжает заряжаться, "
                "отключи питание физически"
            )

            if is_shutdown:
                if self._can_alert(dev_id, "temp_shutdown", temp=temp, now=now):
                    shutdown_err = None
                    try:
                        await actions.power(dev_id, "shutdown", actor="alerts")
                    except Exception as exc:
                        logger.error("Error shutting down device %s on overheat: %s", dev_id, exc)
                        shutdown_err = exc

                    if shutdown_err:
                        body = (
                            f"Критический перегрев {dev_name}: {temp:.1f} °C! "
                            f"Сбой авто-выключения: {shutdown_err}. {charging_warning}"
                        )
                    else:
                        body = (
                            f"Критический перегрев {dev_name}: {temp:.1f} °C. "
                            f"Устройство автоматически выключается! {charging_warning}"
                        )
                    msg = self._format_msg(body)
                    await self.notifier.send(msg)
                    self._record_alert(dev_id, "temp_shutdown", now=now, temp=temp)
            elif is_alert:
                if self._can_alert(dev_id, "temp_alert", temp=temp, now=now):
                    body = f"Тревога: перегрев {dev_name}: {temp:.1f} °C. {charging_warning}"
                    msg = self._format_msg(body)
                    buttons = None
                    if self.settings.public_url and self.settings.public_url.strip():
                        base_url = self.settings.public_url.strip().rstrip("/")
                        buttons = [{"text": "Открыть панель", "url": f"{base_url}/device/{dev_id}"}]
                    await self.notifier.send(msg, buttons=buttons)
                    self._record_alert(dev_id, "temp_alert", now=now, temp=temp)
            elif is_warn:
                if self._can_alert(dev_id, "temp_warn", temp=temp, now=now):
                    body = f"Предупреждение: нагрев {dev_name}: {temp:.1f} °C"
                    msg = self._format_msg(body)
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
