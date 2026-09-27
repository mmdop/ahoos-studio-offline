"""The download queue: everything the app fetches, one file at a time.

A build from the catalogue, a GGUF picked from the Hub, an adapter pasted in as
a link -- each becomes a job here and waits its turn. One at a time on purpose:
two five-gigabyte downloads side by side on a domestic connection finish no
sooner than one after the other, and both are half-done for twice as long.

Every file goes through download.fetch, so every one resumes after a dropped
connection and is checked against the Hub's size and checksum. A cancelled job
keeps its `.part` files; starting it again picks up where it stopped.
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from .download import DownloadError, Progress, fetch


class Cancelled(Exception):
    pass


class Jobs:
    def __init__(self, finish: Callable[[dict], None]) -> None:
        # `finish(job)` runs on the worker thread once a job's files are all here:
        # it registers them, converts an adapter, switches to a first model.
        self.finish = finish
        self.jobs: list[dict] = []
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._cancel: dict[str, threading.Event] = {}
        threading.Thread(target=self._work, daemon=True, name="downloads").start()

    # -- the queue -------------------------------------------------------------

    def add(self, *, title: str, kind: str, files: list[dict], folder: Path, extra: dict | None = None) -> dict:
        """`files` are {url, path (relative to folder), bytes, sha256}."""
        with self._lock:
            for job in self.jobs:
                if job["state"] in ("queued", "running") and job["title"] == title and job["kind"] == kind:
                    return job
            job = {"id": uuid.uuid4().hex[:10], "title": title, "kind": kind, "state": "queued",
                   "files": files, "folder": str(folder), "done": 0,
                   "total": sum(int(f.get("bytes") or 0) for f in files), "speed": 0.0, "seconds_left": None,
                   "current": "", "error": "", "added": time.time(), "extra": extra or {}}
            self.jobs.append(job)
        self._wake.set()
        return job

    def cancel(self, job_id: str) -> None:
        with self._lock:
            for job in self.jobs:
                if job["id"] == job_id:
                    if job["state"] == "queued":
                        job["state"] = "cancelled"
                    elif job["id"] in self._cancel:
                        self._cancel[job["id"]].set()

    def retry(self, job_id: str) -> None:
        with self._lock:
            for job in self.jobs:
                if job["id"] == job_id and job["state"] in ("failed", "cancelled"):
                    job.update(state="queued", error="")
        self._wake.set()

    def remove(self, job_id: str) -> None:
        with self._lock:
            self.jobs = [j for j in self.jobs if not (j["id"] == job_id and j["state"] not in ("running",))]

    def public(self) -> list[dict]:
        with self._lock:
            return [{k: v for k, v in job.items() if k not in ("files", "extra")}
                    | {"count": len(job["files"])} for job in self.jobs]

    def running(self) -> bool:
        with self._lock:
            return any(j["state"] in ("queued", "running", "converting") for j in self.jobs)

    # -- the worker ----------------------------------------------------------------

    def _next(self) -> dict | None:
        with self._lock:
            return next((j for j in self.jobs if j["state"] == "queued"), None)

    def _work(self) -> None:
        while True:
            job = self._next()
            if job is None:
                self._wake.wait(timeout=5)
                self._wake.clear()
                continue
            stop = threading.Event()
            with self._lock:
                self._cancel[job["id"]] = stop
                job["state"] = "running"
            try:
                self._run(job, stop)
            except Cancelled:
                job.update(state="cancelled", speed=0.0, seconds_left=None)
            except DownloadError as error:
                job.update(state="failed", error=str(error), speed=0.0)
            except Exception as error:  # noqa: BLE001 - shown on the job, not lost in a thread
                job.update(state="failed", error=str(error), speed=0.0)
            finally:
                with self._lock:
                    self._cancel.pop(job["id"], None)

    def _run(self, job: dict, stop: threading.Event) -> None:
        folder = Path(job["folder"])
        before = 0
        for item in job["files"]:
            target = folder / item["path"]
            job["current"] = Path(item["path"]).name

            def progress(p: Progress, base=before) -> None:
                if stop.is_set():
                    raise Cancelled()
                job.update(done=base + p.done, speed=p.speed, seconds_left=p.seconds_left)

            fetch(item["url"], target, expected_bytes=int(item.get("bytes") or 0),
                  sha256=item.get("sha256") or "", on_progress=progress, headers=item.get("headers"))
            before += int(item.get("bytes") or target.stat().st_size)
            job["done"] = before
        job.update(state="converting" if job["kind"] == "adapter-peft" else "running", current="",
                   speed=0.0, seconds_left=None)
        self.finish(job)
        job["state"] = "done"
