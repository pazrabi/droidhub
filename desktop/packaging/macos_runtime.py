"""Configure paths at startup for the packaged macOS application."""

import os
import sys
from pathlib import Path

import certifi


tools_dir = Path(sys._MEIPASS) / "tools"
os.environ.setdefault("DROIDHUB_ADB_PATH", str(tools_dir / "adb"))
os.environ.setdefault("DROIDHUB_SCRCPY_PATH", str(tools_dir / "scrcpy"))
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
