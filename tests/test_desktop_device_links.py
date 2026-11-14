"""Тесты модуля явной привязки USB ADB серийных номеров к ID DroidHub."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from unittest.mock import patch
import pytest

from desktop.device_links import (
    DeviceLink,
    DeviceLinkConflictError,
    DeviceLinkCorruptedFileError,
    DeviceLinkError,
    DeviceLinkLimitError,
    DeviceLinkValidationError,
    LinkRegistry,
    SCHEMA_VERSION,
    validate_identifier,
)


class TestValidateIdentifier:
    """Тестирование валидации идентификаторов."""

    def test_valid_identifiers(self) -> None:
        """Корректные идентификаторы успешно проходят проверку."""
        assert validate_identifier("device-1234") == "device-1234"
        assert validate_identifier("FA6AA0301234") == "FA6AA0301234"
        assert validate_identifier("emulator-5554") == "emulator-5554"
        assert validate_identifier("phone_hub_99") == "phone_hub_99"
        assert validate_identifier("Pixel 7a") == "Pixel 7a"
        assert validate_identifier("a" * 256) == "a" * 256

    @pytest.mark.parametrize(
        "invalid_val",
        [
            None,
            123,
            True,
            False,
            3.14,
            [],
            {},
        ],
    )
    def test_non_string_types(self, invalid_val: object) -> None:
        """Нестроковые типы вызывают ошибку валидации."""
        with pytest.raises(DeviceLinkValidationError, match="должен быть строкой"):
            validate_identifier(invalid_val)  # type: ignore[arg-type]

    @pytest.mark.parametrize("empty_val", ["", "   ", "\t", "\n", "\r\n"])
    def test_empty_or_whitespace(self, empty_val: str) -> None:
        """Пустые строки и строки только из пробелов запрещены."""
        with pytest.raises(DeviceLinkValidationError, match="не может быть пустым"):
            validate_identifier(empty_val)

    @pytest.mark.parametrize(
        "surrounded_val",
        [" device", "device ", "  device  ", "\tdevice", "device\n"],
    )
    def test_leading_trailing_whitespace(self, surrounded_val: str) -> None:
        """Пробелы по краям строго запрещены."""
        with pytest.raises(DeviceLinkValidationError):
            validate_identifier(surrounded_val)

    def test_too_long(self) -> None:
        """Превышение максимальной длины вызывает ошибку."""
        with pytest.raises(DeviceLinkValidationError, match="превышает максимальную длину"):
            validate_identifier("a" * 257)

    @pytest.mark.parametrize(
        "control_char",
        [
            "\x00",
            "\x01",
            "\x08",
            "\x0b",
            "\x0c",
            "\x1b",
            "\x7f",
            "\u0080",
            "\u009f",
        ],
    )
    def test_control_characters(self, control_char: str) -> None:
        """Управляющие и непечатные символы запрещены."""
        with pytest.raises(DeviceLinkValidationError, match="недопустимый управляющий или непечатный"):
            validate_identifier(f"serial{control_char}123")

    @pytest.mark.parametrize(
        "url_val",
        [
            "http://localhost:8080",
            "https://hub.example.com/api",
            "ws://127.0.0.1:5000",
            "wss://secure.hub/ws",
            "ftp://files.local",
            "tcp://192.168.1.1:5555",
            "file:///etc/hosts",
            "ssh://user@server:22",
            "adb://192.168.1.5:5555",
            "intent://package/#Intent;scheme=android;end",
            "grpc://127.0.0.1:50051",
            "custom-scheme://test",
        ],
    )
    def test_urls_forbidden(self, url_val: str) -> None:
        """Сетевые URL и адреса запрещены в качестве идентификаторов."""
        with pytest.raises(DeviceLinkValidationError, match="не может быть сетевым URL"):
            validate_identifier(url_val)

    @pytest.mark.parametrize(
        "auth_val",
        [
            "Bearer eyJhbGciOi...",
            "Basic YWRtaW46cGFzcw==",
            "Token secret123",
        ],
    )
    def test_auth_headers_forbidden(self, auth_val: str) -> None:
        """Учетные данные и токены запрещены в качестве идентификаторов."""
        with pytest.raises(DeviceLinkValidationError, match="учетные данные или токены авторизации"):
            validate_identifier(auth_val)


class TestLinkRegistryRoundTrip:
    """Тестирование кругового цикла сохранения и чтения данных."""

    def test_empty_registry_when_file_missing(self, tmp_path: Path) -> None:
        """При отсутствии файла реестр возвращает пустой список без ошибок."""
        storage_file = tmp_path / "links.json"
        reg = LinkRegistry(storage_file)

        assert reg.load() == []
        assert reg.list() == []
        assert len(reg) == 0
        assert not reg.is_corrupted
        assert not storage_file.exists()

    def test_save_load_round_trip(self, tmp_path: Path) -> None:
        """Сохранение и повторное чтение связей полностью совпадают."""
        storage_file = tmp_path / "links.json"
        reg1 = LinkRegistry(storage_file)

        link1 = reg1.link("serial-aaa", "hub-111")
        link2 = reg1.link("serial-bbb", "hub-222")
        link3 = reg1.link("serial-ccc", "hub-333")

        assert isinstance(link1, DeviceLink)
        assert link1.serial == "serial-aaa"
        assert link1.hub_id == "hub-111"
        assert link1.hub_device_id == "hub-111"
        assert link1.to_dict() == {"serial": "serial-aaa", "hub_id": "hub-111"}

        assert len(reg1) == 3
        assert storage_file.exists()

        # Создаем второй независимый экземпляр и загружаем
        reg2 = LinkRegistry(storage_file)
        loaded = reg2.load()

        assert len(loaded) == 3
        assert reg2.list() == [
            DeviceLink("serial-aaa", "hub-111"),
            DeviceLink("serial-bbb", "hub-222"),
            DeviceLink("serial-ccc", "hub-333"),
        ]

        # Проверка вспомогательных методов
        assert reg2.get_by_serial("serial-aaa") == DeviceLink("serial-aaa", "hub-111")
        assert reg2.get_by_hub_id("hub-222") == DeviceLink("serial-bbb", "hub-222")
        assert reg2.get("serial-ccc") == DeviceLink("serial-ccc", "hub-333")
        assert reg2.get_hub_id("serial-aaa") == "hub-111"
        assert reg2.get_serial("hub-333") == "serial-ccc"

        assert reg2.get_by_serial("unknown") is None
        assert reg2.get_by_hub_id("unknown") is None

        # Проверка операторов in и iter
        assert "serial-aaa" in reg2
        assert "hub-222" in reg2
        assert "nonexistent" not in reg2
        assert DeviceLink("serial-aaa", "hub-111") in reg2
        assert DeviceLink("serial-aaa", "hub-999") not in reg2
        assert list(iter(reg2)) == reg2.list()

    def test_json_formatting_and_version(self, tmp_path: Path) -> None:
        """JSON-файл содержит версию 1, валидную структуру и форматирование с отступами."""
        storage_file = tmp_path / "links.json"
        reg = LinkRegistry(storage_file)
        reg.link("FA6AA0301234", "phone_remote_1")

        raw_content = storage_file.read_text(encoding="utf-8")
        assert "\n" in raw_content
        assert "  " in raw_content  # отступ 2 пробела

        data = json.loads(raw_content)
        assert data["version"] == SCHEMA_VERSION
        assert data["links"] == [
            {"serial": "FA6AA0301234", "hub_id": "phone_remote_1"}
        ]


class TestLinkRegistryConflicts:
    """Тестирование проверки конфликтов связей 1:1 с обеих сторон."""

    def test_conflict_on_same_serial_different_hub_id(self, tmp_path: Path) -> None:
        """Попытка привязать уже занятый serial к другому hub_id вызывает DeviceLinkConflictError."""
        storage_file = tmp_path / "links.json"
        reg = LinkRegistry(storage_file)
        reg.link("serial-1", "hub-1")

        with pytest.raises(DeviceLinkConflictError, match="уже связан с устройством хаба 'hub-1'"):
            reg.link("serial-1", "hub-2")

        # Существующая связь не изменилась
        assert reg.get_hub_id("serial-1") == "hub-1"
        assert len(reg) == 1

    def test_conflict_on_same_hub_id_different_serial(self, tmp_path: Path) -> None:
        """Попытка привязать уже занятый hub_id к другому serial вызывает DeviceLinkConflictError."""
        storage_file = tmp_path / "links.json"
        reg = LinkRegistry(storage_file)
        reg.link("serial-1", "hub-1")

        with pytest.raises(DeviceLinkConflictError, match="уже связано с серийным номером 'serial-1'"):
            reg.link("serial-2", "hub-1")

        # Существующая связь не изменилась
        assert reg.get_serial("hub-1") == "serial-1"
        assert len(reg) == 1

    def test_relink_requires_explicit_unlink(self, tmp_path: Path) -> None:
        """Связь заменяется только после явного unlink."""
        storage_file = tmp_path / "links.json"
        reg = LinkRegistry(storage_file)
        reg.link("serial-1", "hub-1")

        # Явный unlink освобождает оба идентификатора
        assert reg.unlink("serial-1") is True
        assert len(reg) == 0

        # Теперь serial-1 можно связать с hub-2
        reg.link("serial-1", "hub-2")
        assert reg.get_hub_id("serial-1") == "hub-2"

        # А hub-1 можно связать с serial-2
        reg.link("serial-2", "hub-1")
        assert reg.get_serial("hub-1") == "serial-2"


class TestLinkRegistryIdempotency:
    """Тестирование идемпотентности операций link и unlink."""

    def test_link_same_pair_is_idempotent(self, tmp_path: Path) -> None:
        """Повторный вызов link с теми же параметрами успешен и ничего не дублирует."""
        storage_file = tmp_path / "links.json"
        reg = LinkRegistry(storage_file)

        res1 = reg.link("serial-1", "hub-1")
        res2 = reg.link("serial-1", "hub-1")

        assert res1 == res2
        assert len(reg) == 1
        assert reg.list() == [DeviceLink("serial-1", "hub-1")]

    def test_unlink_by_serial_idempotency(self, tmp_path: Path) -> None:
        """Первый unlink возвращает True, повторный unlink возвращает False."""
        storage_file = tmp_path / "links.json"
        reg = LinkRegistry(storage_file)
        reg.link("serial-1", "hub-1")

        assert reg.unlink("serial-1") is True
        assert reg.unlink("serial-1") is False
        assert reg.unlink("nonexistent") is False
        assert len(reg) == 0

    def test_unlink_by_hub_id_idempotency(self, tmp_path: Path) -> None:
        """Удаление по hub_id работает корректно и идемпотентно."""
        storage_file = tmp_path / "links.json"
        reg = LinkRegistry(storage_file)
        reg.link("serial-1", "hub-1")

        assert reg.unlink(hub_id="hub-1") is True
        assert reg.unlink(hub_id="hub-1") is False
        assert reg.unlink(hub_id="nonexistent") is False
        assert len(reg) == 0

    def test_unlink_both_matching(self, tmp_path: Path) -> None:
        """Удаление с указанием обоих корректных параметров."""
        storage_file = tmp_path / "links.json"
        reg = LinkRegistry(storage_file)
        reg.link("serial-1", "hub-1")

        assert reg.unlink("serial-1", "hub-1") is True
        assert reg.unlink("serial-1", "hub-1") is False

    def test_unlink_both_mismatch_raises_conflict(self, tmp_path: Path) -> None:
        """Удаление с несогласованными параметрами вызывает ошибку конфликта."""
        storage_file = tmp_path / "links.json"
        reg = LinkRegistry(storage_file)
        reg.link("serial-1", "hub-1")
        reg.link("serial-2", "hub-2")

        with pytest.raises(DeviceLinkConflictError, match="Несоответствие"):
            reg.unlink("serial-1", "hub-2")

        # Ни одна связь не удалена
        assert len(reg) == 2

    def test_unlink_no_arguments_raises_validation_error(self, tmp_path: Path) -> None:
        """Вызов unlink без параметров вызывает DeviceLinkValidationError."""
        storage_file = tmp_path / "links.json"
        reg = LinkRegistry(storage_file)
        with pytest.raises(DeviceLinkValidationError, match="необходимо указать хотя бы"):
            reg.unlink()


class TestLinkRegistryLimits:
    """Тестирование лимита количества записей в реестре."""

    def test_max_entries_limit(self, tmp_path: Path) -> None:
        """Превышение лимита max_entries вызывает DeviceLinkLimitError."""
        storage_file = tmp_path / "links.json"
        reg = LinkRegistry(storage_file, max_entries=2)

        reg.link("s1", "h1")
        reg.link("s2", "h2")

        # Повторный вызов существующей связи проходит (идемпотентность)
        reg.link("s1", "h1")

        # Добавление 3-й связи вызывает ошибку
        with pytest.raises(DeviceLinkLimitError, match="достигнут максимальный лимит записей"):
            reg.link("s3", "h3")

        assert len(reg) == 2


class TestCorruptionAndNoOverwrite:
    """Тестирование обработки повреждений и запрета перезаписи поврежденных файлов."""

    @pytest.mark.parametrize(
        "broken_content",
        [
            "",  # пустой файл
            "   \n",  # пробелы
            "{not a valid json",  # синтаксическая ошибка JSON
            "[1, 2, 3]",  # корень не объект
            '"string"',  # корень строка
            '{"version": 2, "links": []}',  # неподдерживаемая версия
            '{"version": "1", "links": []}',  # версия не int
            '{"links": []}',  # нет поля version
            '{"version": 1}',  # нет поля links
            '{"version": 1, "links": "not a list"}',  # links не список
            '{"version": 1, "links": [], "extra_key": "forbidden"}',  # лишние поля в корне
            '{"version": 1, "links": ["not an object"]}',  # элемент не словарь
            '{"version": 1, "links": [{"serial": "s1"}]}',  # нет hub_id
            '{"version": 1, "links": [{"hub_id": "h1"}]}',  # нет serial
            '{"version": 1, "links": [{"serial": "s1", "hub_id": "h1", "token": "leak"}]}',  # лишние поля
            '{"version": 1, "links": [{"serial": "s1", "hub_id": "h1"}, {"serial": "s1", "hub_id": "h2"}]}',  # дубликат serial
            '{"version": 1, "links": [{"serial": "s1", "hub_id": "h1"}, {"serial": "s2", "hub_id": "h1"}]}',  # дубликат hub_id
        ],
    )
    def test_corrupted_file_raises_error(
        self, tmp_path: Path, broken_content: str
    ) -> None:
        """При любых повреждениях структуры или синтаксиса выбрасывается DeviceLinkCorruptedFileError."""
        storage_file = tmp_path / "corrupted.json"
        storage_file.write_text(broken_content, encoding="utf-8")

        reg = LinkRegistry(storage_file)
        with pytest.raises(DeviceLinkCorruptedFileError):
            reg.load()

        assert reg.is_corrupted

    def test_refuse_to_overwrite_corrupted_file(self, tmp_path: Path) -> None:
        """Поврежденный файл ни при каких обстоятельствах не перезаписывается."""
        storage_file = tmp_path / "corrupted.json"
        initial_content = '{"version": 1, "invalid_json_content": true'
        storage_file.write_text(initial_content, encoding="utf-8")

        reg = LinkRegistry(storage_file)
        with pytest.raises(DeviceLinkCorruptedFileError):
            reg.load()

        # Попытка вызвать save()
        with pytest.raises(DeviceLinkCorruptedFileError, match="Отказ от перезаписи"):
            reg.save()

        # Попытка вызвать link()
        with pytest.raises(DeviceLinkCorruptedFileError):
            reg.link("new_s", "new_h")

        # Попытка вызвать unlink()
        with pytest.raises(DeviceLinkCorruptedFileError):
            reg.unlink("new_s")

        # Попытка вызвать list()
        with pytest.raises(DeviceLinkCorruptedFileError):
            reg.list()

        # Содержимое файла на диске осталось исходным и не повреждено перезаписью
        assert storage_file.read_text(encoding="utf-8") == initial_content

    def test_direct_save_without_load_checks_file_integrity(self, tmp_path: Path) -> None:
        """Даже если load() не вызывался, save() проверяет файл на диске перед перезаписью."""
        storage_file = tmp_path / "corrupted.json"
        initial_content = "{broken json syntax"
        storage_file.write_text(initial_content, encoding="utf-8")

        reg = LinkRegistry(storage_file, autoload=False)
        with pytest.raises(DeviceLinkCorruptedFileError):
            reg.save()

        assert storage_file.read_text(encoding="utf-8") == initial_content

    def test_direct_save_without_load_preserves_existing_data(self, tmp_path: Path) -> None:
        """Вызов save() на неинициализированном экземпляре не стирает существующие данные."""
        storage_file = tmp_path / "links.json"
        reg1 = LinkRegistry(storage_file)
        reg1.link("serial-1", "hub-1")
        reg1.link("serial-2", "hub-2")
        assert len(reg1) == 2

        # Создаем второй экземпляр БЕЗ предварительного load() и сразу вызываем save()
        reg2 = LinkRegistry(storage_file, autoload=False)
        assert not reg2._loaded
        reg2.save()

        # Проверяем, что существующие связи не затерлись
        reg3 = LinkRegistry(storage_file)
        links = reg3.load()
        assert len(links) == 2
        assert reg3.get_hub_id("serial-1") == "hub-1"
        assert reg3.get_hub_id("serial-2") == "hub-2"
        assert reg3.list() == [
            DeviceLink("serial-1", "hub-1"),
            DeviceLink("serial-2", "hub-2"),
        ]


class TestAtomicityOnError:
    """Тестирование атомарности записи и корректности отката при сбоях."""

    def test_atomic_save_failure_keeps_original_file(self, tmp_path: Path) -> None:
        """При сбое os.replace оригинальный файл остается нетронутым, временные файлы удаляются."""
        storage_file = tmp_path / "links.json"
        reg = LinkRegistry(storage_file)
        reg.link("initial_s", "initial_h")

        original_content = storage_file.read_text(encoding="utf-8")

        # Имитируем сбой диска/записи во время os.replace
        with patch("desktop.device_links.os.replace", side_effect=OSError("Disk write error")):
            with pytest.raises(OSError, match="Disk write error"):
                reg.link("second_s", "second_h")

        # Файл на диске не изменился
        assert storage_file.read_text(encoding="utf-8") == original_content

        # В реестре в памяти состояние откатилось
        assert reg.list() == [DeviceLink("initial_s", "initial_h")]

        # В каталоге нет оставшихся временных файлов .tmp
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_atomic_unlink_failure_rolls_back_memory(self, tmp_path: Path) -> None:
        """При сбое записи во время unlink состояние в памяти возвращается назад."""
        storage_file = tmp_path / "links.json"
        reg = LinkRegistry(storage_file)
        reg.link("initial_s", "initial_h")

        with patch("desktop.device_links.os.replace", side_effect=OSError("Disk failure")):
            with pytest.raises(OSError, match="Disk failure"):
                reg.unlink("initial_s")

        assert reg.list() == [DeviceLink("initial_s", "initial_h")]


class TestNoSecretsAndNoUrls:
    """Тестирование отсутствия сетевых адресов, URL и учетных данных в файле."""

    def test_saved_json_contains_no_secrets_or_urls(self, tmp_path: Path) -> None:
        """Сохраненный файл содержит только разрешенные поля и никаких сетевых URL или секретов."""
        storage_file = tmp_path / "links.json"
        reg = LinkRegistry(storage_file)
        reg.link("FA6AA0301234", "phone_hub_42")
        reg.link("emulator-5554", "phone_hub_43")

        raw_text = storage_file.read_text(encoding="utf-8")
        data = json.loads(raw_text)

        # Проверка корневых ключей
        assert set(data.keys()) == {"version", "links"}

        # Проверка структуры записей
        for entry in data["links"]:
            assert set(entry.keys()) == {"serial", "hub_id"}

        # Проверка отсутствия секретов и адресов
        forbidden_keywords = [
            "url",
            "server",
            "host",
            "port",
            "token",
            "password",
            "secret",
            "auth",
            "credential",
            "api_key",
            "http:",
            "https:",
            "ws:",
            "ftp:",
        ]
        lower_raw = raw_text.lower()
        for kw in forbidden_keywords:
            assert kw not in lower_raw, f"Forbidden keyword '{kw}' found in storage JSON"


class TestFilePermissions:
    """Тестирование прав доступа к файлу (только владелец 0600)."""

    def test_file_permissions_owner_only(self, tmp_path: Path) -> None:
        """На POSIX системах права файла устанавливаются в 0600 (только для владельца)."""
        storage_file = tmp_path / "secure_links.json"
        reg = LinkRegistry(storage_file)
        reg.link("s1", "h1")

        if os.name == "posix":
            file_stat = os.stat(storage_file)
            file_mode = stat.S_IMODE(file_stat.st_mode)
            assert file_mode == 0o600, f"Expected permissions 0600, got {oct(file_mode)}"
