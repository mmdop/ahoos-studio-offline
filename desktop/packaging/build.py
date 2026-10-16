"""Build the offline studio for the platform this runs on.

    python desktop/packaging/build.py

Output, in dist/:

    Windows   AhoosAI-Studio-<version>-windows-x64-setup.exe  an installer (for this user, no admin)
              AhoosAI-Studio-<version>-windows-x64.zip        the same, unzip and run the .exe
    macOS     AhoosAI-Studio-<version>-macos-<arch>.dmg       drag to Applications
    Linux     AhoosAI-Studio-<version>-linux-<arch>.deb       an installer for Debian and Ubuntu
              AhoosAI-Studio-<version>-linux-<arch>.tar.gz    extract and run

The Windows installer is made with Inno Setup (ISCC, on GitHub's Windows images), the .deb with
dpkg-deb; where either is missing the archive alone is made, and the build says so.

WHY EACH PLATFORM BUILDS ITS OWN

PyInstaller freezes the interpreter it runs under, and llama.cpp's binaries are
per platform. Neither can be cross-built, so the three builds come from three
machines -- desktop/packaging/github-workflow.yml runs this script on each. Running it by
hand on any one of them produces that one.

WHAT GOES IN

    the Python app       desktop/, studio/, and their dependencies
    ui/                  the studio's page, written for the desktop
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
from desktop import __version__, catalogue, family  # noqa: E402

VERSION = __version__
NAME = "AhoosAI Studio"

# Nimbus 2 Apex's adapter, exactly as published on the Hub.
APEX_GGUF_URL = "https://huggingface.co/AhoosAI/nimbus-2-apex/resolve/main/nimbus-2-apex.gguf"
APEX_GGUF_BYTES = 174_622_368
APEX_GGUF_SHA256 = "21e406daa25dd46f464b0c2204f9aca268f41ef74c1d046c767d67255a30a485"


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
    # The desktop's own page since 2.5.0; nothing is generated from the web studio.
    for name in ("studio.html", "studio.css", "studio.js", "i18n.js", "md.js", "starters.js"):
        if not (ROOT / "desktop/ui" / name).is_file():
            raise SystemExit(f"interface: desktop/ui/{name} is missing")

    step("adapters")
    # One per model, and both have to be here: a package missing an adapter
    # starts, looks fine, and fails on the first message to that model.
    for model in catalogue.MODELS:
        path = catalogue.adapter_path(model.id)
        if not path.is_file():
            if model.id == "nimbus-1.1-prime-ee":
                # Read-only against the Hub: it downloads the published PEFT
                # weights and converts them here. Nothing is uploaded anywhere.
                run(sys.executable, "tools/adapter_to_gguf.py")
            else:
                # Published as a GGUF beside the PEFT weights, so it is fetched
                # as it is rather than converted -- and pinned, so a changed
                # file on the Hub stops the build instead of shipping quietly.
                from desktop.download import fetch

                fetch(APEX_GGUF_URL, path, expected_bytes=APEX_GGUF_BYTES, sha256=APEX_GGUF_SHA256)
        print(f"  {model.name:<22} {path.stat().st_size / 1e6:6.1f} MB  {path.name}")

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

    step("installer")
    made = installer(tag)
    if made:
        print(f"  {made}  {made.stat().st_size / 1e6:.1f} MB")
    return 0


def iscc() -> str:
    for candidate in (shutil.which("iscc"), shutil.which("ISCC"),
                      r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe", r"C:\Program Files\Inno Setup 6\ISCC.exe"):
        if candidate and Path(candidate).is_file():
            return candidate
    return ""


def installer(tag: str) -> Path | None:
    """An installer beside the archive: Inno Setup on Windows, a .deb on Linux (macOS has its .dmg)."""
    base = f"AhoosAI-Studio-{VERSION}-{tag}"
    system = platform.system()
    folder = DIST / NAME
    if system == "Windows":
        compiler = iscc()
        if not compiler:
            print("  no Inno Setup here (ISCC): the .zip alone")
            return None
        script = STAGE / "studio.iss"
        script.write_text(f"""; AhoosAI Studio's installer, written by desktop/packaging/build.py
[Setup]
AppId=AhoosAI-Studio-Offline
AppName={NAME}
AppVersion={VERSION}
AppPublisher=AhoosAI
AppPublisherURL=https://ahoos-ai.site
DefaultDirName={{localappdata}}\\Programs\\{NAME}
DefaultGroupName={NAME}
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir={DIST}
OutputBaseFilename={base}-setup
SetupIconFile={ROOT / "desktop/assets/icon.ico"}
UninstallDisplayIcon={{app}}\\{NAME}.exe
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{{cm:CreateDesktopIcon}}"; GroupDescription: "{{cm:AdditionalIcons}}"

[Files]
Source: "{folder}\\*"; DestDir: "{{app}}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{{group}}\\{NAME}"; Filename: "{{app}}\\{NAME}.exe"
Name: "{{autodesktop}}\\{NAME}"; Filename: "{{app}}\\{NAME}.exe"; Tasks: desktopicon

[Run]
Filename: "{{app}}\\{NAME}.exe"; Description: "{{cm:LaunchProgram,{NAME}}}"; Flags: nowait postinstall skipifsilent
""", encoding="utf-8")
        run(compiler, "/Q", str(script))
        return DIST / f"{base}-setup.exe"
    if system == "Linux":
        if not shutil.which("dpkg-deb"):
            print("  no dpkg-deb here: the .tar.gz alone")
            return None
        arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine().lower(), platform.machine().lower())
        root = STAGE / "deb"
        shutil.rmtree(root, ignore_errors=True)
        (root / "DEBIAN").mkdir(parents=True)
        shutil.copytree(folder, root / "opt" / "ahoos-studio")
        (root / "usr" / "bin").mkdir(parents=True)
        (root / "usr" / "bin" / "ahoos-studio").symlink_to("/opt/ahoos-studio/ahoos-studio")
        apps = root / "usr" / "share" / "applications"
        apps.mkdir(parents=True)
        (apps / "ahoos-studio.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=AhoosAI Studio\nComment=The AhoosAI studio, offline\n"
            "Exec=/opt/ahoos-studio/ahoos-studio\nIcon=ahoos-studio\nCategories=Development;\nTerminal=false\n",
            encoding="utf-8")
        icons = root / "usr" / "share" / "icons" / "hicolor" / "256x256" / "apps"
        icons.mkdir(parents=True)
        shutil.copy(ROOT / "desktop/assets/icon.png", icons / "ahoos-studio.png")
        size = sum(f.stat().st_size for f in root.rglob("*") if f.is_file() and not f.is_symlink()) // 1024
        (root / "DEBIAN" / "control").write_text(
            f"Package: ahoos-studio\nVersion: {VERSION}\nSection: devel\nPriority: optional\n"
            f"Architecture: {arch}\nInstalled-Size: {size}\nMaintainer: AhoosAI <noreply@ahoos-ai.site>\n"
            "Homepage: https://ahoos-ai.site\n"
            "Description: AhoosAI Studio, offline\n"
            " The AhoosAI studio on your own computer: Nimbus models run locally, placed by PML-sRUN,\n"
            " and online models with an API key.\n", encoding="utf-8")
        out = DIST / f"{base}.deb"
        out.unlink(missing_ok=True)
        run("dpkg-deb", "--root-owner-group", "--build", str(root), str(out))
        return out
    return None


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
