"""The packaged app's entry point.

PyInstaller needs a script, not a module, and `python -m desktop run` is a module.
This is that command, plus the one thing a windowed build needs that a terminal
does not: somewhere for its output to go.

WHY THE LOG FILE

A build with no console starts with sys.stdout and sys.stderr set to None. Every
print is silently dropped, and anything that touches the stream itself -- a
logging handler asking whether the terminal supports colour -- raises inside a
thread nobody is watching. The first packaged build did exactly that: the window
process stayed alive, never opened a port, and said nothing about why.

So both streams go to logs/studio.log in the data folder, which is also the file
to ask someone for when something goes wrong on their machine.
"""

import os
import sys
import traceback


def _log_to_file() -> None:
    if sys.stdout is not None and sys.stderr is not None:
        return
    from desktop.paths import data_dir

    folder = data_dir() / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / "studio.log"
    # Kept short: the previous run is what matters, not last month's.
    if log.is_file() and log.stat().st_size > 2_000_000:
        log.replace(folder / "studio.previous.log")
    stream = open(log, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or stream
    sys.stderr = sys.stderr or stream
    print(f"\n--- start, pid {os.getpid()} ---", flush=True)


if __name__ == "__main__":
    _log_to_file()
    try:
        from desktop.app import run

        sys.exit(run(browser="--browser" in sys.argv))
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        raise
