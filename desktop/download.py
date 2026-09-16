"""Fetching a multi-gigabyte file over a connection that will drop.

This is written for the case that actually happens rather than the one that is
easy to code: a five-gigabyte download on a domestic connection, interrupted,
resumed an hour later, on a laptop that slept in between.

HOW IT SURVIVES

Bytes go to `<name>.part` and the file is renamed only once it is whole, so a
half-file is never mistaken for a model. A resume sends `Range: bytes=N-` and
appends; a server that ignores the header and answers 200 instead of 206 is
caught, because appending a fresh copy of the file onto a partial one produces a
corrupt file of plausible size, which is the worst possible outcome.

Every attempt is retried with a widening delay, and the retry count resets
whenever progress is made -- a download that is moving slowly is not a download
that is failing, and five stalls in an hour should not end it when five stalls
in a minute should.

WHY THE HASH IS CHECKED AND NOT JUST THE SIZE

A truncated file has the wrong size and is caught by arithmetic. A file that
arrived through a captive portal, a proxy that injected an error page, or a
mirror serving a different quantisation can have a plausible size. The Hub
publishes the sha256 of every file it stores; checking it turns "the model does
not load" into "the download is wrong", which is the difference between a bug
report and a retry.
"""

from __future__ import annotations

import hashlib
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

CHUNK = 1 << 20              # 1 MiB: big enough to be cheap, small enough to feel live
CONNECT_TIMEOUT = 60
MAX_STALLED_ATTEMPTS = 8
USER_AGENT = "AhoosAI-Studio-Offline"


class DownloadError(RuntimeError):
    """The file did not arrive, or arrived wrong."""


@dataclass
class Progress:
    done: int
    total: int
    speed: float                 # bytes per second, averaged over this run
    seconds_left: float | None

    @property
    def fraction(self) -> float:
        return (self.done / self.total) if self.total else 0.0


OnProgress = Callable[[Progress], None]


def _open(url: str, offset: int):
    headers = {"User-Agent": USER_AGENT}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(request, timeout=CONNECT_TIMEOUT)


def _sha256(path: Path, on_progress: OnProgress | None = None) -> str:
    digest = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    started = time.monotonic()
    with path.open("rb") as handle:
        while True:
            block = handle.read(CHUNK * 8)
            if not block:
                break
            digest.update(block)
            done += len(block)
            if on_progress:
                elapsed = max(time.monotonic() - started, 1e-6)
                on_progress(Progress(done, total, done / elapsed, None))
    return digest.hexdigest()


def fetch(url: str, target: Path, *, expected_bytes: int = 0, sha256: str = "",
          on_progress: OnProgress | None = None) -> Path:
    """Download `url` to `target`, resuming a previous attempt if there is one.

    Returns the path. Raises DownloadError with something a person can act on.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")

    if target.is_file() and (not expected_bytes or target.stat().st_size == expected_bytes):
        if sha256 and _sha256(target) != sha256:
            target.unlink()                       # wrong file: start over rather than keep it
        else:
            return target

    # One writer per file. Two copies of the app -- or the app and
    # `python -m desktop pull` -- resuming the same .part would interleave their
    # appends into a file the checksum rejects, after gigabytes. The lock is a
    # file created exclusively, so the operating system arbitrates, and a lock
    # left by a process that died is recognised by its age and taken over.
    lock = target.with_suffix(target.suffix + ".lock")
    _acquire(lock, partial)
    try:
        return _fetch_locked(url, target, partial, expected_bytes, sha256, on_progress)
    finally:
        lock.unlink(missing_ok=True)


LOCK_STALE_SECONDS = 120


def _acquire(lock: Path, partial: Path) -> None:
    for _ in range(2):
        try:
            with open(lock, "x", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
            return
        except FileExistsError:
            # A live writer touches the .part file continuously; a dead one
            # stops. Two minutes without a byte is a lock nobody holds.
            newest = max((p.stat().st_mtime for p in (lock, partial) if p.exists()), default=0)
            if time.time() - newest > LOCK_STALE_SECONDS:
                lock.unlink(missing_ok=True)
                continue
            raise DownloadError(
                "this file is already being downloaded by another window or process. "
                "Close the other one, or wait for it to finish."
            )
    raise DownloadError("could not take the download lock")


def _fetch_locked(url: str, target: Path, partial: Path, expected_bytes: int, sha256: str,
                  on_progress: OnProgress | None) -> Path:
    stalled = 0
    declared = expected_bytes
    started = time.monotonic()
    started_at_bytes = partial.stat().st_size if partial.is_file() else 0

    while True:
        offset = partial.stat().st_size if partial.is_file() else 0
        if expected_bytes and offset > expected_bytes:
            partial.unlink()                      # longer than the file: not a resume point
            offset = 0

        try:
            with _open(url, offset) as response:
                # A server that ignored the Range header sends 200 and the whole
                # file. Appending that to what is already there would make a
                # corrupt file of believable size -- so start again instead.
                if offset and response.status != 206:
                    partial.unlink(missing_ok=True)
                    continue

                total = expected_bytes
                if not total:
                    length = response.headers.get("Content-Length")
                    total = (int(length) + offset) if length else 0
                    # A caller that did not say how big the file is still gets a
                    # completeness check, because the server said. Without this a
                    # connection that closes early produces a short file that is
                    # renamed as if it were whole -- which is exactly what
                    # happened to a 161 MB adapter that arrived as 12 MB and was
                    # handed to a converter as a finished download.
                    declared = total or declared

                with partial.open("ab" if offset else "wb") as handle:
                    done = offset
                    while True:
                        block = response.read(CHUNK)
                        if not block:
                            break
                        handle.write(block)
                        done += len(block)
                        if on_progress:
                            elapsed = max(time.monotonic() - started, 1e-6)
                            speed = (done - started_at_bytes) / elapsed
                            left = ((total - done) / speed) if (speed > 0 and total) else None
                            on_progress(Progress(done, total, speed, left))

        except urllib.error.HTTPError as exc:
            if exc.code == 416:
                # The range starts past the end: what is on disk is already the
                # whole file, or longer than it. The size check below decides.
                pass
            elif exc.code in (401, 403):
                raise DownloadError(
                    f"the server refused the download ({exc.code}). "
                    "This file may have been made private or gated since the app was built."
                ) from exc
            elif exc.code == 404:
                raise DownloadError(
                    f"the file is no longer at {url}. The catalogue needs updating."
                ) from exc
            else:
                stalled += 1
                if stalled >= MAX_STALLED_ATTEMPTS:
                    raise DownloadError(f"gave up after {stalled} attempts: HTTP {exc.code}") from exc
                time.sleep(min(2 ** stalled, 60))
                continue
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            moved = (partial.stat().st_size if partial.is_file() else 0) > offset
            stalled = 0 if moved else stalled + 1
            if stalled >= MAX_STALLED_ATTEMPTS:
                raise DownloadError(
                    f"the connection failed {stalled} times in a row: {exc}. "
                    "What has arrived so far is kept; running this again resumes it."
                ) from exc
            time.sleep(min(2 ** stalled, 60))
            continue

        size = partial.stat().st_size if partial.is_file() else 0
        expected_bytes = expected_bytes or declared
        if expected_bytes and size < expected_bytes:
            # The server closed early; go round again. Only a close that brought
            # nothing counts against the limit. Counting every close ended a
            # 4.7 GB download at 1.1 GB: a CDN that recycles long connections
            # closes a two-hour transfer many times, and each of those closes had
            # moved the file forward.
            stalled = 0 if size > offset else stalled + 1
            if stalled >= MAX_STALLED_ATTEMPTS:
                raise DownloadError(
                    f"the download keeps stopping short: {size:,} of {expected_bytes:,} bytes."
                )
            time.sleep(min(2 ** stalled, 30))
            continue
        if expected_bytes and size > expected_bytes:
            partial.unlink()
            raise DownloadError(
                f"the file is longer than it should be ({size:,} vs {expected_bytes:,}). "
                "It has been removed; run this again for a clean copy."
            )
        break

    if sha256:
        actual = _sha256(partial, on_progress)
        if actual != sha256:
            partial.unlink()
            raise DownloadError(
                "the downloaded file is not the one it should be — its checksum does not "
                "match what the Hub publishes. It has been removed; try again, and if it "
                "happens twice, something between here and the Hub is changing the file."
            )

    os.replace(partial, target)
    return target
