"""Commands that keep running: a dev server, a watcher, a game window.

In 2.5, `python -m http.server 8000` held the turn for the whole command timeout
and was then killed -- two minutes of nothing, and a server that was gone the
moment it was needed. A command like that is not finished when it is quiet; it
is working.

So a command can be started in the background: it runs on, its output is kept,
and the turn gets the first few seconds of what it printed (where a server says
its address). A foreground command that is still running when its time is up,
and has printed an address, is moved to the background rather than killed. They
are all stopped when the studio closes, and any one of them can be stopped from
the conversation.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .terminal import kill_tree, shell_for

# What a command line that starts a server usually looks like.
_SERVER = re.compile(
    r"(?:http\.server|SimpleHTTPServer|\bnpm\s+(?:run\s+)?(?:start|dev|serve)\b|\byarn\s+(?:dev|start)\b|"
    r"\bpnpm\s+(?:dev|start)\b|\bnpx\s+(?:serve|http-server|live-server|vite)\b|\bvite\b|"
    r"\bflask\s+run\b|\buvicorn\b|\bgunicorn\b|manage\.py\s+runserver|\bjekyll\s+serve\b|"
    r"\bhugo\s+server\b|\blive-server\b|\bhttp-server\b|\bphp\s+-S\b|\bstreamlit\s+run\b|"
    r"\bjupyter\s+(?:notebook|lab)\b)",
    re.I,
)
_ADDRESS = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::\]|[\w.-]+):\d+[^\s'\"]*", re.I)


def looks_like_server(command: str) -> bool:
    return bool(_SERVER.search(command))


def address_in(text: str) -> str:
    match = _ADDRESS.search(text)
    if not match:
        return ""
    return match.group(0).replace("0.0.0.0", "localhost").replace("[::]", "localhost").rstrip(".,)")


@dataclass
class Proc:
    id: str
    command: str
    folder: str
    process: subprocess.Popen
    started: float = field(default_factory=time.time)
    lines: deque = field(default_factory=lambda: deque(maxlen=400))
    code: int | None = None
    url: str = ""

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    def public(self) -> dict:
        return {"id": self.id, "command": self.command, "folder": self.folder, "running": self.running,
                "code": self.code if not self.running else None, "url": self.url,
                "seconds": round(time.time() - self.started), "tail": list(self.lines)[-12:]}


class Procs:
    """Every background command this session started."""

    def __init__(self) -> None:
        self._all: dict[str, Proc] = {}
        self._lock = threading.Lock()

    def start(self, command: str, folder: Path) -> Proc:
        process = subprocess.Popen(
            shell_for(command), cwd=str(folder), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return self.adopt(command, folder, process)

    def adopt(self, command: str, folder: Path, process: subprocess.Popen, lines: list[str] | None = None,
              reader_running: bool = False) -> Proc:
        """Keep a process that is already running -- one moved out of the foreground."""
        proc = Proc(uuid.uuid4().hex[:6], command, str(folder), process)
        for line in lines or []:
            proc.lines.append(line)
            proc.url = proc.url or address_in(line)
        with self._lock:
            self._all[proc.id] = proc
        if not reader_running:
            threading.Thread(target=self._pump, args=(proc,), daemon=True, name=f"proc-{proc.id}").start()
        return proc

    def _pump(self, proc: Proc) -> None:
        try:
            for line in proc.process.stdout or []:
                line = line.rstrip("\n")[:2000]
                proc.lines.append(line)
                if not proc.url:
                    proc.url = address_in(line)
        except Exception:  # noqa: BLE001 - a closed pipe at exit
            pass
        try:
            proc.code = proc.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def feed(self, proc: Proc, line: str) -> None:
        """A line read by someone else's reader (a moved foreground command)."""
        proc.lines.append(line)
        if not proc.url:
            proc.url = address_in(line)

    def wait_for_start(self, proc: Proc, seconds: float = 6.0) -> None:
        """Give a new background command a moment to say where it is, or to fail."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not proc.running or proc.url:
                if proc.url:
                    time.sleep(0.3)     # the line after the address is often the useful one
                return
            time.sleep(0.2)

    def get(self, proc_id: str) -> Proc | None:
        return self._all.get(proc_id)

    def stop(self, proc_id: str) -> bool:
        proc = self._all.get(proc_id)
        if proc is None:
            return False
        kill_tree(proc.process)
        return True

    def stop_all(self) -> None:
        for proc in list(self._all.values()):
            try:
                kill_tree(proc.process)
            except Exception:  # noqa: BLE001 - at exit, best effort
                pass

    def public(self) -> list[dict]:
        return [p.public() for p in sorted(self._all.values(), key=lambda p: -p.started)][:20]

    def running(self) -> list[Proc]:
        return [p for p in self._all.values() if p.running]


PROCS = Procs()
