from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from typing import Any

try:
    from hub.audit import init_audit
except (ImportError, ModuleNotFoundError):
    init_audit = None  # type: ignore

try:
    from hub.config import get_settings, load_devices
except (ImportError, ModuleNotFoundError):
    get_settings = None  # type: ignore
    load_devices = None  # type: ignore

try:
    from hub.devices.manager import DeviceManager
except (ImportError, ModuleNotFoundError):
    DeviceManager = None  # type: ignore

from hub.devices import actions


async def init_cli_manager() -> Any:
    """Инициализирует аудит и менеджер устройств из настроек."""
    if get_settings is None or load_devices is None or DeviceManager is None:
        raise RuntimeError("Модули hub.config или hub.devices.manager недоступны")

    settings = get_settings()
    if init_audit is not None:
        try:
            init_audit(settings.db_path)
        except Exception:
            pass

    devices = load_devices(settings.devices_file)
    manager = DeviceManager(devices)
    actions.set_manager(manager)
    return manager


async def setup_cli() -> Any:
    """Возвращает существующий менеджер или инициализирует новый."""
    try:
        return actions.get_manager()
    except RuntimeError:
        return await init_cli_manager()


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hub.cli",
        description="CLI для управления Android-устройствами в DroidHub",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # devices
    subparsers.add_parser("devices", help="Список настроенных устройств")

    # screenshot
    p_screen = subparsers.add_parser("screenshot", help="Сделать скриншот экрана устройства")
    p_screen.add_argument("device", help="ID устройства (например, realme)")
    p_screen.add_argument(
        "output",
        nargs="?",
        default=None,
        help="Путь к файлу для сохранения (по умолчанию <device>.jpg)",
    )
    p_screen.add_argument(
        "--max-width",
        type=int,
        default=720,
        help="Максимальная ширина скриншота в пикселях (по умолчанию 720)",
    )

    # tap
    p_tap = subparsers.add_parser("tap", help="Тап по координатам (x, y)")
    p_tap.add_argument("device", help="ID устройства")
    p_tap.add_argument("x", type=int, help="Координата X")
    p_tap.add_argument("y", type=int, help="Координата Y")

    # text
    p_text = subparsers.add_parser("text", help="Ввести текст (латиница или кириллица)")
    p_text.add_argument("device", help="ID устройства")
    p_text.add_argument("text", help="Текст для ввода")

    # ui
    p_ui = subparsers.add_parser("ui", help="Дамп элементов интерфейса (uiautomator dump)")
    p_ui.add_argument("device", help="ID устройства")
    p_ui.add_argument("--json", action="store_true", help="Вывод в формате JSON")

    # key
    p_key = subparsers.add_parser("key", help="Нажать клавишу (back, home, recents, enter, power, ...)")
    p_key.add_argument("device", help="ID устройства")
    p_key.add_argument("name", help="Имя клавиши")

    # swipe
    p_swipe = subparsers.add_parser("swipe", help="Выполнить свайп")
    p_swipe.add_argument("device", help="ID устройства")
    p_swipe.add_argument("x1", type=int, help="Начальная X")
    p_swipe.add_argument("y1", type=int, help="Начальная Y")
    p_swipe.add_argument("x2", type=int, help="Конечная X")
    p_swipe.add_argument("y2", type=int, help="Конечная Y")
    p_swipe.add_argument("--ms", type=int, default=300, help="Длительность в мс (по умолчанию 300)")

    # open_app
    p_app = subparsers.add_parser("open_app", help="Запустить приложение")
    p_app.add_argument("device", help="ID устройства")
    p_app.add_argument("package", help="Имя пакета (например, com.android.chrome)")

    # open_url
    p_url = subparsers.add_parser("open_url", help="Открыть URL в браузере")
    p_url.add_argument("device", help="ID устройства")
    p_url.add_argument("url", help="URL адрес")

    return parser


async def async_main(argv: list[str] | None = None) -> int:
    parser = create_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 2

    try:
        if args.command == "devices":
            manager = await setup_cli()
            devices_found = False
            statuses = manager.statuses() if hasattr(manager, "statuses") else []
            if statuses:
                for s in statuses:
                    status_text = "online" if getattr(s, "online", False) else "offline"
                    bat = getattr(s, "battery", None)
                    bat_text = f", батарея: {bat}%" if bat is not None else ""
                    print(f"- {s.id}: {s.name} [{status_text}{bat_text}]")
                    devices_found = True
            else:
                devs: list[Any] = []
                if hasattr(manager, "_devices"):
                    devs = list(manager._devices.values()) if isinstance(manager._devices, dict) else list(manager._devices)
                elif hasattr(manager, "devices"):
                    devs = list(manager.devices.values()) if isinstance(manager.devices, dict) else list(manager.devices)
                elif get_settings is not None and load_devices is not None:
                    try:
                        settings = get_settings()
                        devs = load_devices(settings.devices_file)
                    except Exception:
                        devs = []

                for d in devs:
                    host = getattr(d, "host", "")
                    port = getattr(d, "port", "")
                    addr = f" ({host}:{port})" if host else ""
                    print(f"- {d.id}: {d.name}{addr}")
                    devices_found = True

            if not devices_found:
                print("Нет доступных устройств")
            return 0

        elif args.command == "screenshot":
            await setup_cli()
            data = await actions.screenshot(args.device, max_width=args.max_width, actor="cli")
            out_path = args.output or f"{args.device}.jpg"
            Path(out_path).write_bytes(data)
            print(f"Скриншот сохранён: {out_path} ({len(data)} байт)")
            return 0

        elif args.command == "tap":
            await setup_cli()
            await actions.tap(args.device, args.x, args.y, actor="cli")
            print(f"Тап выполнен на {args.device}: ({args.x}, {args.y})")
            return 0

        elif args.command == "text":
            await setup_cli()
            await actions.type_text(args.device, args.text, actor="cli")
            print(f"Текст введён на {args.device}: {args.text}")
            return 0

        elif args.command == "ui":
            await setup_cli()
            elements = await actions.ui_dump(args.device, actor="cli")
            if args.json:
                print(json.dumps(elements, ensure_ascii=False, indent=2))
            else:
                print(f"Найдено элементов на {args.device}: {len(elements)}")
                for idx, elem in enumerate(elements, start=1):
                    info = []
                    if elem["text"]:
                        info.append(f"text='{elem['text']}'")
                    if elem["resource_id"]:
                        info.append(f"id='{elem['resource_id']}'")
                    if elem["content_desc"]:
                        info.append(f"desc='{elem['content_desc']}'")
                    if elem["clickable"]:
                        info.append("clickable")
                    details = f" ({', '.join(info)})" if info else ""
                    print(f"[{idx}] {elem['class']} center={elem['center']}{details}")
            return 0

        elif args.command == "key":
            await setup_cli()
            await actions.key(args.device, args.name, actor="cli")
            print(f"Клавиша '{args.name}' нажата на {args.device}")
            return 0

        elif args.command == "swipe":
            await setup_cli()
            await actions.swipe(args.device, args.x1, args.y1, args.x2, args.y2, ms=args.ms, actor="cli")
            print(f"Свайп выполнен на {args.device}: ({args.x1}, {args.y1}) -> ({args.x2}, {args.y2})")
            return 0

        elif args.command == "open_app":
            await setup_cli()
            await actions.open_app(args.device, args.package, actor="cli")
            print(f"Приложение '{args.package}' запущено на {args.device}")
            return 0

        elif args.command == "open_url":
            await setup_cli()
            await actions.open_url(args.device, args.url, actor="cli")
            print(f"URL '{args.url}' открыт на {args.device}")
            return 0

        else:
            parser.print_help()
            return 1

    except Exception as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> None:
    code = asyncio.run(async_main(argv))
    if code != 0:
        sys.exit(code)


if __name__ == "__main__":
    main()
