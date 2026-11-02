# Правила проекта Phone Hub

Полный план: PLAN.md. Пакет называется `hub` (папка `hub/` в корне репозитория).

## Общие правила
- Python 3.9+ совместимый код (на сервере 3.11): `from __future__ import annotations`, без `match`, без `X | Y` вне аннотаций.
- Всё асинхронное (asyncio). Тесты: pytest + pytest-asyncio (`asyncio_mode = auto`). Реальный `adb` в тестах НЕ нужен и недоступен: мокать запуск процесса.
- Комментарии и тексты ошибок для пользователя на русском, имена в коде на английском.
- Секреты только из `.env` / переменных окружения (`hub/config.py`), в git не попадают.
- Не трогай файлы вне своей задачи. Общие контракты ниже менять нельзя, только реализовывать.
- Перед завершением запусти `python -m pytest -q` и убедись, что всё зелёное.

## Контракты (сигнатуры, которых держимся)

### hub/config.py
`Settings` (dataclass): `devices_file: str = "config/devices.yaml"`, `db_path: str = "data/hub.db"`, `admin_user: str`, `admin_password_hash: str`, `session_secret: str`, `mcp_token: str`, `api_token: str`, `adb_path: str = "adb"`, `allow_shell: bool = False`, `rate_limit_per_min: int = 60`.
`get_settings() -> Settings` читает переменные окружения `HUB_*` (например `HUB_ADMIN_USER`, `HUB_DB_PATH`) и `.env`.
`DeviceConfig` (dataclass): `id: str`, `name: str`, `host: str`, `port: int = 5555`. `load_devices(path) -> list[DeviceConfig]` читает YAML (`devices: [{id, name, host, port}]`).

### hub/devices/adb.py
`class AdbError(Exception)`, `class DeviceOffline(AdbError)`, `class AdbTimeout(AdbError)`.
`async def run_adb(args: list[str], *, serial: str | None = None, timeout: float = 15.0, binary: bool = False) -> str | bytes` запускает `adb [-s serial] args` через asyncio subprocess; не нулевой код -> `AdbError`/`DeviceOffline` с понятным русским текстом; таймаут -> `AdbTimeout` и убитый процесс.
`async def adb_shell(serial, cmd: str, timeout=15.0) -> str`, `async def connect(host, port) -> bool`.
Путь к adb берётся из `get_settings().adb_path`.

### hub/devices/manager.py
`class DeviceStatus` (dataclass): `id, name, online: bool, battery: int | None, temperature: float | None, screen_on: bool | None, last_seen: float | None, error: str | None`.
`class DeviceManager`: `__init__(devices: list[DeviceConfig])`, `get(device_id) -> DeviceConfig` (KeyError если нет), `serial(device_id) -> str` (`host:port`), `async refresh(device_id) -> DeviceStatus`, `statuses() -> list[DeviceStatus]` (кеш), `async start()` / `async stop()` фоновая проверка каждые 30 с с `adb connect` при обрыве.

### hub/devices/actions.py
Все функции `async`, первый аргумент `device_id: str`, обращаются к менеджеру через `set_manager(manager)` / `get_manager()` на уровне модуля; каждое действие пишет запись в audit.
`screenshot(device_id, max_width=720) -> bytes` (JPEG), `tap(device_id, x, y)`, `swipe(device_id, x1, y1, x2, y2, ms=300)`, `long_press(device_id, x, y, ms=800)`, `type_text(device_id, text)` (латиница через `input text`, кириллица/эмодзи через ADBKeyBoard broadcast `ADB_INPUT_B64`), `key(device_id, name)` (back, home, recents, enter, power, volume_up, volume_down), `open_app(device_id, package)`, `list_apps(device_id) -> list[str]`, `open_url(device_id, url)`, `ui_dump(device_id) -> list[dict]` (элементы: `text, resource_id, class, content_desc, clickable, bounds, center: [x, y]`), `tap_element(device_id, text=None, resource_id=None) -> dict`, `wake_and_unlock(device_id)`, `shell(device_id, cmd)` (только если `allow_shell`, иначе `PermissionError`), `wait(device_id, seconds)`.

### hub/audit.py
`class Audit`: `__init__(db_path)`, `async log(actor: str, device_id: str | None, action: str, params: dict, ok: bool, error: str | None = None)`, `async recent(limit=100) -> list[dict]`. SQLite (stdlib sqlite3 в `asyncio.to_thread`). Модульные `init_audit(db_path)` / `get_audit()`.

### HTTP API (hub/api/routes.py), все пути под `/api`, авторизация: сессия панели ИЛИ заголовок `Authorization: Bearer <api_token>`
`GET /api/devices`, `GET /api/devices/{id}/screenshot?max_width=`, `POST /api/devices/{id}/tap {x,y}`, `/swipe`, `/text {text}`, `/key {name}`, `/open_app {package}`, `GET /api/devices/{id}/apps`, `GET /api/devices/{id}/ui`, `GET /api/audit`.
