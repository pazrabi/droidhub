"""Точка входа настольного контроллера DroidHub."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path, если desktop/main.py запущен напрямую
repo_root = str(Path(__file__).resolve().parent.parent)
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

import tkinter as tk

from desktop.controller import AppController
from desktop.ui import DesktopApp


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("desktop")


def main() -> int:
    """Запуск приложения DroidHub Desktop."""
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print("=" * 60)
        print("Ошибка инициализации графической подсистемы (Tkinter):")
        print(f"  {exc}")
        print()
        print("Графический дисплей не обнаружен.")
        print("Приложение DroidHub Desktop предназначено для запуска на macOS или Windows")
        print("с установленным графическим окружением.")
        print()
        # Вывод краткой диагностики в консоль
        controller = AppController()
        print(f"Статус ADB: {controller.adb_info.path if controller.adb_info.found else 'Не найден'}")
        print(f"Статус scrcpy: {controller.scrcpy_info.path if controller.scrcpy_info.found else 'Не найден'}")
        print("=" * 60)
        return 1

    try:
        app = DesktopApp(root)
        root.mainloop()
        return 0
    except KeyboardInterrupt:
        logger.info("Получен сигнал прерывания, завершение работы...")
        return 0
    except Exception as exc:
        logger.exception("Критическая ошибка работы приложения: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
