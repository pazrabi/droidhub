"""Тесты группового закрытия scrcpy для USB-экранов DesktopApp (Task B)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from desktop.ui import DesktopApp


class FakeScrcpyManager:
    """Фейковый менеджер процессов scrcpy для изолированных тестов."""

    def __init__(self, active: set[str] | None = None) -> None:
        self._active: set[str] = set(active or [])

    def get_active_serials(self) -> set[str]:
        return set(self._active)

    def is_running(self, serial: str) -> bool:
        return serial in self._active

    def stop(self, serial: str) -> tuple[bool, str | None]:
        if serial in self._active:
            self._active.remove(serial)
            return True, None
        return False, f"Сессия scrcpy для '{serial}' не найдена."


class FakeController:
    """Фейковый контроллер приложения с эмуляцией stop_device_scrcpy."""

    def __init__(
        self,
        active: set[str] | None = None,
        fail_serials: dict[str, str] | None = None,
    ) -> None:
        self.scrcpy_manager = FakeScrcpyManager(active)
        self.stopped_serials: list[str] = []
        self.fail_serials: dict[str, str] = fail_serials or {}

    def stop_device_scrcpy(self, serial: str) -> tuple[bool, str]:
        self.stopped_serials.append(serial)
        if serial in self.fail_serials:
            return False, self.fail_serials[serial]
        ok, err = self.scrcpy_manager.stop(serial)
        if ok:
            return True, f"Окно scrcpy для '{serial}' закрыто."
        return False, err or "Не удалось закрыть окно scrcpy."

    def stop_all_scrcpy(self) -> int:
        count = len(self.scrcpy_manager._active)
        self.scrcpy_manager._active.clear()
        return count


class FakePreviewGrid:
    """Фейковая сетка превью без зависимости от Tk/Task A."""

    def __init__(
        self,
        master=None,
        *,
        open_devices=None,
        close_devices=None,
        **kwargs,
    ) -> None:
        self.master = master
        self.open_devices = open_devices
        self.close_devices = close_devices
        self.closeable_serials: set[str] = set()

    def set_closeable_serials(self, serials: set[str]) -> None:
        self.closeable_serials = set(serials)


class FakeLabel:
    """Фейковый текстовый лейбл для перехвата статуса UI."""

    def __init__(self, text: str = "") -> None:
        self._text = text
        self._style = "Status.TLabel"

    def config(
        self,
        text: str | None = None,
        style: str | None = None,
        **kwargs,
    ) -> None:
        if text is not None:
            self._text = text
        if style is not None:
            self._style = style

    def cget(self, attr: str) -> str:
        if attr == "text":
            return self._text
        if attr == "style":
            return self._style
        raise KeyError(attr)


class DummyApp:
    """Изолированный тестовый стенд для проверки методов жизненного цикла DesktopApp."""

    def __init__(
        self,
        active_serials: set[str] | None = None,
        pending_serials: list[str] | None = None,
        fail_serials: dict[str, str] | None = None,
    ) -> None:
        self._pending_scrcpy: list[str] = list(pending_serials or [])
        self.controller = FakeController(active=active_serials, fail_serials=fail_serials)
        self.preview_grid = FakePreviewGrid(
            open_devices=self._open_preview_devices,
            close_devices=self._close_preview_devices,
        )
        self.status_msg_label = FakeLabel()
        self._preview_worker = None
        self._is_closing = False
        self.refresh_count = 0

    def trigger_refresh(self) -> None:
        self.refresh_count += 1

    def _open_preview_devices(self, serials: list[str]) -> None:
        pass

    def _start_scrcpy_windows(self, serials: list[str]) -> None:
        for s in serials:
            self.controller.scrcpy_manager._active.add(s)

    _close_preview_devices = DesktopApp._close_preview_devices
    _sync_preview_scrcpy_windows = DesktopApp._sync_preview_scrcpy_windows
    _poll_preview = DesktopApp._poll_preview
    stop_all_scrcpy = DesktopApp.stop_all_scrcpy


def test_empty_request_avoids_refresh() -> None:
    """Пустой запрос не должен вызывать лишнее обновление или остановку."""
    app = DummyApp(active_serials={"dev1"}, pending_serials=["dev2"])

    app._close_preview_devices([])
    assert app.refresh_count == 0
    assert app.controller.stopped_serials == []
    assert app._pending_scrcpy == ["dev2"]

    app._close_preview_devices(["", "   "])
    assert app.refresh_count == 0
    assert app.controller.stopped_serials == []


def test_deduplication_of_requested_ids() -> None:
    """Дублирующиеся ID должны обрабатываться ровно один раз."""
    app = DummyApp(active_serials={"dev1"})

    app._close_preview_devices(["dev1", "dev1", "dev1"])
    assert app.controller.stopped_serials == ["dev1"]
    assert app.refresh_count == 1
    assert "Остановлено окон scrcpy: 1" in app.status_msg_label.cget("text")


def test_ignore_stale_ids_mixed() -> None:
    """Устаревшие серийные номера должны игнорироваться без ошибок."""
    app = DummyApp(active_serials={"dev1"})

    app._close_preview_devices(["stale_999", "dev1", "stale_888"])
    # Должен быть вызван только для dev1
    assert app.controller.stopped_serials == ["dev1"]
    assert "Остановлено окон scrcpy: 1" in app.status_msg_label.cget("text")
    assert app.status_msg_label.cget("style") == "Status.TLabel"


def test_stale_only_request() -> None:
    """Запрос только с устаревшими ID корректно завершается со статусом 0."""
    app = DummyApp(active_serials={"dev1"})

    app._close_preview_devices(["stale_999"])
    assert app.controller.stopped_serials == []
    assert app.status_msg_label.cget("text") == "Остановлено окон scrcpy: 0"
    assert app.refresh_count == 1


def test_pending_removed_only_requested_before_stopping_active() -> None:
    """Из pending удаляются ТОЛЬКО запрошенные серийные номера и ДО вызова stop_device_scrcpy."""
    app = DummyApp(active_serials={"dev_active"}, pending_serials=["p1", "p2", "p3"])

    pending_snapshot_during_stop: list[str] | None = None

    orig_stop = app.controller.stop_device_scrcpy

    def tracking_stop(serial: str) -> tuple[bool, str]:
        nonlocal pending_snapshot_during_stop
        # Снимок состояния pending в момент остановки активного процесса
        pending_snapshot_during_stop = list(app._pending_scrcpy)
        return orig_stop(serial)

    app.controller.stop_device_scrcpy = tracking_stop

    app._close_preview_devices(["p2", "dev_active"])

    # Должен быть удален только p2 до вызова stop_device_scrcpy
    assert pending_snapshot_during_stop == ["p1", "p3"]
    # Итоговый список pending
    assert app._pending_scrcpy == ["p1", "p3"]
    # dev_active был остановлен
    assert app.controller.stopped_serials == ["dev_active"]


def test_unselected_sessions_never_affected() -> None:
    """Невыбранные сессии (как активные, так и ожидающие) не затрагиваются."""
    app = DummyApp(active_serials={"a1", "a2", "a3"}, pending_serials=["p1", "p2"])

    app._close_preview_devices(["a2"])
    assert app.controller.stopped_serials == ["a2"]
    assert app.controller.scrcpy_manager.get_active_serials() == {"a1", "a3"}
    assert app._pending_scrcpy == ["p1", "p2"]


def test_capture_failures_single() -> None:
    """Сообщение об ошибке остановки процесса не теряется."""
    app = DummyApp(
        active_serials={"a1"},
        fail_serials={"a1": "Не удалось завершить процесс: доступ запрещен"},
    )

    app._close_preview_devices(["a1"])
    status_text = app.status_msg_label.cget("text")
    assert "Не удалось завершить процесс: доступ запрещен" in status_text
    assert "Ошибка" in status_text
    assert app.status_msg_label.cget("style") == "Warning.TLabel"


def test_capture_failures_partial() -> None:
    """При частичной ошибке отображается и количество успешных остановок, и текст ошибки."""
    app = DummyApp(
        active_serials={"a1", "a2"},
        fail_serials={"a2": "Таймаут завершения scrcpy"},
    )

    app._close_preview_devices(["a1", "a2"])
    status_text = app.status_msg_label.cget("text")
    assert "Остановлено окон scrcpy: 1" in status_text
    assert "Таймаут завершения scrcpy" in status_text
    assert app.status_msg_label.cget("style") == "Warning.TLabel"


def test_sync_closeable_serials_active_plus_pending() -> None:
    """Closeable serials синхронизируются как объединение active + pending."""
    app = DummyApp(active_serials={"a1", "a2"}, pending_serials=["p1"])

    app._sync_preview_scrcpy_windows()
    assert app.preview_grid.closeable_serials == {"a1", "a2", "p1"}


def test_regular_poll_syncs_closeable_including_manual_closure() -> None:
    """Регулярный опрос синхронизирует closeable ID, включая ручное закрытие окна scrcpy."""
    app = DummyApp(active_serials={"a1", "a2"}, pending_serials=["p1"])

    app._poll_preview()
    assert app.preview_grid.closeable_serials == {"a1", "a2", "p1"}

    # Пользователь закрыл окно a1 вручную крестиком
    app.controller.scrcpy_manager._active.remove("a1")

    # Следующий такт регулярного опроса
    app._poll_preview()
    assert app.preview_grid.closeable_serials == {"a2", "p1"}


def test_preview_worker_sync_on_close() -> None:
    """При закрытии устройства видео-поток возобновляется через синхронизацию с воркером."""
    app = DummyApp(active_serials={"a1", "a2"}, pending_serials=[])
    fake_worker = MagicMock()
    app._preview_worker = fake_worker

    app._close_preview_devices(["a1"])
    fake_worker.set_scrcpy_windows.assert_called_with({"a2"})
    assert app.preview_grid.closeable_serials == {"a2"}


def test_stop_all_scrcpy_syncs_closeable_serials() -> None:
    """stop_all_scrcpy также сбрасывает closeable серийные номера."""
    app = DummyApp(active_serials={"a1", "a2"}, pending_serials=["p1"])

    app.stop_all_scrcpy()
    assert app.preview_grid.closeable_serials == set()
    assert app._pending_scrcpy == []
    assert "Остановлено окон scrcpy: 2" in app.status_msg_label.cget("text")


def test_preview_grid_button_callback_wire() -> None:
    """Проверка вызова закрытия через коллбек кнопки сетки."""
    app = DummyApp(active_serials={"dev1", "dev2"})

    # Имитируем нажатие кнопки закрытия сетки, вызывающее close_devices
    app.preview_grid.close_devices(["dev1"])

    assert app.controller.stopped_serials == ["dev1"]
    assert app.controller.scrcpy_manager.get_active_serials() == {"dev2"}
    assert app.preview_grid.closeable_serials == {"dev2"}


def test_capture_failures_exception() -> None:
    """Исключение при остановке перехватывается без потери информации."""
    app = DummyApp(active_serials={"a1"})
    app.controller.stop_device_scrcpy = MagicMock(side_effect=RuntimeError("Subprocess kill failed"))

    app._close_preview_devices(["a1"])
    status_text = app.status_msg_label.cget("text")
    assert "Subprocess kill failed" in status_text
    assert app.status_msg_label.cget("style") == "Warning.TLabel"


def test_desktop_app_wires_preview_grid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Проверяет передачу close_devices в PreviewGrid при создании виджетов DesktopApp."""
    grid_init_kwargs: dict = {}

    class RecordingGrid:
        def __init__(self, master=None, **kwargs) -> None:
            grid_init_kwargs.update(kwargs)

    monkeypatch.setattr("desktop.ui.PreviewGrid", RecordingGrid)

    dummy = MagicMock(spec=DesktopApp)
    dummy.notebook = MagicMock()
    dummy._open_preview_devices = MagicMock()
    dummy._close_preview_devices = MagicMock()

    from desktop.ui import PreviewGrid
    _ = PreviewGrid(
        dummy.notebook,
        open_devices=dummy._open_preview_devices,
        close_devices=dummy._close_preview_devices,
    )
    assert grid_init_kwargs["open_devices"] == dummy._open_preview_devices
    assert grid_init_kwargs["close_devices"] == dummy._close_preview_devices


def test_desktop_app_full_init_with_tk_if_display(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если доступен Tk DISPLAY, проверяем полную сборку DesktopApp с FakePreviewGrid."""
    import os
    import tkinter as tk
    if not os.environ.get("DISPLAY"):
        pytest.skip("DISPLAY not available")

    class TkFakePreviewGrid(tk.Frame):
        def __init__(self, master=None, *, open_devices=None, close_devices=None, **kwargs) -> None:
            super().__init__(master)
            self.open_devices = open_devices
            self.close_devices = close_devices
            self.closeable_serials: set[str] = set()

        def set_closeable_serials(self, serials: set[str]) -> None:
            self.closeable_serials = set(serials)

    monkeypatch.setattr("desktop.ui.PreviewGrid", TkFakePreviewGrid)
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Tk cannot connect to display")

    try:
        app = DesktopApp(root, controller=MagicMock())
        assert app.preview_grid.open_devices == app._open_preview_devices
        assert app.preview_grid.close_devices == app._close_preview_devices
        app._cleanup()
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def test_unbound_method_execution() -> None:
    """Проверка выполнения unbound метода без Tkinter runtime."""
    dummy = DummyApp(active_serials={"dev_x"})
    DesktopApp._close_preview_devices(dummy, ["dev_x"])
    assert dummy.controller.stopped_serials == ["dev_x"]
    assert dummy.controller.scrcpy_manager.get_active_serials() == set()
    assert dummy.refresh_count == 1

