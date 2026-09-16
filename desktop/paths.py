"""Where the offline build keeps things.

Five gigabytes of model does not belong next to the executable. On Windows that
is often Program Files, which a normal user cannot write to; on macOS it is
inside an app bundle, where it breaks the signature; everywhere it means
reinstalling the app throws the download away.

So the weights live in the user's own data directory, and the app directory
holds only the app. The one exception is a portable build: a `portable.txt` file
beside the executable moves everything next to it, which is what a USB stick or
a machine with no writable home directory needs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP = "AhoosAI Studio"


def app_dir() -> Path:
    """Where this program lives -- the bundle when frozen, the package otherwise."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def portable() -> bool:
    return (app_dir() / "portable.txt").is_file()


def data_dir() -> Path:
    if portable():
        return app_dir() / "data"
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return root / APP


def models_dir() -> Path:
    path = data_dir() / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def bin_dir() -> Path:
    """The runtime lives with the app when frozen and with the data otherwise.

    A packaged build ships llama-server inside it and must not go looking in a
    home directory for something it already has. A source checkout has nothing
    inside it, and writing into the repository would put a 20 MB binary where
    git can see it.
    """
    if getattr(sys, "frozen", False):
        # Inside the bundle, beside the other data files. PyInstaller 6 puts
        # those in _internal/ (or the .app's Resources), not next to the
        # executable, so app_dir() would look one level too high.
        return Path(getattr(sys, "_MEIPASS", app_dir())) / "bin"
    return data_dir() / "bin"


def settings_file() -> Path:
    return data_dir() / "settings.json"


def sessions_dir() -> Path:
    path = data_dir() / "conversations"
    path.mkdir(parents=True, exist_ok=True)
    return path


def assets_dir() -> Path:
    """Files that ship with the app: the adapter, the icon, the interface."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "assets"
