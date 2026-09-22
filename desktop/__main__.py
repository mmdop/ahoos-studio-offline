"""The offline build's own command line.

    python -m desktop fetch-runtime      get llama.cpp for this platform
    python -m desktop verify-catalogue   re-read sizes and hashes from the Hub
    python -m desktop pull [--build q4_k_m]   download the base model
    python -m desktop doctor             what is present, what is missing
    python -m desktop run                the studio itself

These are the steps a packaged build performs for the user. They are here as
commands too, because a developer needs to run them one at a time, and because a
build script that cannot be run by hand is a build script nobody can debug.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

from . import catalogue, runtime
from .download import DownloadError, fetch
from .paths import bin_dir, models_dir

# The CPU builds, one per platform. No CUDA or ROCm here on purpose: those are
# hundreds of megabytes each and only pay off on hardware the app cannot assume.
# Someone with a card can point the app at their own build; everyone else gets
# something that runs everywhere.
LLAMA_BUILD = "b10991"
RUNTIME_ASSETS = {
    ("Windows", "AMD64"):  "llama-%s-bin-win-cpu-x64.zip",
    ("Windows", "ARM64"):  "llama-%s-bin-win-cpu-arm64.zip",
    ("Linux", "x86_64"):   "llama-%s-bin-ubuntu-x64.tar.gz",
    ("Linux", "aarch64"):  "llama-%s-bin-ubuntu-arm64.tar.gz",
    ("Darwin", "arm64"):   "llama-%s-bin-macos-arm64.tar.gz",
    ("Darwin", "x86_64"):  "llama-%s-bin-macos-x64.tar.gz",
}
RELEASE_URL = "https://github.com/ggml-org/llama.cpp/releases/download/%s/%s"


def asset_for(system: str | None = None, machine: str | None = None) -> str:
    system = system or platform.system()
    machine = machine or platform.machine()
    key = (system, machine)
    if key not in RUNTIME_ASSETS:
        raise SystemExit(
            f"no llama.cpp build is published for {system}/{machine}. "
            "Build it yourself and put llama-server beside the app."
        )
    return RUNTIME_ASSETS[key] % LLAMA_BUILD


def human(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(count) < 1024 or unit == "GB":
            return f"{count:,.1f} {unit}"
        count /= 1024
    return f"{count:,.1f} GB"


def bar(label: str):
    """A progress line that rewrites itself, and says something useful."""
    last = [0.0]

    def report(progress) -> None:
        now = time.monotonic()
        if now - last[0] < 0.25 and progress.fraction < 1:
            return
        last[0] = now
        left = ""
        if progress.seconds_left and progress.seconds_left > 1:
            minutes, seconds = divmod(int(progress.seconds_left), 60)
            left = f"  {minutes:d}m{seconds:02d}s left"
        filled = int(progress.fraction * 28)
        sys.stdout.write(
            f"\r  {label}  [{'#' * filled}{'.' * (28 - filled)}] "
            f"{progress.fraction * 100:5.1f}%  {human(progress.speed)}/s{left}   "
        )
        sys.stdout.flush()

    return report


# -- commands ---------------------------------------------------------------

def cmd_fetch_runtime(args) -> int:
    name = asset_for()
    target = bin_dir()
    target.mkdir(parents=True, exist_ok=True)
    archive = target / name
    print(f"llama.cpp {LLAMA_BUILD} for {platform.system()}/{platform.machine()}")
    try:
        fetch(RELEASE_URL % (LLAMA_BUILD, name), archive, on_progress=bar(name[:34]))
    except DownloadError as exc:
        print(f"\n  {exc}")
        return 1
    print()

    # The archives keep their files in a build/bin/ prefix on some platforms and
    # at the root on others. Flattening means find_binary() has one place to look.
    staging = target / "_unpacked"
    if staging.exists():
        shutil.rmtree(staging)
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(staging)
    else:
        with tarfile.open(archive) as bundle:
            bundle.extractall(staging)

    moved = 0
    for path in staging.rglob("*"):
        if not path.is_file():
            continue
        if path.name.startswith("llama-server") or path.suffix in (".dll", ".so", ".dylib") \
                or ".so." in path.name:
            destination = target / path.name
            shutil.move(str(path), destination)
            if os.name != "nt":
                destination.chmod(0o755)
            moved += 1
    shutil.rmtree(staging)
    archive.unlink()

    print(f"  {moved} files in {target}")
    try:
        print("  found:", runtime.find_binary(target))
    except runtime.RuntimeError_ as exc:
        print(f"  {exc}")
        return 1
    return 0


def cmd_verify_catalogue(args) -> int:
    """Ask the Hub what it actually has, and say where we disagree.

    One request per base repository, because each model's builds live in its
    own -- and a size that is a few hundred bytes out is a download the app
    would reject as damaged."""
    wrong = 0
    for model in catalogue.MODELS:
        wanted = {b.filename: b for b in catalogue.builds_for(model.id)}
        if not wanted:
            continue
        print(f"{model.name}  <-  {model.base_repo}")
        body = json.dumps({"paths": list(wanted)}).encode()
        request = urllib.request.Request(
            f"https://huggingface.co/api/models/{model.base_repo}/paths-info/main",
            data=body, headers={"Content-Type": "application/json", "User-Agent": "AhoosAI"})
        with urllib.request.urlopen(request, timeout=60) as response:
            live = json.loads(response.read())
        for entry in live:
            build = wanted.pop(entry["path"], None)
            if build is None:
                continue
            size = entry.get("size", 0)
            sha = (entry.get("lfs") or {}).get("oid", "")
            ok = size == build.bytes and sha == build.sha256
            wrong += 0 if ok else 1
            print(f"  {'ok  ' if ok else 'DIFF'} {build.key:<12} {size:>13,}  {sha[:16]}")
            if not ok:
                print(f"       catalogue says {build.bytes:>13,}  {build.sha256[:16]}")
        for missing in wanted:
            wrong += 1
            print(f"  GONE {missing} is no longer in {model.base_repo}")

    print("catalogue matches the Hub" if not wrong else f"{wrong} entries need updating")
    return 0 if not wrong else 1


def cmd_pull(args) -> int:
    build = catalogue.build(args.build)
    where = models_dir()
    print(f"base     {build.filename}  {build.gigabytes:.2f} GB")
    try:
        fetch(build.url, where / build.filename, expected_bytes=build.bytes,
              sha256=build.sha256, on_progress=bar("base   "))
        print()
    except DownloadError as exc:
        print(f"\n  {exc}")
        return 1
    print(f"in {where}")
    adapter = catalogue.adapter_path(build.model)
    if not adapter.is_file():
        print(f"adapter  {adapter.name} missing from the app -- "
              "build it with: python tools/adapter_to_gguf.py")
        return 1
    return 0


def cmd_doctor(args) -> int:
    facts = runtime.machine()
    print(f"machine   {facts['system']} {facts['machine']}, {facts['cpus']} cpus, "
          f"{facts['ram_bytes'] / 1e9:.1f} GB memory, {facts['free_disk_bytes'] / 1e9:.0f} GB free")
    try:
        print(f"runtime   {runtime.find_binary()}")
    except runtime.RuntimeError_:
        print("runtime   missing -- run: python -m desktop fetch-runtime")

    where = models_dir()
    print(f"models    {where}")
    for model in catalogue.MODELS:
        print(f"  {model.name}")
        for build in catalogue.builds_for(model.id):
            path = where / build.filename
            if path.is_file():
                complete = path.stat().st_size == build.bytes
                print(f"    {'ok  ' if complete else 'PART'} {build.key:<12} {build.filename}")
            else:
                print(f"    --   {build.key:<12} not downloaded")
        adapter = catalogue.adapter_path(model.id)
        print(f"    {'ok  ' if adapter.is_file() else '--  '} adapter      {adapter.name}")
    return 0


def cmd_run(args) -> int:
    from .app import run

    return run(engine=args.engine, browser=args.browser, port=args.port)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m desktop", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("fetch-runtime", help="download llama.cpp for this platform")
    sub.add_parser("verify-catalogue", help="check sizes and hashes against the Hub")
    pull = sub.add_parser("pull", help="download the model and the adapter")
    pull.add_argument("--build", default=catalogue.DEFAULT_BUILD,
                      choices=[b.key for b in catalogue.BUILDS])
    sub.add_parser("doctor", help="what is present and what is missing")
    run = sub.add_parser("run", help="start the offline studio")
    run.add_argument("--browser", action="store_true", help="open in the browser, not a window")
    run.add_argument("--engine", default="local", help="`echo` runs the interface with no model")
    run.add_argument("--port", type=int, default=0)

    args = parser.parse_args(argv)
    return {
        "fetch-runtime": cmd_fetch_runtime,
        "verify-catalogue": cmd_verify_catalogue,
        "pull": cmd_pull,
        "doctor": cmd_doctor,
        "run": cmd_run,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
