"""Менеджер процессов scrcpy для управления нативными окнами устройств."""

from __future__ import annotations

import atexit
import logging
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field


logger = logging.getLogger(__name__)


@dataclass
class ScrcpySession:
    """Информация о запущенном процессе scrcpy."""
    serial: str
    process: subprocess.Popen
    start_time: float
    window_title: str
    last_error: str | None = None
    stderr_lines: deque[str] = field(default_factory=lambda: deque(maxlen=50))
    reader_thread: threading.Thread | None = None

    def is_running(self) -> bool:
        """Проверяет, работает ли процесс в данный момент."""
        return self.process.poll() is None

    @property
    def exit_code(self) -> int | None:
        """Возвращает код завершения процесса, если он завершился."""
        return self.process.poll()


class ScrcpyManager:
    """Управление жизненным циклом процессов scrcpy без shell=True.

    Гарантирует:
    - Запуск списком аргументов с явным -s <serial>
    - Защиту от повторного запуска scrcpy для одного и того же устройства
    - Корректную остановку процессов при закрытии приложения
    - Обработку отключения устройств
    """

    def __init__(self, scrcpy_path: str = "scrcpy") -> None:
        self.scrcpy_path = scrcpy_path
        self._sessions: dict[str, ScrcpySession] = {}
        # Автоматическая очистка всех процессов при выходе из Python
        atexit.register(self.stop_all)

    def set_scrcpy_path(self, path: str) -> None:
        """Обновляет путь к исполняемому файлу scrcpy."""
        self.scrcpy_path = path

    def is_running(self, serial: str) -> bool:
        """Проверяет, открыто ли окно scrcpy для данного устройства."""
        session = self._sessions.get(serial)
        if session is None:
            return False
        return session.is_running()

    def get_session(self, serial: str) -> ScrcpySession | None:
        """Возвращает информацию о сессии устройства."""
        return self._sessions.get(serial)

    def get_active_serials(self) -> set[str]:
        """Возвращает множество серийных номеров с активными процессами scrcpy."""
        active: set[str] = set()
        for serial, session in list(self._sessions.items()):
            if session.is_running():
                active.add(serial)
        return active

    def start(
        self,
        serial: str,
        title: str | None = None,
        extra_args: list[str] | None = None,
    ) -> tuple[bool, str | None]:
        """Запускает отдельное управляемое окно scrcpy для указанного устройства.

        Возвращает: (успех: bool, сообщение_об_ошибке: str | None)
        """
        if not serial:
            return False, "Серийный номер устройства не указан."

        # Защита от дублирования запуска
        if self.is_running(serial):
            return False, f"Окно scrcpy для устройства '{serial}' уже запущено."

        # Подготовка аргументов (строго списком, без shell=True)
        window_title = f"DroidHub: {title or serial}"
        cmd: list[str] = [
            self.scrcpy_path,
            "-s",
            serial,
            f"--window-title={window_title}",
        ]
        if extra_args:
            cmd.extend(extra_args)

        try:
            # Запуск процесса без shell=True с UTF-8 кодировкой
            proc = subprocess.Popen(
                cmd,
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            stderr_lines: deque[str] = deque(maxlen=50)

            def _drain_stderr() -> None:
                try:
                    pipe = proc.stderr
                    if pipe:
                        for line in pipe:
                            if isinstance(line, str):
                                stderr_lines.append(line.rstrip("\r\n"))
                except Exception:
                    pass
                finally:
                    try:
                        if proc.stderr and hasattr(proc.stderr, "close"):
                            proc.stderr.close()
                    except Exception:
                        pass

            reader_thread = threading.Thread(
                target=_drain_stderr,
                name=f"scrcpy-stderr-{serial}",
                daemon=True,
            )
            reader_thread.start()

            # Короткая пауза для проверки немедленного падения (например, неверный флаг или устройство недоступно)
            time.sleep(0.3)
            exit_code = proc.poll()
            if exit_code is not None:
                reader_thread.join(timeout=0.2)
                err_text = "\n".join(stderr_lines).strip()
                if not err_text and proc.stderr:
                    try:
                        read_val = proc.stderr.read()
                        if isinstance(read_val, str) and read_val.strip():
                            err_text = read_val.strip()
                    except Exception:
                        pass
                try:
                    if proc.stderr and hasattr(proc.stderr, "close"):
                        proc.stderr.close()
                except Exception:
                    pass
                msg = f"Ошибка запуска scrcpy (код {exit_code}): {err_text or 'Процесс завершился сразу после старта'}"
                return False, msg

            session = ScrcpySession(
                serial=serial,
                process=proc,
                start_time=time.time(),
                window_title=window_title,
                stderr_lines=stderr_lines,
                reader_thread=reader_thread,
            )
            self._sessions[serial] = session
            return True, None

        except FileNotFoundError:
            return (
                False,
                f"Исполняемый файл scrcpy не найден по пути: '{self.scrcpy_path}'. "
                "Установите scrcpy или укажите верный путь в настройках.",
            )
        except Exception as exc:
            return False, f"Не удалось запустить scrcpy: {exc}"

    def _cleanup_session(self, session: ScrcpySession) -> None:
        """Закрывает дескриптор stderr и фиксирует last_error для завершенной сессии."""
        if session.reader_thread and session.reader_thread.is_alive():
            session.reader_thread.join(timeout=0.2)

        try:
            if session.process.stderr and hasattr(session.process.stderr, "close"):
                session.process.stderr.close()
        except Exception:
            pass

        if session.last_error is None:
            if session.exit_code not in (None, 0):
                err = "\n".join(session.stderr_lines).strip()
                if not err and session.process.stderr:
                    try:
                        read_val = session.process.stderr.read()
                        if isinstance(read_val, str) and read_val.strip():
                            err = read_val.strip()
                    except Exception:
                        pass
                session.last_error = err or f"Процесс завершился с кодом {session.exit_code}"
            else:
                session.last_error = ""

    def stop(self, serial: str) -> tuple[bool, str | None]:
        """Останавливает окно scrcpy для указанного устройства."""
        session = self._sessions.get(serial)
        if not session:
            return False, f"Сессия scrcpy для '{serial}' не найдена."

        if not session.is_running():
            self._cleanup_session(session)
            return True, None

        try:
            # Сначала мягкая остановка SIGTERM
            session.process.terminate()
            try:
                session.process.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                # Если не завершился вовремя — принудительно SIGKILL
                session.process.kill()
                session.process.wait(timeout=1.0)
            self._cleanup_session(session)
            return True, None
        except Exception as exc:
            self._cleanup_session(session)
            return False, f"Ошибка при остановке scrcpy: {exc}"

    def stop_all(self) -> int:
        """Завершает все запущенные процессы scrcpy. Возвращает количество остановленных."""
        stopped = 0
        for serial, session in list(self._sessions.items()):
            if session.is_running():
                try:
                    session.process.terminate()
                    stopped += 1
                except Exception:
                    pass

        # Ожидание завершения и очистка ресурсов
        for serial, session in list(self._sessions.items()):
            if session.is_running():
                try:
                    session.process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    try:
                        session.process.kill()
                    except Exception:
                        pass
                except Exception:
                    pass
            self._cleanup_session(session)
        return stopped

    def poll_sessions(self) -> dict[str, bool]:
        """Опрашивает состояние всех процессов и возвращает статус активности."""
        status: dict[str, bool] = {}
        for serial, session in list(self._sessions.items()):
            running = session.is_running()
            status[serial] = running
            if not running and session.last_error is None:
                self._cleanup_session(session)
        return status

    def handle_device_disconnection(self, current_connected_serials: set[str]) -> list[str]:
        """Обрабатывает отключение физических устройств от USB.

        Если устройство отключено, но его окно scrcpy еще числилось запущенным,
        аккуратно завершает процесс и возвращает список таких отключенных серийных номеров.
        """
        disconnected_with_sessions: list[str] = []
        for serial, session in list(self._sessions.items()):
            if serial not in current_connected_serials:
                if session.is_running():
                    logger.info("Устройство %s отключено, закрываем процесс scrcpy", serial)
                    self.stop(serial)
                    disconnected_with_sessions.append(serial)
        return disconnected_with_sessions
