"""A shell in the working folder, one line at a time.

WHY NOT A REAL TERMINAL EMULATOR

A pseudo-terminal and an emulator on top of it would give colours, cursor
movement and programs that repaint the screen. It would also add a vendored
JavaScript library, a platform-specific PTY dependency, and a surface where a
program can move the cursor anywhere it likes. What this is for is running a
command in the folder and reading what it said -- `git status`, `npm test`,
`ls` -- so it runs one command at a time, captures both streams, and sends the
text back. A program that insists on a real terminal says so and is not run.

WHY EVERY COMMAND IS STILL ASKED ABOUT

This is the part of the app that can do anything, so it is behind the same gate
as writing a file: the command line is shown exactly as it will be run, and
nothing runs until the person says so. `Controller.needs_asking` decides
whether they have already said so this session.

WHAT IS REFUSED OUTRIGHT

Nothing, by pattern. A list of forbidden commands reads as safety and is not:
it stops the obvious spelling and misses the same thing written differently,
while teaching the person that the gate can be trusted to catch things. The gate
is the person. What is enforced is the working directory -- a command runs in
the chosen folder and nowhere else.
"""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue

TIMEOUT = 600.0                 # seconds one command may take before it is stopped
MAX_LINE = 4000
MAX_LINES = 2000                # what one command may send back before it is cut


def kill_tree(process: subprocess.Popen) -> None:
    """Stop a command and whatever it started.

    On Windows the process is cmd.exe, and killing it leaves `python app.py`
    running underneath with no window and nobody to stop it.
    """
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                       capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        process.kill()


def shell_for(command: str) -> list[str]:
    """The argv that runs a command line as the platform's own shell would."""
    if os.name == "nt":
        return ["cmd.exe", "/d", "/s", "/c", command]
    return ["/bin/sh", "-lc", command]


@dataclass
class Run:
    """One command, running or finished."""

    command: str
    folder: Path
    lines: list[str] = field(default_factory=list)
    code: int | None = None
    cut: bool = False
    error: str = ""


class Shell:
    """Runs one command at a time in one folder, streaming its output."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.process: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self) -> None:
        with self._lock:
            if self.busy and self.process is not None:
                kill_tree(self.process)

    def run(self, command: str, timeout: float = TIMEOUT) -> Queue:
        """Start the command; lines arrive on the returned queue, then None.

        A queue rather than a generator because the reader is a WebSocket on a
        different thread, and because a command that produces nothing for a
        minute must not hold the socket open with no way to cancel it.
        """
        command = command.strip()
        out: Queue = Queue()
        if not command:
            out.put(None)
            return out
        with self._lock:
            if self.busy:
                out.put({"error": "a command is already running"})
                out.put(None)
                return out
            try:
                self.process = subprocess.Popen(
                    shell_for(command), cwd=str(self.folder),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                    errors="replace", bufsize=1,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as error:
                out.put({"error": f"could not run it: {error}"})
                out.put(None)
                return out
        process = self.process

        # The deadline is a timer, not a wait after the output ends: a program
        # that never closes its output -- a dev server, a watcher -- would
        # otherwise hold the loop below open and the limit would never apply.
        expired = threading.Event()

        def expire() -> None:
            expired.set()
            kill_tree(process)

        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()

        def pump() -> None:
            count = 0
            code = None
            try:
                for line in process.stdout or []:
                    count += 1
                    if count > MAX_LINES:
                        out.put({"cut": True})
                        kill_tree(process)
                        break
                    out.put({"line": line.rstrip("\n")[:MAX_LINE]})
                code = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                kill_tree(process)
            except Exception as error:  # noqa: BLE001 - reported, not swallowed
                out.put({"error": str(error)})
            finally:
                timer.cancel()
            if expired.is_set():
                out.put({"error": f"stopped after {timeout:.0f} seconds"})
                code = None
            out.put({"code": code})
            out.put(None)

        threading.Thread(target=pump, daemon=True, name="shell").start()
        return out


def drain(queue: Queue, timeout: float = 0.5):
    """Whatever is on the queue now, without waiting for more."""
    items = []
    while True:
        try:
            item = queue.get(timeout=timeout)
        except Empty:
            return items, False
        if item is None:
            return items, True
        items.append(item)
