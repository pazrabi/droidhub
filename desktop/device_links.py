"""Хранилище явных связей 1:1 между локальными USB ADB серийными номерами и ID DroidHub.

Модуль предоставляет компактный и безопасный API для управления персистентным
реестром связей без зависимостей от внешних библиотек (только стандартная библиотека Python).

Ключевые гарантии:
- Строго 1:1 отношение: один serial и один hub_id могут быть связаны только друг с другом.
- Никаких сетевых вызовов, ADB-команд, GUI или автосопоставления.
- Не хранит URL-адреса, токены, пароли или другие учетные данные.
- Версионированный читабельный JSON с проверкой схемы.
- Защита от перезаписи поврежденного файла.
- Атомарное сохранение через временный файл + os.replace.
- Ограничение прав доступа только для владельца (0600) на поддерживаемых ОС.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Iterable, Iterator
import unicodedata

# Текущая версия схемы файла хранения
SCHEMA_VERSION: int = 1

# Разумный лимит длины идентификатора (серийного номера или hub id)
MAX_ID_LENGTH: int = 256

# Разумный лимит количества записей в одном реестре
DEFAULT_MAX_ENTRIES: int = 1000

# Запрещенные префиксы сетевых URL/схем
FORBIDDEN_URL_PREFIXES = (
    "http://",
    "https://",
    "ftp://",
    "ftps://",
    "ws://",
    "wss://",
    "tcp://",
)

# Запрещенные префиксы заголовков авторизации / учетных данных
FORBIDDEN_AUTH_PREFIXES = (
    "bearer ",
    "basic ",
    "token ",
)

# Разрешенные ключи в корневом объекте JSON
ALLOWED_ROOT_KEYS = frozenset({"version", "links"})

# Разрешенные ключи в объекте записи связи
ALLOWED_ENTRY_KEYS = frozenset({"serial", "hub_id"})


class DeviceLinkError(Exception):
    """Базовое исключение для ошибок модуля device_links."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


class DeviceLinkValidationError(DeviceLinkError):
    """Ошибка валидации идентификаторов или ограничений реестра."""
    pass


class DeviceLinkConflictError(DeviceLinkError):
    """Конфликт связи 1:1 (serial или hub_id уже заняты другим партнером)."""
    pass


class DeviceLinkCorruptedFileError(DeviceLinkError):
    """Файл хранилища поврежден или нарушена схема версионирования."""
    pass


class DeviceLinkLimitError(DeviceLinkValidationError):
    """Превышено максимальное допустимое количество записей в реестре."""
    pass


@dataclass(frozen=True)
class DeviceLink:
    """Неизменяемая явная связь 1:1 между USB ADB серийным номером и ID DroidHub."""

    serial: str
    hub_id: str

    @property
    def hub_device_id(self) -> str:
        """Псевдоним для hub_id."""
        return self.hub_id

    def to_dict(self) -> dict[str, str]:
        """Возвращает словарь с данными связи."""
        return {"serial": self.serial, "hub_id": self.hub_id}


def validate_identifier(value: Any, name: str = "ID") -> str:
    """Проверяет строгое соответствие строкового идентификатора правилам безопасности.

    Правила:
    - Строго тип str (bool, int, None и прочие типы запрещены)
    - Непустая строка без пробелов по краям
    - Отсутствие управляющих символов (Cc, Cs, ASCII < 32, ASCII 127) и непечатных символов
    - Длина от 1 до MAX_ID_LENGTH (256) символов
    - Запрещены сетевые URL и заголовки авторизации
    """
    if not isinstance(value, str) or isinstance(value, bool):
        raise DeviceLinkValidationError(
            f"{name} должен быть строкой, получено {type(value).__name__}"
        )

    if not value or value.strip() == "":
        raise DeviceLinkValidationError(f"{name} не может быть пустым")

    if value != value.strip():
        raise DeviceLinkValidationError(
            f"{name} не должен содержать пробелов в начале или конце"
        )

    if len(value) > MAX_ID_LENGTH:
        raise DeviceLinkValidationError(
            f"{name} превышает максимальную длину в {MAX_ID_LENGTH} символов"
        )

    for ch in value:
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cs") or ord(ch) < 32 or ord(ch) == 127 or not ch.isprintable():
            raise DeviceLinkValidationError(
                f"{name} содержит недопустимый управляющий или непечатный символ (код {ord(ch)})"
            )

    lower = value.lower()
    if "://" in lower or any(lower.startswith(prefix) for prefix in FORBIDDEN_URL_PREFIXES):
        raise DeviceLinkValidationError(
            f"{name} не может быть сетевым URL или адресом: {value}"
        )

    for prefix in FORBIDDEN_AUTH_PREFIXES:
        if lower.startswith(prefix):
            raise DeviceLinkValidationError(
                f"{name} не может содержать учетные данные или токены авторизации"
            )

    return value


def _parse_and_validate_json(
    raw_text: str, max_entries: int
) -> dict[str, str]:
    """Парсит и валидирует содержимое файла JSON реестра.

    Возвращает отображение serial -> hub_id.
    При любых нарушениях схемы или синтаксиса выбрасывает DeviceLinkCorruptedFileError.
    """
    if not raw_text.strip():
        raise DeviceLinkCorruptedFileError("Файл хранилища пуст")

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise DeviceLinkCorruptedFileError(
            f"Файл хранилища содержит некорректный JSON: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise DeviceLinkCorruptedFileError(
            f"Корневой элемент JSON хранилища должен быть объектом, получено {type(data).__name__}"
        )

    root_keys = set(data.keys())
    extra_keys = root_keys - ALLOWED_ROOT_KEYS
    if extra_keys:
        raise DeviceLinkCorruptedFileError(
            f"JSON хранилища содержит непредвиденные корневые поля: {sorted(extra_keys)}"
        )

    if "version" not in data:
        raise DeviceLinkCorruptedFileError("В JSON хранилища отсутствует обязательное поле 'version'")

    version = data["version"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise DeviceLinkCorruptedFileError("Поле 'version' в JSON хранилища должно быть целым числом")

    if version != SCHEMA_VERSION:
        raise DeviceLinkCorruptedFileError(
            f"Неподдерживаемая версия схемы: {version} (ожидается {SCHEMA_VERSION})"
        )

    if "links" not in data:
        raise DeviceLinkCorruptedFileError("В JSON хранилища отсутствует обязательное поле 'links'")

    links = data["links"]
    if not isinstance(links, list):
        raise DeviceLinkCorruptedFileError(
            f"Поле 'links' в JSON хранилища должно быть списком, получено {type(links).__name__}"
        )

    if len(links) > max_entries:
        raise DeviceLinkCorruptedFileError(
            f"JSON хранилища содержит {len(links)} связей, что превышает лимит {max_entries}"
        )

    serial_to_hub: dict[str, str] = {}
    hub_to_serial: dict[str, str] = {}

    for idx, item in enumerate(links):
        if not isinstance(item, dict):
            raise DeviceLinkCorruptedFileError(
                f"Запись связи по индексу {idx} должна быть объектом, получено {type(item).__name__}"
            )

        item_keys = set(item.keys())
        if item_keys != ALLOWED_ENTRY_KEYS:
            diff = item_keys.symmetric_difference(ALLOWED_ENTRY_KEYS)
            raise DeviceLinkCorruptedFileError(
                f"Запись связи по индексу {idx} содержит недопустимые поля: {sorted(diff)}"
            )

        try:
            serial = validate_identifier(item["serial"], name=f"Link[{idx}].serial")
            hub_id = validate_identifier(item["hub_id"], name=f"Link[{idx}].hub_id")
        except DeviceLinkValidationError as err:
            raise DeviceLinkCorruptedFileError(
                f"Недопустимый идентификатор в записи связи по индексу {idx}: {err}"
            ) from err

        if serial in serial_to_hub:
            raise DeviceLinkCorruptedFileError(
                f"Файл хранилища содержит дубликат serial '{serial}' по индексу {idx}"
            )

        if hub_id in hub_to_serial:
            raise DeviceLinkCorruptedFileError(
                f"Файл хранилища содержит дубликат hub_id '{hub_id}' по индексу {idx}"
            )

        serial_to_hub[serial] = hub_id
        hub_to_serial[hub_id] = serial

    return serial_to_hub


class LinkRegistry:
    """Реестр явных связей 1:1 между USB ADB серийными номерами и ID DroidHub.

    Основные методы:
    - load(): читает и валидирует связи из файла (пустой файл/отсутствие файла -> пустой список).
    - list(): возвращает отсортированный список всех связей DeviceLink.
    - link(serial, hub_id): связывает serial и hub_id, проверяя отсутствие конфликтов 1:1.
    - unlink(serial=..., hub_id=...): удаляет связь по serial, hub_id или обоим.
    - save(): атомарно записывает реестр в файл с правами 0600.
    """

    def __init__(
        self,
        path: str | Path,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        autoload: bool = False,
    ) -> None:
        self.path: Path = Path(path)
        self.max_entries: int = max_entries
        self._serial_to_hub: dict[str, str] = {}
        self._hub_to_serial: dict[str, str] = {}
        self._corrupted: bool = False
        self._loaded: bool = False

        if autoload:
            self.load()

    @property
    def is_corrupted(self) -> bool:
        """Возвращает True, если обнаружено повреждение файла хранилища."""
        return self._corrupted

    @property
    def links(self) -> list[DeviceLink]:
        """Псевдоним для list()."""
        return self.list()

    def _ensure_loaded(self) -> None:
        """Убеждается, что данные загружены из файла, если он существует."""
        if self._corrupted:
            raise DeviceLinkCorruptedFileError(
                f"Файл хранилища '{self.path}' поврежден и не может использоваться"
            )
        if not self._loaded and self.path.exists():
            self.load()
        else:
            self._loaded = True

    def _check_file_integrity_before_save(self) -> None:
        """Проверяет целостность существующего файла перед перезаписью.

        Предотвращает случайную перезапись поврежденного файла, если реестр
        был инициализирован без предварительного load().
        """
        self._ensure_loaded()

    def load(self) -> list[DeviceLink]:
        """Загружает связи из файла хранилища.

        Если файл отсутствует, инициализирует пустой реестр и возвращает [].
        Если файл поврежден, выбрасывает DeviceLinkCorruptedFileError и блокирует
        последующую запись в этот файл.
        """
        if not self.path.exists():
            self._serial_to_hub.clear()
            self._hub_to_serial.clear()
            self._corrupted = False
            self._loaded = True
            return []

        try:
            raw_text = self.path.read_text(encoding="utf-8")
            serial_to_hub = _parse_and_validate_json(raw_text, self.max_entries)
        except DeviceLinkCorruptedFileError:
            self._corrupted = True
            raise
        except OSError as exc:
            self._corrupted = True
            raise DeviceLinkCorruptedFileError(
                f"Не удалось прочитать файл хранилища '{self.path}': {exc}"
            ) from exc

        self._serial_to_hub = serial_to_hub
        self._hub_to_serial = {h: s for s, h in serial_to_hub.items()}
        self._corrupted = False
        self._loaded = True
        return self.list()

    def list(self) -> list[DeviceLink]:
        """Возвращает отсортированный по серийному номеру список связей DeviceLink."""
        self._ensure_loaded()
        return [
            DeviceLink(serial=s, hub_id=h)
            for s, h in sorted(self._serial_to_hub.items())
        ]

    def link(self, serial: str, hub_id: str) -> DeviceLink:
        """Явно связывает 1:1 локальный USB ADB serial и remote DroidHub id.

        Параметры:
            serial: серийный номер локального USB ADB устройства.
            hub_id: идентификатор удаленного устройства DroidHub.

        Возвращает:
            DeviceLink с подтвержденной связью.

        Исключения:
            DeviceLinkValidationError: при некорректных параметрах или превышении лимита.
            DeviceLinkConflictError: если serial или hub_id уже привязаны к другому партнеру.
            DeviceLinkCorruptedFileError: если файл на диске поврежден.
        """
        self._ensure_loaded()

        val_serial = validate_identifier(serial, name="serial")
        val_hub_id = validate_identifier(hub_id, name="hub_id")

        existing_hub = self._serial_to_hub.get(val_serial)
        existing_serial = self._hub_to_serial.get(val_hub_id)

        # Идемпотентность: повторный вызов с идентичной парой
        if existing_hub == val_hub_id and existing_serial == val_serial:
            return DeviceLink(serial=val_serial, hub_id=val_hub_id)

        # Конфликт по serial
        if existing_hub is not None and existing_hub != val_hub_id:
            raise DeviceLinkConflictError(
                f"Серийный номер '{val_serial}' уже связан с устройством хаба '{existing_hub}'. "
                f"Сначала выполните unlink."
            )

        # Конфликт по hub_id
        if existing_serial is not None and existing_serial != val_serial:
            raise DeviceLinkConflictError(
                f"Устройство хаба '{val_hub_id}' уже связано с серийным номером '{existing_serial}'. "
                f"Сначала выполните unlink."
            )

        # Проверка лимита количества записей
        if len(self._serial_to_hub) >= self.max_entries:
            raise DeviceLinkLimitError(
                f"Невозможно создать связь: достигнут максимальный лимит записей ({self.max_entries})"
            )

        # Транзакционное обновление в памяти с откатом при ошибке сохранения
        old_s2h = dict(self._serial_to_hub)
        old_h2s = dict(self._hub_to_serial)
        self._serial_to_hub[val_serial] = val_hub_id
        self._hub_to_serial[val_hub_id] = val_serial

        try:
            self.save()
        except Exception:
            self._serial_to_hub = old_s2h
            self._hub_to_serial = old_h2s
            raise

        return DeviceLink(serial=val_serial, hub_id=val_hub_id)

    def unlink(
        self,
        serial: str | None = None,
        hub_id: str | None = None,
    ) -> bool:
        """Удаляет связь 1:1 по serial, hub_id или обоим.

        Параметры:
            serial: серийный номер ADB (опционально, если передан hub_id).
            hub_id: идентификатор DroidHub (опционально, если передан serial).

        Возвращает:
            True, если связь найдена и удалена; False, если связь не существовала (идемпотентно).

        Исключения:
            DeviceLinkValidationError: если не переданы идентификаторы или они некорректны.
            DeviceLinkConflictError: если переданы оба параметра, но они не связаны друг с другом.
            DeviceLinkCorruptedFileError: если файл на диске поврежден.
        """
        self._ensure_loaded()

        if serial is None and hub_id is None:
            raise DeviceLinkValidationError(
                "Для unlink необходимо указать хотя бы 'serial' или 'hub_id'"
            )

        val_serial = validate_identifier(serial, name="serial") if serial is not None else None
        val_hub_id = validate_identifier(hub_id, name="hub_id") if hub_id is not None else None

        target_serial: str | None = None
        target_hub_id: str | None = None

        if val_serial is not None and val_hub_id is not None:
            s_linked_hub = self._serial_to_hub.get(val_serial)
            h_linked_serial = self._hub_to_serial.get(val_hub_id)

            if s_linked_hub is None and h_linked_serial is None:
                return False

            if s_linked_hub != val_hub_id or h_linked_serial != val_serial:
                raise DeviceLinkConflictError(
                    f"Несоответствие: серийный номер '{val_serial}' связан с '{s_linked_hub}', "
                    f"а hub_id '{val_hub_id}' связан с '{h_linked_serial}'"
                )

            target_serial = val_serial
            target_hub_id = val_hub_id

        elif val_serial is not None:
            target_hub_id = self._serial_to_hub.get(val_serial)
            if target_hub_id is None:
                return False
            target_serial = val_serial

        elif val_hub_id is not None:
            target_serial = self._hub_to_serial.get(val_hub_id)
            if target_serial is None:
                return False
            target_hub_id = val_hub_id

        # Транзакционное удаление с откатом при сбое сохранения
        old_s2h = dict(self._serial_to_hub)
        old_h2s = dict(self._hub_to_serial)

        del self._serial_to_hub[target_serial]
        del self._hub_to_serial[target_hub_id]

        try:
            self.save()
        except Exception:
            self._serial_to_hub = old_s2h
            self._hub_to_serial = old_h2s
            raise

        return True

    def save(self) -> None:
        """Атомарно сохраняет реестр связей в файл JSON.

        Гарантирует:
        - Versioned читабельный JSON (версия 1, 2 пробела отступа).
        - Отказ от перезаписи поврежденного файла.
        - Атомарную запись через временный файл в том же каталоге + os.replace.
        - Права доступа 0600 (только для владельца) на поддерживаемых ОС.
        - Очистку временных файлов при любых ошибках.
        """
        if self._corrupted:
            raise DeviceLinkCorruptedFileError(
                f"Отказ от перезаписи поврежденного файла хранилища: '{self.path}'"
            )

        self._ensure_loaded()

        payload = {
            "version": SCHEMA_VERSION,
            "links": [
                {"serial": s, "hub_id": h}
                for s, h in sorted(self._serial_to_hub.items())
            ],
        }
        content = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

        target_dir = self.path.parent
        if str(target_dir) and not target_dir.exists():
            target_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(target_dir, 0o700)
            except (OSError, NotImplementedError):
                pass

        dir_for_tmp = str(target_dir) if str(target_dir) else "."
        fd, tmp_path_str = tempfile.mkstemp(
            dir=dir_for_tmp,
            prefix=f".{self.path.name}_tmp_",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_path_str)

        try:
            try:
                os.chmod(tmp_path, 0o600)
            except (OSError, NotImplementedError):
                pass

            with open(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_path, self.path)

            try:
                os.chmod(self.path, 0o600)
            except (OSError, NotImplementedError):
                pass
        except Exception:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            raise

    def get_by_serial(self, serial: str) -> DeviceLink | None:
        """Возвращает DeviceLink по USB ADB серийному номеру или None."""
        self._ensure_loaded()
        hub_id = self._serial_to_hub.get(serial)
        if hub_id is None:
            return None
        return DeviceLink(serial=serial, hub_id=hub_id)

    def get_by_hub_id(self, hub_id: str) -> DeviceLink | None:
        """Возвращает DeviceLink по ID удаленного DroidHub или None."""
        self._ensure_loaded()
        serial = self._hub_to_serial.get(hub_id)
        if serial is None:
            return None
        return DeviceLink(serial=serial, hub_id=hub_id)

    def get_hub_id(self, serial: str) -> str | None:
        """Возвращает связанный hub_id по serial или None."""
        self._ensure_loaded()
        return self._serial_to_hub.get(serial)

    def get_serial(self, hub_id: str) -> str | None:
        """Возвращает связанный serial по hub_id или None."""
        self._ensure_loaded()
        return self._hub_to_serial.get(hub_id)

    def get(self, serial: str) -> DeviceLink | None:
        """Псевдоним для get_by_serial."""
        return self.get_by_serial(serial)

    def __len__(self) -> int:
        self._ensure_loaded()
        return len(self._serial_to_hub)

    def __iter__(self) -> Iterator[DeviceLink]:
        return iter(self.list())

    def __contains__(self, item: Any) -> bool:
        self._ensure_loaded()
        if isinstance(item, DeviceLink):
            return self._serial_to_hub.get(item.serial) == item.hub_id
        if isinstance(item, str):
            return item in self._serial_to_hub or item in self._hub_to_serial
        return False

    def __repr__(self) -> str:
        count = len(self._serial_to_hub) if self._loaded else "unloaded"
        return f"<LinkRegistry path='{self.path}' entries={count} corrupted={self._corrupted}>"
