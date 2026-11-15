"""Configure paths at startup for the packaged Windows application."""

import os
import sys
from pathlib import Path

import certifi


tools_dir = Path(sys._MEIPASS) / "tools"
os.environ.setdefault("DROIDHUB_ADB_PATH", str(tools_dir / "adb.exe"))
os.environ.setdefault("DROIDHUB_SCRCPY_PATH", str(tools_dir / "scrcpy.exe"))
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
