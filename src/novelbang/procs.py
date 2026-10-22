"""Running other programs (git, Ollama) from NovelBang."""

import subprocess
import sys

# A windowed Windows app flashes up a console window for every program it runs without this.
NO_CONSOLE = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
