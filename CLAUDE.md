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
- Python 3.11+ (на сервере 3.14): современный синтаксис разрешён (`X | Y`, `match`, `list[str]`). Локально тесты гоняем на Python 3.11+ (uv), системный 3.9 на Mac не используем.
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

## Контракты этапа 2

### hub/auth.py
`hash_password(pw: str) -> str` (stdlib scrypt/pbkdf2, формат `algo$params$salt$hash`), `verify_password(pw, hashed) -> bool` (constant-time). `class LoginLimiter`: `__init__(max_attempts=5, window_s=300)`, `check(key: str) -> bool` (можно ли пробовать), `fail(key)`, `reset(key)`.
FastAPI-зависимости: `require_user(request) -> str` (логин из сессии, иначе 401 для /api и редирект на /login для страниц обрабатывает веб-слой), `require_api(request) -> str` (сессия ИЛИ `Authorization: Bearer <api_token>`, возвращает actor: логин или `"api"`; токены сравнивать через `hmac.compare_digest`).
`auth_router` (APIRouter без префикса): `POST /login` (form: username, password; ограничение попыток по IP; успех -> сессия `request.session["user"]`, редирект 303 на `/`; ошибка -> редирект на `/login?error=1`), `POST /logout`.
`python -m hub.auth hash` спрашивает пароль через getpass и печатает хэш для `.env`.
Сессия: starlette `SessionMiddleware` подключается в `main.py` (ключ `session_secret`, cookie `https_only` если `HUB_COOKIE_SECURE=true`).

### hub/main.py
`create_app(settings: Settings | None = None) -> FastAPI`: собирает настройки, `DeviceManager`, `init_audit`, `actions.set_manager`, SessionMiddleware, подключает `auth_router`, `hub.api.routes.api_router` (префикс `/api` уже внутри роутера), `hub.web.views.web_router`, статику `/static` из `hub/web/static`; lifespan запускает/останавливает manager. `app = create_app()` на уровне модуля НЕ создавать (ленивый `def app_factory()` для uvicorn `--factory`). Запуск: `uvicorn hub.main:create_app --factory --host 127.0.0.1 --port 8090`.

### hub/api/routes.py
`api_router = APIRouter(prefix="/api", dependencies=[Depends(require_api)])`. Эндпоинты из раздела «HTTP API» выше + `GET /api/health` (без авторизации — вынести отдельным роутером `public_router`, также подключается в main). Ошибки adb -> 502 с русским текстом, неизвестный device_id -> 404, `shell` не отдавать в HTTP API вообще. Лимит `rate_limit_per_min` действий на устройство (скользящее окно в памяти, 429). Все действия вызывают функции из `hub/devices/actions.py` с `actor=` из `require_api`.

### hub/web/views.py
`web_router`: `GET /login` (страница входа), `GET /` (список устройств, нужна сессия, иначе редирект на /login), `GET /device/{id}` (экран телефона). Шаблоны Jinja2 в `hub/web/templates/`, статика в `hub/web/static/` (`app.js`, `style.css`). Страницы работают только через `/api/...` с cookie-сессией. Никаких CDN и фреймворков, только vanilla JS. Адаптивная вёрстка (телефон), тач-события, тёмная тема. Экран: обновление скриншота 1–3 раза в секунду (не накладывать запросы друг на друга), тап по картинке с пересчётом координат из размера картинки в реальные (размер экрана брать из заголовка ответа `X-Screen-Width`/`X-Screen-Height`, которые добавляет `/screenshot`), свайп мышью/пальцем, поле ввода текста, кнопки Назад/Домой/Недавние/Питание, список приложений с запуском.

## Контракты этапа 3.5: безопасность телефонов (алерты, питание, автопробуждение)

### hub/devices/actions.py (добавляется)
`async ensure_awake(device_id, *, actor="system") -> bool`: если экран выключен (по `dumpsys power`/`dumpsys display`: Display Power state OFF / mWakefulness != Awake), послать `KEYCODE_WAKEUP`, при наличии свайпа-блокировки свайпнуть вверх, подождать до 3 с пока экран не включится; True если пришлось будить. Вызывается автоматически в начале screenshot, ui_dump, tap, swipe, long_press, type_text, key (кроме power), open_app, open_url. Кеш состояния экрана не дольше 3 с.
`async power(device_id, action: str, *, actor="system") -> None`: action одно из `wake`, `sleep` (KEYCODE_SLEEP), `reboot` (`reboot`), `shutdown` (`reboot -p`, при ошибке `svc power shutdown`). Пишет audit с action=`power:<action>`.

### HTTP API (hub/api/routes.py)
`POST /api/devices/{id}/power` тело `{"action": "wake|sleep|reboot|shutdown", "confirm": bool}`; для `reboot` и `shutdown` без `"confirm": true` отвечать 400 с русским текстом. Остальное как у остальных POST (404, 502, 429 по общим правилам). В MCP эти действия НЕ добавлять.

### hub/config.py (добавляется в Settings, все с дефолтами)
`telegram_token: str = ""`, `telegram_chat_id: str = ""` (env `HUB_TELEGRAM_TOKEN`, `HUB_TELEGRAM_CHAT_ID`), `temp_warn_c: float = 40.0`, `temp_alert_c: float = 45.0`, `temp_shutdown_c: float = 50.0` (0 отключает автовыключение), `full_charge_hours: float = 6.0`, `offline_alert_min: float = 10.0`, `alert_cooldown_min: float = 30.0`.

### hub/alerts.py
`class AlertMonitor`: `__init__(manager, settings, notifier=None)`, `async start()/stop()` — фоновый цикл раз в 30 с по `manager.statuses()`. Правила: температура >= warn — предупреждение, >= alert — тревога с inline-кнопкой «Выключить <имя>», >= shutdown_c (если не 0) — вызвать `actions.power(id, "shutdown", actor="alerts")` и сообщить; заряд 100% и подключено дольше `full_charge_hours` — напоминание «заряд держится на 100%»; устройство оффлайн дольше `offline_alert_min` — сообщение, при возврате «снова онлайн». Кулдаун на каждое правило и устройство (`alert_cooldown_min`), температурные тревоги не молчат при росте на ≥2 °C. `class TelegramNotifier`: `send(text, buttons=None)` через `httpx` к api.telegram.org (токен в URL не логировать), и long-polling `getUpdates` для нажатий inline-кнопок (callback_data `off:<device_id>` → ответить подтверждением «Выключить? Да/Нет» → при «Да» `actions.power(... "shutdown", actor="telegram")`); реагировать ТОЛЬКО на `telegram_chat_id`. Если токен пуст — монитор работает, но шлёт только в лог. Подключение в lifespan `hub/main.py`.

### ИЗМЕНЕНИЕ контракта hub/alerts.py (бот Telegram общий для нескольких проектов)
Хаб ТОЛЬКО отправляет (`sendMessage`), НИКОГДА не вызывает `getUpdates`, не ставит webhook, не обрабатывает `callback_query` (иначе конфликт 409 и кража апдейтов у других проектов). Каждое сообщение начинается с префикса `[<alert_tag>]` (по умолчанию `droidhub`). Вместо callback-кнопок: inline-кнопка с `url` = `<public_url>/device/<id>` («Открыть панель»); если public_url пуст, кнопки нет. Новые поля Settings: `alert_tag: str = "droidhub"` (env `HUB_ALERT_TAG`), `public_url: str = ""` (env `HUB_PUBLIC_URL`). Автовыключение при `temp_shutdown_c` остаётся. В тревожном сообщении явно писать: «Зарядка не остановлена: при подключённом кабеле батарея продолжает заряжаться, отключи питание физически».
