# Phone Hub

Серверное приложение для централизованного удалённого управления домашними Android-смартфонами через защищённую сеть Tailscale.

Phone Hub позволяет:
- **Управлять телефонами через веб-браузер** в режиме реального времени: просмотр экрана, тапы, свайпы, ввод текста (включая русский язык), нажатия системных клавиш, открытие приложений и ссылок.
- **Предоставлять доступ к телефонам для нейросетей** (Claude, Gemini pool) через стандартный протокол **MCP (Model Context Protocol)** как к набору инструментов.
- **Использовать ресурсы телефонов** (домашний IP-адрес, мобильный Chrome) для внешних сервисов и ботов (например, парсинга цен авиабилетов в `aviaticket`).

---

## Архитектура

```
 [MacBook / любой браузер]      [Claude, Gemini pool]       [aviaticket бот]
            │ HTTPS                    │ MCP (HTTPS)               │ HTTP API / прокси
            └──────────────┬───────────┴───────────────────────────┘
                           ▼
              ┌─────────────────────────────┐
              │   Caddy (HTTPS, порт 443)   │
              │  hub.193-41-86-92.sslip.io  │
              └──────────────┬──────────────┘
                             │ reverse_proxy (127.0.0.1:8090)
                             ▼
              ┌─────────────────────────────┐
              │   Phone Hub (FastAPI)       │
              │  systemd-сервис, user 'hub' │
              │  MemoryMax=600M, hardening  │
              └──────────────┬──────────────┘
                             │ Tailscale (приватная VPN-сеть)
                             │ ADB по TCP (порт 5555)
              ┌──────────────┴──────────────┐
              ▼                             ▼
       [Realme C21Y]                  [Redmi Note 11]
    дома, на зарядке, Wi-Fi        дома, на зарядке, Wi-Fi
```

- **Связь с телефонами**: исключительно внутри защищённой сети Tailscale (адреса `100.x.x.x`). Наружу порты телефонов не открываются.
- **Управление**: через протокол Android Debug Bridge (ADB) по TCP-порту 5555. Поддерживается ввод кириллицы через [ADBKeyBoard](https://github.com/senbox-org/ADBKeyBoard).
- **Стек**: Python 3.11+ / FastAPI / Uvicorn, SQLite аудит-лог, Caddy HTTPS reverse proxy, systemd с изолированным окружением.

---

## Быстрый старт

### 1. Локальный запуск (для разработки)

```bash
# Клонируйте репозиторий и перейдите в каталог
git clone https://github.com/pazrabi/droidhub.git
cd droidhub

# Создайте и активируйте виртуальное окружение Python
python3 -m venv venv
source venv/bin/activate

# Установите зависимости
pip install -r requirements.txt

# Настройте конфигурацию окружения
cp .env.example .env
# Сгенерируйте хэш пароля администратора:
python -m hub.auth hash
# Отредактируйте .env и укажите полученный хэш и секреты

# Запустите сервер разработки
uvicorn hub.main:create_app --factory --host 127.0.0.1 --port 8090

# Запуск тестов
python -m pytest -q
```

### 2. Деплой на боевой сервер (Ubuntu)

В репозитории подготовлены готовые скрипты и конфигурации в каталоге `deploy/`:

1. **Предпросмотр установки (dry-run)**:
   ```bash
   ./deploy/install.sh --dry-run
   ```
2. **Запуск установки**:
   ```bash
   sudo ./deploy/install.sh
   ```
   Скрипт установки идемпотентен:
   - создаёт системного пользователя `hub` и каталог `/opt/phone-hub`;
   - разворачивает виртуальное окружение и ставит зависимости;
   - регистрирует и активирует systemd-сервис `/etc/systemd/system/phone-hub.service`;
   - добавляет блок домена `hub.193-41-86-92.sslip.io` в `/etc/caddy/Caddyfile`, **не затрагивая** существующий блок gemini-pool;
   - **не перезаписывает** существующие `.env` и `devices.yaml`.

3. **Настройка секретов и запуск**:
   ```bash
   # Отредактируйте переменные окружения
   sudo nano /opt/phone-hub/.env

   # Запустите сервис
   sudo systemctl start phone-hub

   # Проверьте статус и логи
   sudo systemctl status phone-hub
   sudo journalctl -u phone-hub -f
   ```

---

## Как добавить телефон

Подробнейшая пошаговая инструкция для новичка (включая обход защиты Redmi и настройку без SIM-карты) находится в документе:  
👉 [**docs/phone-setup.md**](docs/phone-setup.md).

### Краткий чеклист:
1. Включите **«Режим разработчика»** на телефоне (7 тапов по номеру сборки / версии MIUI).
2. Включите **«Отладку по USB»** и отключите блокировку экрана.
3. *Для Redmi*: включите **«Отладку по USB (настройки безопасности)»** (см. инструкцию по обходу через временную SIM в docs).
4. Установите **Tailscale**, войдите в единый аккаунт и включите **«Постоянный VPN»**.
5. Подключите телефон кабелем к компьютеру и включите сетевую отладку:
   ```bash
   adb tcpip 5555
   ```
6. Установите клавиатуру **ADBKeyBoard** для поддержки кириллицы:
   ```bash
   adb install ADBKeyBoard.apk
   adb shell ime enable com.android.adbkeyboard/.AdbIME
   adb shell ime set com.android.adbkeyboard/.AdbIME
   ```
7. Добавьте телефон в файл конфигурации `config/devices.yaml` (или `/opt/phone-hub/config/devices.yaml` на сервере):
   ```yaml
   devices:
     - id: redmi
       name: Redmi Note 11
       host: 100.64.0.2
       port: 5555
     - id: realme
       name: Realme C21Y
       host: 100.64.0.1
       port: 5555
   ```

---

## Как подключить MCP позже (Этап 3)

После включения этапа 3 Phone Hub будет предоставлять стандартный эндпоинт протокола **Model Context Protocol (MCP)**:  
`https://hub.193-41-86-92.sslip.io/mcp`

### Подключение к Claude Desktop
В файле конфигурации Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json` на macOS):
```json
{
  "mcpServers": {
    "phone-hub": {
      "url": "https://hub.193-41-86-92.sslip.io/mcp",
      "headers": {
        "Authorization": "Bearer <HUB_MCP_TOKEN>"
      }
    }
  }
}
```

### Подключение к Claude Code / claude.ai
Укажите URL эндпоинта `https://hub.193-41-86-92.sslip.io/mcp` и токен доступа из переменной `HUB_MCP_TOKEN`.

### Доступные инструменты для ИИ
- `list_devices()` — список доступных телефонов и их статус подключения.
- `device_status(device_id)` — уровень заряда батареи, температура, состояние экрана.
- `screenshot(device_id)` — снимок экрана в формате JPEG.
- `ui_dump(device_id)` — детальное дерево элементов интерфейса (тексты, id, координаты центров для клика).
- `tap(device_id, x, y)` / `tap_element(device_id, text, resource_id)` — клик по экрану или элементу.
- `swipe(device_id, x1, y1, x2, y2, ms)` — свайп.
- `type_text(device_id, text)` — ввод любого текста (включая русский язык).
- `press_key(device_id, name)` — нажатие клавиш (`back`, `home`, `recents`, `power`, `enter`).
- `open_app(device_id, package)` / `list_apps(device_id)` — запуск и список приложений.
- `open_url(device_id, url)` — открытие ссылки в мобильном браузере.

---

## Безопасность

- **Изоляция сети**: приложение слушает только интерфейс `127.0.0.1:8090`. Доступ снаружи открыт только через Caddy по HTTPS.
- **Приватный ADB**: телефоны не доступны из публичного интернета, связь с ними осуществляется строго по зашифрованному туннелю Tailscale.
- **Хранение секретов**: все пароли, хэши и токены хранятся в `.env` (файл защищён правами `0600` и исключён из Git).
- **Hardening сервиса**: systemd-юнит запускается под отдельным пользователем `hub`, с ограничениями `MemoryMax=600M`, `ProtectSystem=strict`, `NoNewPrivileges=true`, `PrivateTmp=true` и полным сбросом Linux capabilities.
- **Аудит действий**: каждая операция (тап, скриншот, запуск приложения) с именем инициатора (пользователь веб-панели или API/MCP-клиент) протоколируется в SQLite-базе `data/hub.db`.
- **Защита от перегрузки**: лимит действий на устройство (`rate_limit_per_min`) и защита от перебора паролей на странице входа.

---

## Структура репозитория

```
phone-hub/
├── config/
│   └── devices.yaml          # Список подключённых устройств
├── data/                     # SQLite-база аудита (hub.db)
├── deploy/
│   ├── phone-hub.service     # Systemd-юнит с харденингом и MemoryMax=600M
│   ├── Caddyfile.snippet     # Блок reverse_proxy для Caddy
│   └── install.sh            # Идемпотентный скрипт установки (--dry-run)
├── docs/
│   └── phone-setup.md        # Пошаговая инструкция по настройке телефонов
├── hub/
│   ├── main.py               # Точка входа FastAPI приложения
│   ├── config.py             # Настройки из .env и devices.yaml
│   ├── auth.py               # Авторизация (сессии, Bearer-токены, хэширование)
│   ├── audit.py              # Журнал аудита действий
│   ├── cli.py                # Вспомогательный CLI для проверки
│   ├── devices/
│   │   ├── adb.py            # Обёртка над ADB через asyncio subprocess
│   │   ├── manager.py        # Менеджер устройств и мониторинг
│   │   └── actions.py        # Действия (тап, ввод, свайп, скриншот)
│   └── api/                  # REST API роутеры
├── tests/                    # Набор юнит- и интеграционных тестов
├── requirements.txt          # Зависимости Python
├── .env.example              # Пример переменных окружения
└── README.md
```
