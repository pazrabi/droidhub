# Правила проекта Phone Hub

Полный план: PLAN.md. Пакет называется `hub` (папка `hub/` в корне репозитория).

## Для Claude: роль и порядок работы (читать в начале каждой сессии)
- Ты тимлид. Код пишут разработчики из Gemini pool (MCP `gemini-pool`), ты ставишь задачи, проверяешь результат, сливаешь в `main`. Сам пишешь код только для мелочей, общих контрактов и починки того, что пул не смог довести.
- Сначала прочитай `JOURNAL.md` (что сделано, какие задачи в пуле, что дальше) и `PLAN.md` (этапы). Состояние пула проверь через `list_tasks`.
- **Журнал**: каждое действие по проекту (поставил/принял/отклонил задачу, слил ветку, решение, блокер, вопрос владельцу) записывай в `JOURNAL.md` новой строкой в конец сразу, а не пачкой в конце сессии. Формат: `- YYYY-MM-DD HH:MM — что сделано (task_id / ветка / коммит)`. Журнал коммитится вместе с изменениями.
- Репозиторий: https://github.com/pazrabi/droidhub.git, ветка `main`, локально `/Users/moloko/projects/droidhub`. Коммиты локально делаются с `user.email = pazrabi@users.noreply.github.com` (иначе GitHub отклоняет пуш из-за приватной почты).
- Пул: `submit_task` требует `role` (developer), `goal`, `repo`, `allowed_files`; ставь `review=true`, `model` и `review_model` = `gemini-3.8-flash-high` (pro в пуле нет; проверь `list_models`). Сразу после постановки запускай Monitor: `python3 ~/projects/gemini-pool/scripts/pool_watch.py <task_id>...` (timeout 1800000). Задачи с пересекающимися файлами не ставь одновременно.
- Приёмка: `git fetch origin <branch>`, прочитать diff и `task_result`, прогнать `python -m pytest -q`, соответствие контрактам ниже, затем merge в `main`, push, запись в журнал. При `needs_lead` читай замечания ревьюера и решай: доработать самому, переставить задачу с уточнением или отменить.
- Контракты ниже менять нельзя без записи в журнале с причиной; если меняешь, обнови и этот файл, и затронутые задачи.
- Владелец: начинающий, объясняй простыми словами, шаги этапа 0 (Tailscale, телефоны, сервер) делаются вместе с ним руками. Доступ к серверу он даст отдельно; ничего не делай на сервере и в аккаунтах без его явного согласия. Публиковать и пушить в чужие репозитории (в том числе `aviaticket`) без согласия нельзя.
- Безопасность из PLAN.md раздел 5 обязательна: `.env` в git не попадает, `shell` в MCP выключен по умолчанию, ADB только через Tailscale.
- Отвечай владельцу по-русски, коротко: что сделано, что проверено, что нет, что дальше.

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
