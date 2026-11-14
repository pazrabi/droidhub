"""Тесты для desktop/link_storage.py: изолированное хранилище связей по серверам."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from unittest.mock import MagicMock, patch
import pytest

from desktop.adb_client import DeviceInfo
from desktop.device_links import (
    DeviceLink,
    DeviceLinkError,
    DeviceLinkValidationError,
    LinkRegistry,
)
from desktop.hub_client import HubDevice
from desktop.link_service import DeviceLinkService
from desktop.link_storage import (
    LinkStorageError,
    LinkStorageValidationError,
    get_canonical_url,
    get_default_storage_dir,
    get_storage_key,
    get_storage_path,
    normalize_devices_url,
    open_link_service,
)


class TestValidation:
    """Тестирование строгой валидации входных URL без обращения к сети."""

    @pytest.mark.parametrize(
        "invalid_type",
        [
            None,
            123,
            True,
            False,
            3.14,
            [],
            {},
            Path("/tmp"),
        ],
    )
    def test_invalid_types(self, invalid_type: object) -> None:
        """Нестроковые типы вызывают ошибку валидации."""
        with pytest.raises(LinkStorageValidationError, match="must be a string"):
            normalize_devices_url(invalid_type)  # type: ignore[arg-type]

    @pytest.mark.parametrize("empty_val", ["", "   ", "\t", "\n", "\r\n"])
    def test_empty_or_whitespace(self, empty_val: str) -> None:
        """Пустые строки и строки только из пробелов запрещены."""
        with pytest.raises(LinkStorageValidationError, match="cannot be empty"):
            normalize_devices_url(empty_val)

    @pytest.mark.parametrize(
        "non_https_url",
        [
            "http://hub.example.com/api/devices",
            "ftp://hub.example.com/api/devices",
            "ws://hub.example.com/api/devices",
            "wss://hub.example.com/api/devices",
            "tcp://127.0.0.1:5555",
            "file:///tmp/devices",
            "grpc://hub.example.com/api/devices",
            "hub.example.com/api/devices",
            "://hub.example.com/api/devices",
        ],
    )
    def test_forbidden_schemes(self, non_https_url: str) -> None:
        """Разрешена только схема HTTPS."""
        with pytest.raises(LinkStorageValidationError, match="scheme must be 'https'"):
            normalize_devices_url(non_https_url)

    @pytest.mark.parametrize(
        "url_with_query",
        [
            "https://hub.example.com/api/devices?token=secret123",
            "https://hub.example.com/api/devices?page=1&limit=50",
            "https://hub.example.com/api/devices?",
        ],
    )
    def test_query_parameters_forbidden(self, url_with_query: str) -> None:
        """Параметры запроса (query) строго запрещены."""
        with pytest.raises(LinkStorageValidationError, match="must not contain query parameters"):
            normalize_devices_url(url_with_query)

    @pytest.mark.parametrize(
        "url_with_fragment",
        [
            "https://hub.example.com/api/devices#section1",
            "https://hub.example.com/api/devices#token=abc",
            "https://hub.example.com/api/devices#",
        ],
    )
    def test_fragments_forbidden(self, url_with_fragment: str) -> None:
        """Фрагменты (fragment) строго запрещены."""
        with pytest.raises(LinkStorageValidationError, match="must not contain fragments"):
            normalize_devices_url(url_with_fragment)

    @pytest.mark.parametrize(
        "url_with_userinfo",
        [
            "https://admin:password123@hub.example.com/api/devices",
            "https://admin@hub.example.com/api/devices",
            "https://:secret@hub.example.com/api/devices",
            "https://@hub.example.com/api/devices",
        ],
    )
    def test_userinfo_forbidden(self, url_with_userinfo: str) -> None:
        """Учетные данные в URL (userinfo) строго запрещены."""
        with pytest.raises(LinkStorageValidationError, match="must not contain user credentials"):
            normalize_devices_url(url_with_userinfo)

    @pytest.mark.parametrize(
        "missing_host_url",
        [
            "https:///api/devices",
            "https://:443/api/devices",
            "https:/api/devices",
            "https:",
            "https://",
        ],
    )
    def test_missing_host(self, missing_host_url: str) -> None:
        """Отсутствие хоста вызывает ошибку валидации."""
        with pytest.raises(LinkStorageValidationError, match="host is missing"):
            normalize_devices_url(missing_host_url)

    @pytest.mark.parametrize(
        "invalid_port_url",
        [
            "https://hub.example.com:0/api/devices",
            "https://hub.example.com:-1/api/devices",
            "https://hub.example.com:65536/api/devices",
            "https://hub.example.com:notaport/api/devices",
            "https://hub.example.com:/api/devices",
            "https://hub.example.com:",
        ],
    )
    def test_invalid_ports(self, invalid_port_url: str) -> None:
        """Некорректные порты вызывают ошибку валидации."""
        with pytest.raises(LinkStorageValidationError, match="port"):
            normalize_devices_url(invalid_port_url)

    @pytest.mark.parametrize(
        "control_char",
        ["\x00", "\x01", "\x08", "\r", "\n", "\x1b", "\x7f"],
    )
    def test_control_characters_forbidden(self, control_char: str) -> None:
        """Управляющие символы строго запрещены."""
        url = f"https://hub.example.com/api{control_char}/devices"
        with pytest.raises(LinkStorageValidationError, match="control characters"):
            normalize_devices_url(url)

    def test_exception_hierarchy(self) -> None:
        """LinkStorageValidationError наследует DeviceLinkValidationError и ValueError."""
        assert issubclass(LinkStorageValidationError, LinkStorageError)
        assert issubclass(LinkStorageValidationError, DeviceLinkValidationError)
        assert issubclass(LinkStorageValidationError, DeviceLinkError)
        assert issubclass(LinkStorageValidationError, ValueError)


class TestNoSensitiveUrlLeaks:
    """Проверка того, что исходный URL не попадает в имя файла, его содержимое или ошибки."""

    def test_error_message_does_not_contain_raw_url_or_secrets(self) -> None:
        """Сообщения об ошибках валидации не содержат URL, хосты или пароли."""
        sensitive_url = "https://secret_user:super_secret_pass@internal.classified.corp:99999/private/api/devices?auth=token123"

        with pytest.raises(LinkStorageValidationError) as exc_info:
            open_link_service(sensitive_url)

        err_msg = str(exc_info.value)
        assert "super_secret_pass" not in err_msg
        assert "secret_user" not in err_msg
        assert "internal.classified.corp" not in err_msg
        assert "token123" not in err_msg
        assert sensitive_url not in err_msg

    def test_filename_and_content_do_not_contain_url(self, tmp_path: Path) -> None:
        """Имя файла и его содержимое не содержат исходного URL или хоста."""
        sensitive_url = "https://sensitive-server.internal.corp:8443/secret-dep/api/devices"
        service = open_link_service(sensitive_url, storage_dir=tmp_path)

        # Выполняем явную запись
        service.registry.link("serial-usb-1234", "remote-hub-5678")

        reg_path = service.registry.path
        filename = reg_path.name

        # Имя файла содержит только безопасный хэш
        assert "sensitive-server" not in filename
        assert "internal.corp" not in filename
        assert "secret-dep" not in filename
        assert "https" not in filename
        assert ":" not in filename
        assert re.match(r"^links_[0-9a-f]{64}\.json$", filename)

        # Содержимое файла не содержит URL
        content = reg_path.read_text(encoding="utf-8")
        assert "sensitive-server" not in content
        assert "internal.corp" not in content
        assert "secret-dep" not in content
        assert "https" not in content
        assert "8443" not in content

        # Содержит только явные сериалы и ID связей
        data = json.loads(content)
        assert data["version"] == 1
        assert data["links"] == [{"hub_id": "remote-hub-5678", "serial": "serial-usb-1234"}]


class TestEquivalentVariants:
    """Тестирование нормализации эквивалентных вариантов HTTPS URL."""

    def test_host_case_insensitivity(self, tmp_path: Path) -> None:
        """Регистр хоста нормализуется (hub.com == HUB.COM == Hub.Com)."""
        url1 = "https://hub.example.com/api/devices"
        url2 = "https://HUB.EXAMPLE.COM/api/devices"
        url3 = "https://Hub.Example.Com/api/devices"

        assert get_storage_key(url1) == get_storage_key(url2) == get_storage_key(url3)
        assert get_storage_path(url1, storage_dir=tmp_path) == get_storage_path(url2, storage_dir=tmp_path)

    def test_scheme_case_insensitivity(self, tmp_path: Path) -> None:
        """Регистр схемы нормализуется (https == HTTPS == HtTpS)."""
        url1 = "https://hub.example.com/api/devices"
        url2 = "HTTPS://hub.example.com/api/devices"
        url3 = "Https://hub.example.com/api/devices"

        assert get_storage_key(url1) == get_storage_key(url2) == get_storage_key(url3)

    def test_default_port_443_equivalence(self, tmp_path: Path) -> None:
        """Явный порт 443 и опущенный порт 443 эквивалентны."""
        url_without_port = "https://hub.example.com/api/devices"
        url_with_port = "https://hub.example.com:443/api/devices"

        assert get_storage_key(url_without_port) == get_storage_key(url_with_port)
        assert get_storage_path(url_without_port, storage_dir=tmp_path) == get_storage_path(
            url_with_port, storage_dir=tmp_path
        )

    def test_path_slashes_normalization(self, tmp_path: Path) -> None:
        """Концевые слэши и повторяющиеся слэши в пути нормализуются."""
        url1 = "https://hub.example.com/api/devices"
        url2 = "https://hub.example.com/api/devices/"
        url3 = "https://hub.example.com//api//devices"
        url4 = "https://hub.example.com/custom/../api/devices"

        assert get_storage_key(url1) == get_storage_key(url2) == get_storage_key(url3) == get_storage_key(url4)

    def test_leading_trailing_whitespace(self, tmp_path: Path) -> None:
        """Пробелы в начале и конце URL нормализуются."""
        url1 = "https://hub.example.com/api/devices"
        url2 = "   https://hub.example.com/api/devices   "

        assert get_storage_key(url1) == get_storage_key(url2)

    def test_ipv6_normalization(self, tmp_path: Path) -> None:
        """IPv6 адреса корректно нормализуются и приводятся к общему виду."""
        url1 = "https://[::1]/api/devices"
        url2 = "https://[::1]:443/api/devices"
        url3 = "https://[0:0:0:0:0:0:0:1]:443/api/devices"

        assert get_storage_key(url1) == get_storage_key(url2)

    def test_cross_variant_access(self, tmp_path: Path) -> None:
        """Связь, сохраненная через один вариант, доступна через любой эквивалентный вариант."""
        service1 = open_link_service("https://hub.example.com/api/devices", storage_dir=tmp_path)
        service1.registry.link("serial-A", "hub-1")

        # Открываем через верхний регистр хоста и явный порт :443
        service2 = open_link_service("HTTPS://HUB.EXAMPLE.COM:443/api/devices/", storage_dir=tmp_path)
        assert len(service2.registry) == 1
        assert service2.registry.get_hub_id("serial-A") == "hub-1"


class TestUrlIsolation:
    """Тестирование строгой изоляции хранилищ для разных серверов, портов и путей API."""

    def test_different_hosts_isolated(self, tmp_path: Path) -> None:
        """Разные хосты изолированы в разные файлы."""
        url_a = "https://server-a.internal/api/devices"
        url_b = "https://server-b.internal/api/devices"

        path_a = get_storage_path(url_a, storage_dir=tmp_path)
        path_b = get_storage_path(url_b, storage_dir=tmp_path)
        assert path_a != path_b

        srv_a = open_link_service(url_a, storage_dir=tmp_path)
        srv_b = open_link_service(url_b, storage_dir=tmp_path)

        srv_a.registry.link("s-shared", "hub-a")
        srv_b.registry.link("s-shared", "hub-b")

        assert srv_a.registry.get_hub_id("s-shared") == "hub-a"
        assert srv_b.registry.get_hub_id("s-shared") == "hub-b"

    def test_different_ports_isolated(self, tmp_path: Path) -> None:
        """Разные порты одного хоста изолированы друг от друга."""
        url_443 = "https://server.internal:443/api/devices"
        url_8443 = "https://server.internal:8443/api/devices"
        url_9443 = "https://server.internal:9443/api/devices"

        path_443 = get_storage_path(url_443, storage_dir=tmp_path)
        path_8443 = get_storage_path(url_8443, storage_dir=tmp_path)
        path_9443 = get_storage_path(url_9443, storage_dir=tmp_path)

        assert path_443 != path_8443
        assert path_8443 != path_9443
        assert path_443 != path_9443

        srv_443 = open_link_service(url_443, storage_dir=tmp_path)
        srv_8443 = open_link_service(url_8443, storage_dir=tmp_path)

        srv_443.registry.link("serial-port", "hub-443")
        assert srv_8443.registry.get_hub_id("serial-port") is None

    def test_different_api_paths_isolated(self, tmp_path: Path) -> None:
        """Разные пути API (deployment isolation) строго изолированы."""
        url_dep1 = "https://hub.example.com/deployment-prod/api/devices"
        url_dep2 = "https://hub.example.com/deployment-stage/api/devices"
        url_root = "https://hub.example.com/api/devices"

        path_dep1 = get_storage_path(url_dep1, storage_dir=tmp_path)
        path_dep2 = get_storage_path(url_dep2, storage_dir=tmp_path)
        path_root = get_storage_path(url_root, storage_dir=tmp_path)

        assert path_dep1 != path_dep2
        assert path_dep1 != path_root
        assert path_dep2 != path_root

        srv_dep1 = open_link_service(url_dep1, storage_dir=tmp_path)
        srv_dep2 = open_link_service(url_dep2, storage_dir=tmp_path)

        srv_dep1.registry.link("phone-1", "hub-prod-1")
        assert srv_dep2.registry.get_hub_id("phone-1") is None


class TestLazyCreationAndNoSideEffects:
    """Тестирование ленивой инициализации: открытие не создает каталог/файл до явной записи."""

    def test_open_does_not_create_nonexistent_directory_or_file(self, tmp_path: Path) -> None:
        """open_link_service в несуществующий каталог не создает ни каталог, ни файл."""
        non_existent_dir = tmp_path / "deep" / "nested" / "droidhub_storage"
        assert not non_existent_dir.exists()

        service = open_link_service(
            "https://hub.example.com/api/devices",
            storage_dir=non_existent_dir,
        )

        assert isinstance(service, DeviceLinkService)
        assert isinstance(service.registry, LinkRegistry)

        # Каталог и файл по-прежнему не существуют
        assert not non_existent_dir.exists()
        assert not service.registry.path.exists()

    def test_readonly_queries_do_not_create_file_or_directory(self, tmp_path: Path) -> None:
        """Чтение пустого реестра через сервис не создает файл или каталог."""
        non_existent_dir = tmp_path / "lazy_test"
        service = open_link_service(
            "https://hub.example.com/api/devices",
            storage_dir=non_existent_dir,
        )

        # Выполняем read-only операции
        assert service.registry.list() == []
        assert len(service.registry) == 0
        assert service.registry.get_by_serial("anything") is None
        assert service.get_linked_pairs([], []) == []

        # Ни каталог, ни файл не были созданы
        assert not non_existent_dir.exists()
        assert not service.registry.path.exists()

    def test_explicit_write_creates_directory_and_file(self, tmp_path: Path) -> None:
        """Только явная запись создает каталог и файл на диске."""
        target_dir = tmp_path / "created_on_write"
        service = open_link_service(
            "https://hub.example.com/api/devices",
            storage_dir=target_dir,
        )

        assert not target_dir.exists()

        # Явная запись связи
        service.registry.link("FA6AA0301234", "remote-hub-dev-1")

        # Теперь каталог и файл существуют
        assert target_dir.exists()
        assert service.registry.path.exists()
        assert service.registry.path.is_file()


class TestPersistenceAcrossReopen:
    """Тестирование сохранения и восстановления данных при повторном открытии сервиса."""

    def test_reopen_persists_links(self, tmp_path: Path) -> None:
        """Связи сохраняются на диске и восстанавливаются при повторном open_link_service."""
        url = "https://hub.example.com/api/devices"

        srv1 = open_link_service(url, storage_dir=tmp_path)
        srv1.registry.link("s-001", "hub-id-1")
        srv1.registry.link("s-002", "hub-id-2")
        srv1.registry.link("s-003", "hub-id-3")

        # Открываем новый экземпляр сервиса
        srv2 = open_link_service(url, storage_dir=tmp_path)
        links = srv2.registry.list()

        assert len(links) == 3
        assert srv2.registry.get_hub_id("s-001") == "hub-id-1"
        assert srv2.registry.get_hub_id("s-002") == "hub-id-2"
        assert srv2.registry.get_hub_id("s-003") == "hub-id-3"

        # Удаляем одну связь в srv2
        assert srv2.unlink_by_serial("s-002") is True

        # Открываем третий экземпляр сервиса и проверяем
        srv3 = open_link_service(url, storage_dir=tmp_path)
        assert len(srv3.registry) == 2
        assert srv3.registry.get_hub_id("s-001") == "hub-id-1"
        assert srv3.registry.get_hub_id("s-002") is None
        assert srv3.registry.get_hub_id("s-003") == "hub-id-3"

    def test_service_link_integration(self, tmp_path: Path) -> None:
        """Интеграция с DeviceLinkService.link() и проверкой списка устройств."""
        url = "https://hub.example.com/api/devices"
        service = open_link_service(url, storage_dir=tmp_path)

        local_dev = DeviceInfo(
            serial="FA6AA0301234",
            state="device",
            state_display="Готов к работе",
            connection_type="USB",
            usb_info="usb:1-1.2",
            is_ready=True,
            is_network=False,
        )
        hub_dev = HubDevice(id="hub_phone_99", name="Pixel 7", online=True)

        service.set_devices([local_dev], [hub_dev])
        link = service.link("FA6AA0301234", "hub_phone_99")

        assert isinstance(link, DeviceLink)
        assert link.serial == "FA6AA0301234"
        assert link.hub_id == "hub_phone_99"

        # Проверяем статус пары
        pair_status = service.get_pair_status("FA6AA0301234")
        assert pair_status is not None
        assert pair_status.is_operational is True

        # Переоткрываем сервис и проверяем наличие пары
        reopened = open_link_service(url, storage_dir=tmp_path)
        reopened.set_devices([local_dev], [hub_dev])
        reopened_pairs = reopened.get_linked_pairs()
        assert len(reopened_pairs) == 1
        assert reopened_pairs[0].serial == "FA6AA0301234"
        assert reopened_pairs[0].hub_id == "hub_phone_99"


class TestDefaultStorageDirPerOS:
    """Тестирование определения каталога настроек DroidHub по умолчанию для различных ОС."""

    def test_darwin_directory(self) -> None:
        """На macOS используется ~/Library/Application Support/DroidHub."""
        with patch("sys.platform", "darwin"):
            dir_path = get_default_storage_dir()
            assert dir_path == Path.home() / "Library" / "Application Support" / "DroidHub"

    def test_win32_localappdata_present(self) -> None:
        """На Windows при наличии LOCALAPPDATA используется %LOCALAPPDATA%/DroidHub."""
        local_app = "C:/Users/Test/AppData/Local"
        with patch("sys.platform", "win32"), patch.dict(os.environ, {"LOCALAPPDATA": local_app}):
            dir_path = get_default_storage_dir()
            assert dir_path == Path(local_app) / "DroidHub"
            assert dir_path.name == "DroidHub"

    def test_win32_localappdata_fallback_to_appdata(self) -> None:
        """На Windows при отсутствии LOCALAPPDATA используется APPDATA."""
        roaming_app = "C:/Users/Test/AppData/Roaming"
        env = {"LOCALAPPDATA": "", "APPDATA": roaming_app}
        with patch("sys.platform", "win32"), patch.dict(os.environ, env):
            dir_path = get_default_storage_dir()
            assert dir_path == Path(roaming_app) / "DroidHub"
            assert dir_path.name == "DroidHub"

    def test_win32_fallback_to_userprofile_local(self) -> None:
        """На Windows при отсутствии переменных окружения используется ~/AppData/Local/DroidHub."""
        env = {"LOCALAPPDATA": "", "APPDATA": ""}
        with patch("sys.platform", "win32"), patch.dict(os.environ, env):
            dir_path = get_default_storage_dir()
            assert dir_path == Path.home() / "AppData" / "Local" / "DroidHub"

    def test_linux_xdg_config_home_present(self) -> None:
        """На Linux при наличии XDG_CONFIG_HOME используется $XDG_CONFIG_HOME/DroidHub."""
        with patch("sys.platform", "linux"), patch.dict(os.environ, {"XDG_CONFIG_HOME": "/custom/config"}):
            dir_path = get_default_storage_dir()
            assert dir_path == Path("/custom/config/DroidHub")

    def test_linux_default_to_dot_config(self) -> None:
        """На Linux при отсутствии XDG_CONFIG_HOME используется ~/.config/DroidHub."""
        env = {"XDG_CONFIG_HOME": ""}
        with patch("sys.platform", "linux"), patch.dict(os.environ, env):
            dir_path = get_default_storage_dir()
            assert dir_path == Path.home() / ".config" / "DroidHub"

    def test_default_storage_dir_used_when_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """При storage_dir=None используется каталог по умолчанию."""
        mock_dir = tmp_path / "mock_default"
        monkeypatch.setattr("desktop.link_storage.get_default_storage_dir", lambda: mock_dir)

        path = get_storage_path("https://hub.example.com/api/devices")
        assert path.parent == mock_dir


class TestNoSubprocessNoNetwork:
    """Гарантия отсутствия сетевых вызовов, ADB-команд и subprocess."""

    def test_no_network_or_subprocess_calls(self, tmp_path: Path) -> None:
        """Модуль не выполняет сетевых запросов и не вызывает внешние команды."""
        with patch("urllib.request.urlopen") as mock_urlopen, \
             patch("socket.socket") as mock_socket, \
             patch("subprocess.Popen") as mock_popen, \
             patch("subprocess.run") as mock_run:

            service = open_link_service(
                "https://hub.example.com:443/api/devices",
                storage_dir=tmp_path,
            )
            service.registry.link("serial-1", "hub-1")
            service.registry.list()
            service.registry.unlink("serial-1")

            assert mock_urlopen.call_count == 0
            assert mock_socket.call_count == 0
            assert mock_popen.call_count == 0
            assert mock_run.call_count == 0
