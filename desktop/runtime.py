"""Starting, watching and stopping the local model server.

The offline build runs llama.cpp's `llama-server`, which speaks the same
chat-completions endpoint every cloud provider does. That is the whole reason
this app needed no new engine: `studio/engines/openai_engine.py` already talks to
that shape, and the `local` preset points it at this process.

WHY A SEPARATE PROCESS AND NOT A LIBRARY

`llama-cpp-python` would put the model in this interpreter, which sounds simpler
and is worse in the two ways that matter here. A 7B model that fails to allocate
takes the window down with it rather than printing a message, and the build
becomes a compiled extension that has to be built per Python version per
platform. A subprocess can crash, be reported, and be restarted, and the binary
is the one llama.cpp themselves publish for each platform.

WHY IT WAITS FOR /health AND NOT FOR A SLEEP

Loading a five-gigabyte model off a cold disk takes anywhere from four seconds to
a minute and a half. Any fixed wait is either a stall on a fast machine or a
false failure on a slow one, and the failure arrives as "connection refused" in
the middle of the user's first question rather than as "still loading".
"""

from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path


class RuntimeError_(RuntimeError):
    """The model server could not be started, or died."""


BINARY = "llama-server.exe" if os.name == "nt" else "llama-server"
LOAD_TIMEOUT = 600           # a 5 GB model off a cold spinning disk, with room to spare
POLL = 0.4


def find_binary(bundled: Path | None = None) -> Path:
    """Where llama-server is: beside the app first, then the PATH.

    Beside the app first on purpose. A user who has their own llama.cpp on the
    PATH may have a build from any month, and a version mismatch shows up as a
    model that loads and answers nonsense rather than as an error. The one we
    ship is the one we tested.
    """
    if bundled is None:
        # paths.bin_dir() knows the difference between a frozen build, which
        # carries the binary inside it, and a checkout, which keeps it with the
        # data. Guessing from __file__ here found neither: fetch-runtime wrote
        # to one place and this looked in another, so `doctor` reported a
        # missing runtime thirty seconds after installing it.
        from .paths import bin_dir

        bundled = bin_dir()
    candidate = bundled / BINARY
    if candidate.is_file():
        return candidate

    from shutil import which

    found = which(BINARY)
    if found:
        return Path(found)

    raise RuntimeError_(
        f"{BINARY} is not beside the app and not on the PATH.\n"
        "The offline build ships it; a source checkout needs it fetched once with\n"
        "  python -m desktop fetch-runtime"
    )


def free_port(preferred: int = 8177) -> int:
    """`preferred` if it is free, otherwise whatever the OS hands out."""
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return probe.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError_("no free port on the loopback interface")


@dataclass
class Settings:
    model: Path
    adapter: Path | None = None
    context: int = 8192
    threads: int = 0             # 0 lets llama.cpp choose
    gpu_layers: int = 0          # CPU by default; the shipped binary has no CUDA
    port: int = 0


class ModelServer:
    """One llama-server process, with its log kept and its death noticed."""

    def __init__(self, settings: Settings, *, binary: Path | None = None) -> None:
        self.settings = settings
        self.binary = binary or find_binary()
        self.port = settings.port or free_port()
        self.process: subprocess.Popen | None = None
        self.log: deque[str] = deque(maxlen=400)
        self._reader: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def command(self) -> list[str]:
        argv = [
            str(self.binary),
            "--model", str(self.settings.model),
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "--ctx-size", str(self.settings.context),
            # Nothing outside this machine may reach it. The API is unauthenticated
            # because it is not reachable, and those two facts have to stay joined.
            "--no-webui",
        ]
        if self.settings.adapter:
            argv += ["--lora", str(self.settings.adapter)]
        if self.settings.threads:
            argv += ["--threads", str(self.settings.threads)]
        if self.settings.gpu_layers:
            argv += ["--n-gpu-layers", str(self.settings.gpu_layers)]
        return argv

    # -- lifecycle ---------------------------------------------------------

    def start(self, on_line=None) -> None:
        if not self.settings.model.is_file():
            raise RuntimeError_(f"the model file is missing: {self.settings.model}")
        if self.settings.adapter and not self.settings.adapter.is_file():
            raise RuntimeError_(f"the adapter file is missing: {self.settings.adapter}")

        creation = 0
        if os.name == "nt":
            # Without this a console window opens in front of the app every time.
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        self.process = subprocess.Popen(
            self.command(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            creationflags=creation,
        )

        def pump() -> None:
            assert self.process and self.process.stdout
            for line in self.process.stdout:
                line = line.rstrip()
                self.log.append(line)
                if on_line:
                    on_line(line)

        self._reader = threading.Thread(target=pump, daemon=True, name="llama-log")
        self._reader.start()

    def wait_until_ready(self, timeout: float = LOAD_TIMEOUT, on_wait=None) -> None:
        """Block until the server answers, or until it is clear it never will."""
        deadline = time.monotonic() + timeout
        url = f"http://127.0.0.1:{self.port}/health"
        while time.monotonic() < deadline:
            if self.process and self.process.poll() is not None:
                raise RuntimeError_(
                    "the model server stopped while loading.\n" + self.tail(12)
                )
            try:
                with urllib.request.urlopen(url, timeout=3) as response:
                    if response.status == 200:
                        return
            except urllib.error.HTTPError as exc:
                if exc.code == 503:            # loading: the answer we are waiting for
                    pass
            except Exception:
                pass                            # not listening yet
            if on_wait:
                on_wait(self.tail(1))
            time.sleep(POLL)

        raise RuntimeError_(
            f"the model did not finish loading within {int(timeout)} seconds.\n" + self.tail(12)
        )

    def alive(self) -> bool:
        return bool(self.process) and self.process.poll() is None

    def tail(self, lines: int = 20) -> str:
        return "\n".join(list(self.log)[-lines:])

    def stop(self, grace: float = 10.0) -> None:
        if not self.process:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                # A model mid-generation can ignore a terminate for a while. The
                # process holds gigabytes; leaving it behind is worse than a kill.
                self.process.kill()
                self.process.wait(timeout=grace)
        self.process = None


def probe(base_url: str, timeout: float = 5.0) -> dict:
    """What the server says it is serving. For the about box and for support."""
    request = urllib.request.Request(base_url.rstrip("/") + "/models")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def machine() -> dict:
    """Enough about this computer to explain a slow answer, and no more."""
    import shutil

    total_ram = 0
    try:
        if os.name == "nt":
            import ctypes

            class Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = Status()
            status.dwLength = ctypes.sizeof(Status)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            total_ram = status.ullTotalPhys
        else:
            total_ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except Exception:
        total_ram = 0

    return {
        "system": platform.system(),
        "machine": platform.machine(),
        "cpus": os.cpu_count() or 0,
        "ram_bytes": total_ram,
        "free_disk_bytes": shutil.disk_usage(Path.home()).free,
    }
