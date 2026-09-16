"""Build the offline studio for the platform this runs on.

    python desktop/packaging/build.py

Output, in dist/:

    Windows   AhoosAI-Studio-<version>-windows-x64.zip       unzip and run the .exe
    macOS     AhoosAI-Studio-<version>-macos-<arch>.dmg      drag to Applications
    Linux     AhoosAI-Studio-<version>-linux-<arch>.tar.gz   extract and run

WHY EACH PLATFORM BUILDS ITS OWN

PyInstaller freezes the interpreter it runs under, and llama.cpp's binaries are
per platform. Neither can be cross-built, so the three builds come from three
machines -- desktop/packaging/github-workflow.yml runs this script on each. Running it by
hand on any one of them produces that one.

WHAT GOES IN

    the Python app       desktop/, studio/, and their dependencies
    ui/                  regenerated from the web studio by port_studio_ui.py
    family/              the offline registry root, assembled from the repository
    assets/              the adapter (built from the Hub if absent) and the icon
    bin/                 llama-server and its libraries for this platform

What does not go in is the base model. It is 3.8 to 5.4 GB depending on the
size chosen, and the app downloads it on first run from Qwen's own repository.
An installer that size would be downloaded by fewer people than the model is.

Every step either succeeds or stops the build with the step's name. A package
missing its runtime or its adapter starts, looks fine, and fails on the first
message -- which is the one place nobody tests.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGING = Path(__file__).resolve().parent
STAGE = ROOT / "dist" / "stage"
DIST = ROOT / "dist"
sys.path.insert(0, str(ROOT))

from desktop import __main__ as cli  # noqa: E402
from desktop import catalogue, family  # noqa: E402

VERSION = "1.1.0"
NAME = "AhoosAI Studio"


def step(title: str) -> None:
    print(f"\n== {title}", flush=True)


def run(*argv: str, **kwargs) -> None:
    print("  $", " ".join(argv), flush=True)
    subprocess.run(argv, check=True, cwd=ROOT, **kwargs)


def platform_tag() -> str:
    system = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}[platform.system()]
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine, machine)
    return f"{system}-{arch}"


def stage_runtime() -> Path:
    """llama-server for this platform, unpacked into stage/bin."""
    target = STAGE / "bin"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    name = cli.asset_for()
    archive = STAGE / name
    from desktop.download import fetch

    fetch(cli.RELEASE_URL % (cli.LLAMA_BUILD, name), archive)
    unpack = STAGE / "_runtime"
    if unpack.exists():
        shutil.rmtree(unpack)
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(unpack)
    else:
        with tarfile.open(archive) as bundle:
            bundle.extractall(unpack)

    kept = 0
    for path in unpack.rglob("*"):
        if not path.is_file():
            continue
        wanted = (path.name.startswith("llama-server") or path.suffix in (".dll", ".dylib", ".so")
                  or ".so." in path.name or path.name.startswith("lib"))
        if wanted:
            destination = target / path.name
            shutil.copy2(path, destination)
            if os.name != "nt":
                destination.chmod(0o755)
            kept += 1
    shutil.rmtree(unpack)
    binary = target / ("llama-server.exe" if os.name == "nt" else "llama-server")
    if not binary.is_file():
        raise SystemExit(f"runtime: {binary.name} is not in {name}")
    print(f"  {kept} files, {binary.name} present")
    return target


def main() -> int:
    tag = platform_tag()
    print(f"{NAME} {VERSION} for {tag}")
    STAGE.mkdir(parents=True, exist_ok=True)

    step("interface")
    # The private repository regenerates the page from the web studio; the public
    # one carries the generated page and has nothing to regenerate it from.
    if (ROOT / "tools/port_studio_ui.py").is_file():
        run(sys.executable, "tools/port_studio_ui.py")
    elif not (ROOT / "desktop/ui/index.html").is_file():
        raise SystemExit("interface: desktop/ui/index.html is missing")

    step("adapter")
    if not catalogue.adapter_path().is_file():
        # Read-only against the Hub: it downloads the published PEFT weights and
        # converts them here. Nothing is uploaded anywhere.
        run(sys.executable, "tools/adapter_to_gguf.py")
    print(f"  {catalogue.adapter_path().stat().st_size / 1e6:.1f} MB")

    step("icons")
    missing = [n for n in ("icon.png", "icon.ico", "icon.icns") if not (ROOT / "desktop/assets" / n).is_file()]
    if missing and (ROOT / "desktop/packaging/make_icons.py").is_file():
        run(sys.executable, "desktop/packaging/make_icons.py")
    elif missing:
        raise SystemExit(f"icons: {', '.join(missing)} missing from desktop/assets")

    step("registry")
    family_root = family.assemble(STAGE / "family")
    from studio.registry import Registry

    loaded = Registry.load(family_root)
    print(f"  {len(loaded.models)} model, {len(loaded.gears)} gear, {len(loaded.chips)} chip")

    step("runtime")
    bin_dir = stage_runtime()

    step("freeze")
    env = dict(os.environ,
               AHOOS_STAGE=str(STAGE), AHOOS_BIN=str(bin_dir), AHOOS_FAMILY=str(family_root))
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", str(DIST), "--workpath", str(STAGE / "pyinstaller"),
        str(PACKAGING / "studio.spec"), env=env)

    step("archive")
    out = package(tag)
    print(f"  {out}  {out.stat().st_size / 1e6:.1f} MB")
    return 0


def package(tag: str) -> Path:
    base = f"AhoosAI-Studio-{VERSION}-{tag}"
    system = platform.system()
    if system == "Darwin":
        app = DIST / f"{NAME}.app"
        dmg = DIST / f"{base}.dmg"
        dmg.unlink(missing_ok=True)
        run("hdiutil", "create", "-volname", NAME, "-srcfolder", str(app),
            "-ov", "-format", "UDZO", str(dmg))
        return dmg

    folder = DIST / NAME
    if system == "Windows":
        # Beside the program, so a person who unzips it sees what to do.
        (folder / "README.txt").write_text(
            "AhoosAI Studio (offline)\r\n\r\n"
            "Run \"AhoosAI Studio.exe\". On first start it asks which size of model to\r\n"
            "download (3.8 to 5.4 GB) and fetches it once. After that it needs no\r\n"
            "internet connection.\r\n\r\n"
            "To keep everything beside the program instead of in your user folder --\r\n"
            "for a USB stick, say -- create an empty file named portable.txt here.\r\n",
            encoding="utf-8")
        archive = DIST / f"{base}.zip"
        archive.unlink(missing_ok=True)
        shutil.make_archive(str(archive.with_suffix("")), "zip", DIST, NAME)
        return archive

    desktop_entry = folder / "ahoos-studio.desktop"
    desktop_entry.write_text(
        "[Desktop Entry]\nType=Application\nName=AhoosAI Studio\n"
        "Comment=The AhoosAI studio, offline\nExec=sh -c 'cd \"$(dirname \"%k\")\" && ./ahoos-studio'\n"
        "Icon=ahoos-studio\nCategories=Development;\nTerminal=false\n", encoding="utf-8")
    archive = DIST / f"{base}.tar.gz"
    archive.unlink(missing_ok=True)
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(folder, arcname=f"AhoosAI-Studio-{VERSION}")
    return archive


if __name__ == "__main__":
    raise SystemExit(main())
