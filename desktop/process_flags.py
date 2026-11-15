"""Options for launching command-line Android tools from the desktop GUI."""

import subprocess
import sys


# A console-subsystem tool would otherwise open a console on each GUI refresh.
NO_CONSOLE = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
