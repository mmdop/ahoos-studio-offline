# -*- mode: python -*-
# PyInstaller spec for the offline studio. Run through desktop/packaging/build.py,
# which stages the runtime, the registry and the interface and passes their
# locations in the environment -- this file only describes the bundle.
#
# One folder, not one file. A one-file build unpacks itself to a temporary
# directory on every start, which for a bundle carrying an 80 MB adapter and a
# llama.cpp runtime is several seconds of nothing before a window appears, and
# on Windows is the pattern antivirus products are most suspicious of.

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parents[1]
STAGE = Path(os.environ["AHOOS_STAGE"])
BIN = Path(os.environ["AHOOS_BIN"])
FAMILY = Path(os.environ["AHOOS_FAMILY"])
NAME = "AhoosAI Studio"
IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"

datas = [
    (str(ROOT / "desktop" / "ui"), "ui"),
    (str(ROOT / "desktop" / "assets" / "nimbus-1-1-prime-ee.gguf"), "assets"),
    (str(ROOT / "desktop" / "assets" / "icon.png"), "assets"),
    (str(ROOT / "desktop" / "assets" / "icon.ico"), "assets"),
    (str(FAMILY), "family"),
]
datas += collect_data_files("webview")

# llama-server and its shared libraries go in as binaries, not data, so that on
# macOS they are signed with the bundle and on Linux their permissions survive.
binaries = [(str(path), "bin") for path in BIN.iterdir() if path.is_file()]

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("studio")
    + collect_submodules("desktop")
    + collect_submodules("webview")
)

a = Analysis(
    [str(ROOT / "desktop" / "packaging" / "launch.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # Nothing the app imports, and together several hundred megabytes if a
    # stray transitive import pulls them in.
    excludes=["torch", "tensorflow", "transformers", "peft", "matplotlib", "pandas",
              "scipy", "IPython", "notebook", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

icon = {
    True: str(ROOT / "desktop" / "assets" / "icon.icns"),
    False: str(ROOT / "desktop" / "assets" / ("icon.ico" if IS_WIN else "icon.png")),
}[IS_MAC]

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME if (IS_WIN or IS_MAC) else "ahoos-studio",
    console=False,           # a window app: no terminal behind it
    icon=icon,
    upx=False,               # UPX-packed executables are flagged by antivirus far more often
)

coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)

if IS_MAC:
    app = BUNDLE(
        coll,
        name=f"{NAME}.app",
        icon=icon,
        bundle_identifier="site.ahoos-ai.studio",
        info_plist={
            "CFBundleShortVersionString": "1.1.0",
            "NSHighResolutionCapable": True,
            # The app talks only to itself on 127.0.0.1 and to Hugging Face
            # over HTTPS; neither needs an ATS exception.
            "LSMinimumSystemVersion": "12.0",
        },
    )
