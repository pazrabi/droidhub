"""Tests for macOS desktop release artifact verification script (scripts/verify_desktop_macos.py).

All tests run on Linux using temporary test bundles and simulated system tools (lipo, codesign).
No claims are made that a real signed macOS bundle is verified natively on Linux.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.verify_desktop_macos import (
    EXPECTED_BUNDLE_ID,
    EXPECTED_EXECUTABLE,
    REQUIRED_TOOLS,
    VerificationError,
    check_bundle_symlinks,
    main,
    verify_architecture,
    verify_bundle,
    verify_executable,
    verify_info_plist,
    verify_signature,
    verify_tools,
)


def create_test_bundle(
    bundle_dir: Path,
    *,
    bundle_id: str | None = EXPECTED_BUNDLE_ID,
    bundle_executable: str | None = EXPECTED_EXECUTABLE,
    create_executable: bool = True,
    executable_mode: int = 0o755,
    create_tools: bool = True,
    corrupt_plist: bool = False,
    missing_tools: list[str] | None = None,
) -> Path:
    """Helper to construct a temporary macOS application bundle."""
    contents = bundle_dir / "Contents"
    macos = contents / "MacOS"
    tools = contents / "Frameworks" / "tools"
    macos.mkdir(parents=True, exist_ok=True)
    tools.mkdir(parents=True, exist_ok=True)

    plist_path = contents / "Info.plist"
    if corrupt_plist:
        plist_path.write_bytes(b"INVALID_PLIST_DATA_NOT_XML_OR_BINARY")
    else:
        plist_dict: dict[str, str] = {}
        if bundle_id is not None:
            plist_dict["CFBundleIdentifier"] = bundle_id
        if bundle_executable is not None:
            plist_dict["CFBundleExecutable"] = bundle_executable
        plist_dict["CFBundleName"] = "DroidHub"
        plist_dict["CFBundlePackageType"] = "APPL"
        with open(plist_path, "wb") as fp:
            plistlib.dump(plist_dict, fp)

    if create_executable:
        exec_name = bundle_executable or EXPECTED_EXECUTABLE
        exec_file = macos / exec_name
        exec_file.write_bytes(b"\xcf\xfa\xed\xfe\x07\x00\x00\x01")  # Mach-O dummy header
        exec_file.chmod(executable_mode)

    if create_tools:
        missing = set(missing_tools or [])
        for tool in REQUIRED_TOOLS:
            if tool not in missing:
                tool_path = tools / tool
                tool_path.write_text(f"dummy content for {tool}")

    return bundle_dir


def create_mock_script(path: Path, content: str) -> str:
    """Create an executable script to simulate system utilities like lipo or codesign."""
    path.write_text(content)
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def mock_tools(tmp_path: Path) -> tuple[str, str]:
    """Create simulated lipo and codesign executables."""
    lipo_path = create_mock_script(
        tmp_path / "mock_lipo",
        "#!/bin/sh\necho 'arm64'\n",
    )
    codesign_path = create_mock_script(
        tmp_path / "mock_codesign",
        "#!/bin/sh\nexit 0\n",
    )
    return lipo_path, codesign_path


class TestVerifyDesktopMacOS:
    """Test suite for scripts/verify_desktop_macos.py."""

    def test_valid_thin_arm64_bundle(self, tmp_path: Path, mock_tools: tuple[str, str]) -> None:
        """Valid thin arm64 bundle passes verification."""
        lipo_bin, codesign_bin = mock_tools
        bundle_dir = create_test_bundle(tmp_path / "DroidHub.app")

        result = verify_bundle(
            bundle_dir,
            lipo_bin=lipo_bin,
            codesign_bin=codesign_bin,
        )
        assert result["bundle_id"] == EXPECTED_BUNDLE_ID
        assert result["archs"] == ["arm64"]

    def test_valid_universal_arm64_bundle(self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
        """Valid universal bundle containing arm64 passes verification."""
        bundle_dir = create_test_bundle(tmp_path / "DroidHub.app")
        lipo_bin = create_mock_script(
            tmp_path / "mock_lipo_universal",
            "#!/bin/sh\necho 'x86_64 arm64'\n",
        )
        codesign_bin = create_mock_script(
            tmp_path / "mock_codesign_ok",
            "#!/bin/sh\nexit 0\n",
        )

        result = verify_bundle(
            bundle_dir,
            lipo_bin=lipo_bin,
            codesign_bin=codesign_bin,
        )
        assert result["archs"] == ["x86_64", "arm64"]

    def test_valid_bundle_with_spaces_in_path(self, tmp_path: Path, mock_tools: tuple[str, str]) -> None:
        """Path with spaces in directory and bundle names is handled correctly."""
        lipo_bin, codesign_bin = mock_tools
        spaced_dir = tmp_path / "Release Artifacts Dir" / "DroidHub Desktop.app"
        create_test_bundle(spaced_dir)

        result = verify_bundle(
            spaced_dir,
            lipo_bin=lipo_bin,
            codesign_bin=codesign_bin,
        )
        assert "Release Artifacts Dir" in result["bundle"]
        assert result["bundle_id"] == EXPECTED_BUNDLE_ID

    def test_valid_bundle_with_internal_symlinks(self, tmp_path: Path, mock_tools: tuple[str, str]) -> None:
        """Internal symlinks used by PyInstaller for data/frameworks are supported."""
        lipo_bin, codesign_bin = mock_tools
        bundle_dir = create_test_bundle(tmp_path / "DroidHub.app")

        # Move scrcpy-server and LICENSE to Resources and create internal symlinks
        resources = bundle_dir / "Contents" / "Resources"
        resources.mkdir(parents=True, exist_ok=True)
        (resources / "scrcpy-server").write_text("server binary")
        (resources / "LICENSE").write_text("MIT License")

        tools_dir = bundle_dir / "Contents" / "Frameworks" / "tools"
        (tools_dir / "scrcpy-server").unlink()
        (tools_dir / "LICENSE").unlink()

        (tools_dir / "scrcpy-server").symlink_to("../../Resources/scrcpy-server")
        (tools_dir / "LICENSE").symlink_to("../../Resources/LICENSE")

        result = verify_bundle(
            bundle_dir,
            lipo_bin=lipo_bin,
            codesign_bin=codesign_bin,
        )
        assert result["bundle_id"] == EXPECTED_BUNDLE_ID

    def test_nonexistent_bundle_path(self, tmp_path: Path) -> None:
        """Nonexistent bundle path raises VerificationError."""
        bad_path = tmp_path / "DoesNotExist.app"
        with pytest.raises(VerificationError, match="не существует"):
            verify_bundle(bad_path)

    def test_bundle_is_file_not_dir(self, tmp_path: Path) -> None:
        """Bundle path pointing to a file raises VerificationError."""
        file_bundle = tmp_path / "DroidHub.app"
        file_bundle.write_text("not a directory")
        with pytest.raises(VerificationError, match="не является директорией"):
            verify_bundle(file_bundle)

    def test_symlink_escape_from_bundle(self, tmp_path: Path, mock_tools: tuple[str, str]) -> None:
        """Symlink pointing outside the bundle must be rejected as an escape."""
        lipo_bin, codesign_bin = mock_tools
        bundle_dir = create_test_bundle(tmp_path / "DroidHub.app")

        outside_file = tmp_path / "outside_secret.txt"
        outside_file.write_text("outside data")

        # Symlink in Frameworks pointing outside bundle
        escape_link = bundle_dir / "Contents" / "Frameworks" / "escape_link"
        escape_link.symlink_to(outside_file)

        with pytest.raises(VerificationError, match="выходит за пределы bundle"):
            verify_bundle(
                bundle_dir,
                lipo_bin=lipo_bin,
                codesign_bin=codesign_bin,
            )

    def test_broken_symlink_in_bundle(self, tmp_path: Path, mock_tools: tuple[str, str]) -> None:
        """Broken symlink must raise VerificationError."""
        lipo_bin, codesign_bin = mock_tools
        bundle_dir = create_test_bundle(tmp_path / "DroidHub.app")

        broken_link = bundle_dir / "Contents" / "Frameworks" / "broken_link"
        broken_link.symlink_to(bundle_dir / "Contents" / "nonexistent_target")

        with pytest.raises(VerificationError, match="Битая символическая ссылка"):
            verify_bundle(
                bundle_dir,
                lipo_bin=lipo_bin,
                codesign_bin=codesign_bin,
            )

    def test_circular_symlink_in_bundle(self, tmp_path: Path) -> None:
        """Circular symlinks must raise VerificationError."""
        bundle_dir = create_test_bundle(tmp_path / "DroidHub.app")
        link_a = bundle_dir / "Contents" / "Frameworks" / "link_a"
        link_b = bundle_dir / "Contents" / "Frameworks" / "link_b"
        link_a.symlink_to(link_b)
        link_b.symlink_to(link_a)

        with pytest.raises(VerificationError, match="Циклическая символическая ссылка"):
            check_bundle_symlinks(bundle_dir)

    def test_missing_info_plist(self, tmp_path: Path) -> None:
        """Missing Info.plist raises VerificationError."""
        bundle_dir = create_test_bundle(tmp_path / "DroidHub.app")
        (bundle_dir / "Contents" / "Info.plist").unlink()

        with pytest.raises(VerificationError, match="Отсутствует файл Info.plist"):
            verify_info_plist(bundle_dir)

    def test_corrupted_info_plist(self, tmp_path: Path) -> None:
        """Corrupted/invalid Info.plist raises VerificationError."""
        bundle_dir = create_test_bundle(tmp_path / "DroidHub.app", corrupt_plist=True)

        with pytest.raises(VerificationError, match="Ошибка чтения или повреждён Info.plist"):
            verify_info_plist(bundle_dir)

    def test_invalid_bundle_identifier(self, tmp_path: Path) -> None:
        """Incorrect CFBundleIdentifier raises VerificationError."""
        bundle_dir = create_test_bundle(tmp_path / "DroidHub.app", bundle_id="com.other.app")

        with pytest.raises(VerificationError, match="Неверный или отсутствующий CFBundleIdentifier"):
            verify_info_plist(bundle_dir)

    def test_missing_bundle_identifier(self, tmp_path: Path) -> None:
        """Missing CFBundleIdentifier raises VerificationError."""
        bundle_dir = create_test_bundle(tmp_path / "DroidHub.app", bundle_id=None)

        with pytest.raises(VerificationError, match="Неверный или отсутствующий CFBundleIdentifier"):
            verify_info_plist(bundle_dir)

    def test_invalid_bundle_executable(self, tmp_path: Path) -> None:
        """Incorrect CFBundleExecutable raises VerificationError."""
        bundle_dir = create_test_bundle(tmp_path / "DroidHub.app", bundle_executable="WrongName")

        with pytest.raises(VerificationError, match="Неверный или отсутствующий CFBundleExecutable"):
            verify_info_plist(bundle_dir)

    def test_missing_bundle_executable(self, tmp_path: Path) -> None:
        """Missing CFBundleExecutable raises VerificationError."""
        bundle_dir = create_test_bundle(tmp_path / "DroidHub.app", bundle_executable=None)

        with pytest.raises(VerificationError, match="Неверный или отсутствующий CFBundleExecutable"):
            verify_info_plist(bundle_dir)

    def test_missing_executable_binary(self, tmp_path: Path) -> None:
        """Missing Contents/MacOS/DroidHub executable raises VerificationError."""
        bundle_dir = create_test_bundle(tmp_path / "DroidHub.app", create_executable=False)

        with pytest.raises(VerificationError, match="Исполняемый файл не найден"):
            verify_executable(bundle_dir, EXPECTED_EXECUTABLE)

    def test_non_executable_binary(self, tmp_path: Path) -> None:
        """Binary without +x permissions raises VerificationError."""
        bundle_dir = create_test_bundle(tmp_path / "DroidHub.app", executable_mode=0o644)

        with pytest.raises(VerificationError, match="не имеет прав на исполнение"):
            verify_executable(bundle_dir, EXPECTED_EXECUTABLE)

    def test_missing_tools_directory(self, tmp_path: Path) -> None:
        """Missing Contents/Frameworks/tools directory raises VerificationError."""
        bundle_dir = create_test_bundle(tmp_path / "DroidHub.app")
        import shutil

        shutil.rmtree(bundle_dir / "Contents" / "Frameworks" / "tools")

        with pytest.raises(VerificationError, match="Директория комплектных инструментов не найдена"):
            verify_tools(bundle_dir)

    @pytest.mark.parametrize("tool_name", REQUIRED_TOOLS)
    def test_missing_each_required_tool(self, tmp_path: Path, tool_name: str) -> None:
        """Absence of any tool from adb, scrcpy, scrcpy-server, LICENSE raises VerificationError."""
        bundle_dir = create_test_bundle(tmp_path / f"DroidHub_{tool_name}.app", missing_tools=[tool_name])

        with pytest.raises(VerificationError, match="Отсутствует комплектный инструмент"):
            verify_tools(bundle_dir)

    def test_tool_is_directory_not_file(self, tmp_path: Path) -> None:
        """Tool entry that is a directory instead of a regular file raises VerificationError."""
        bundle_dir = create_test_bundle(tmp_path / "DroidHub.app")
        adb_path = bundle_dir / "Contents" / "Frameworks" / "tools" / "adb"
        adb_path.unlink()
        adb_path.mkdir()

        with pytest.raises(VerificationError, match="не является файлом"):
            verify_tools(bundle_dir)

    def test_lipo_wrong_architecture(self, tmp_path: Path) -> None:
        """Executable architecture without arm64 (e.g. x86_64 only) raises VerificationError."""
        exec_path = tmp_path / "DroidHub"
        exec_path.write_text("binary")
        mock_lipo = create_mock_script(
            tmp_path / "mock_lipo_x86",
            "#!/bin/sh\necho 'x86_64'\n",
        )

        with pytest.raises(VerificationError, match="Неверная архитектура"):
            verify_architecture(exec_path, lipo_bin=mock_lipo)

    def test_lipo_nonzero_exit_code(self, tmp_path: Path) -> None:
        """Lipo failing with non-zero exit code raises VerificationError."""
        exec_path = tmp_path / "DroidHub"
        exec_path.write_text("binary")
        mock_lipo = create_mock_script(
            tmp_path / "mock_lipo_err",
            "#!/bin/sh\necho 'can not figure out architecture' >&2\nexit 1\n",
        )

        with pytest.raises(VerificationError, match="Ошибка lipo"):
            verify_architecture(exec_path, lipo_bin=mock_lipo)

    def test_lipo_empty_output(self, tmp_path: Path) -> None:
        """Lipo returning empty output raises VerificationError."""
        exec_path = tmp_path / "DroidHub"
        exec_path.write_text("binary")
        mock_lipo = create_mock_script(
            tmp_path / "mock_lipo_empty",
            "#!/bin/sh\nexit 0\n",
        )

        with pytest.raises(VerificationError, match="не вернула архитектур"):
            verify_architecture(exec_path, lipo_bin=mock_lipo)

    def test_lipo_timeout(self, tmp_path: Path) -> None:
        """Timeout during lipo execution raises VerificationError."""
        exec_path = tmp_path / "DroidHub"
        exec_path.write_text("binary")

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["lipo"], timeout=0.1)):
            with pytest.raises(VerificationError, match="Превышен таймаут"):
                verify_architecture(exec_path, timeout=0.1)

    def test_codesign_invalid_signature(self, tmp_path: Path) -> None:
        """Codesign failing verification raises VerificationError."""
        bundle_dir = create_test_bundle(tmp_path / "DroidHub.app")
        mock_codesign = create_mock_script(
            tmp_path / "mock_codesign_invalid",
            "#!/bin/sh\necho 'code has been modified or corrupted' >&2\nexit 1\n",
        )

        with pytest.raises(VerificationError, match="Ошибка проверки подписи codesign"):
            verify_signature(bundle_dir, codesign_bin=mock_codesign)

    def test_codesign_timeout(self, tmp_path: Path) -> None:
        """Timeout during codesign execution raises VerificationError."""
        bundle_dir = create_test_bundle(tmp_path / "DroidHub.app")

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["codesign"], timeout=0.1)):
            with pytest.raises(VerificationError, match="Превышен таймаут"):
                verify_signature(bundle_dir, timeout=0.1)

    def test_missing_system_tool_fails_safely(self, tmp_path: Path) -> None:
        """When system tool is missing (e.g. running on Linux without mock tools), fails safely."""
        bundle_dir = create_test_bundle(tmp_path / "DroidHub.app")
        nonexistent_tool = "/nonexistent/path/to/lipo"

        with pytest.raises(VerificationError, match="Не удалось запустить утилиту lipo"):
            verify_architecture(
                bundle_dir / "Contents" / "MacOS" / EXPECTED_EXECUTABLE,
                lipo_bin=nonexistent_tool,
            )

    def test_cli_main_success(self, tmp_path: Path, mock_tools: tuple[str, str], capsys: pytest.CaptureFixture[str]) -> None:
        """CLI main function exits 0 on valid bundle."""
        lipo_bin, codesign_bin = mock_tools
        bundle_dir = create_test_bundle(tmp_path / "DroidHub.app")

        exit_code = main([
            str(bundle_dir),
            f"--lipo-bin={lipo_bin}",
            f"--codesign-bin={codesign_bin}",
        ])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "успешно пройдена" in captured.out

    def test_cli_main_failure(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """CLI main function exits 1 on invalid bundle."""
        bad_bundle = tmp_path / "Nonexistent.app"

        exit_code = main([str(bad_bundle)])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "Ошибка верификации" in captured.err

    def test_cli_subprocess_invocation(self, tmp_path: Path, mock_tools: tuple[str, str]) -> None:
        """Subprocess invocation of verify_desktop_macos.py script matches CLI contract."""
        lipo_bin, codesign_bin = mock_tools
        bundle_dir = create_test_bundle(tmp_path / "Spaces In Name DroidHub.app")
        script_path = Path(__file__).resolve().parent.parent / "scripts" / "verify_desktop_macos.py"

        # Success case with arguments and spaces in path
        proc = subprocess.run(
            [
                sys.executable,
                str(script_path),
                str(bundle_dir),
                f"--lipo-bin={lipo_bin}",
                f"--codesign-bin={codesign_bin}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0
        assert "успешно пройдена" in proc.stdout

        # Failure case with missing bundle argument
        proc_no_args = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc_no_args.returncode == 2
        assert "error: the following arguments are required: bundle" in proc_no_args.stderr
