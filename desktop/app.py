"""AhoosAI Studio, offline: the program a person double-clicks.

    python -m desktop run                 the app, in its own window
    python -m desktop run --browser       the same, in the default browser
    python -m desktop run --engine echo   the whole interface with no model --
                                          for working on the app, not for use

What happens on start:

  1. the local web server comes up on 127.0.0.1, on a free port
  2. if a model is installed, llama-server starts loading it in the background
  3. a window opens on the studio -- or on the setup page, if there is no model
     yet, where the person picks a size and downloads it

The window never waits for the model. Loading five gigabytes takes up to a
minute on a slow disk, and a window that appears after a minute of nothing looks
like a program that did not start. The page says "loading" instead, and a
message sent before the model is ready is answered with that, not with an error.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import catalogue, family, runtime
from .download import DownloadError, Progress, fetch
from .paths import data_dir, models_dir, settings_file
from .store import Store

# The online deployment's gates must not leak into a program on someone's own
# computer. A developer's shell with NIMBUS_API_KEY set would otherwise make the
# local API demand a key the page does not have, and every message would come
# back 401. Cleared before studio.api is imported, because it reads them then.
for _name in ("NIMBUS_API_KEY", "SUPABASE_URL", "SUPABASE_SERVICE_KEY", "NIMBUS_CORS_ORIGINS"):
    os.environ.pop(_name, None)
# One attempt, a long wait. Retrying a local generation that timed out does the
# whole generation again, on the same CPU, and doubles the wait for nothing.
os.environ.setdefault("NIMBUS_HTTP_ATTEMPTS", "1")
os.environ.setdefault("NIMBUS_HTTP_TIMEOUT", "3600")

DEFAULT_SETTINGS = {"build": catalogue.DEFAULT_BUILD, "context": 8192, "threads": 0, "gpu_layers": 0}


class Cancelled(Exception):
    """Raised from the progress callback to stop a download where it is."""


class Controller:
    """Everything the pages ask about: the model, its download, its server."""

    def __init__(self, engine: str = "local") -> None:
        self.engine = engine
        self.store = Store(data_dir() / "studio")
        self.settings = self._load_settings()
        self.state = "missing"            # missing | loading | ready | failed
        self.error = ""
        self.server: runtime.ModelServer | None = None
        self.download: dict | None = None
        self._cancel = threading.Event()
        self._lock = threading.RLock()
        self.self_url = ""

        from studio.api.app import State, create_app

        root = family.root()
        self.api = create_app(root=root, engine=engine)
        # Loaded here and not left to the API's lifespan hook. Starlette does not
        # run a mounted app's lifespan, so that hook never fired, State.get()
        # fell back to the default root, and the offline studio answered with
        # the online family -- six models, a fifteen-thousand-character manager
        # prompt about delegating to specialists who are not here.
        State.load(root)
        self._seed_chips(root)

    # -- settings ------------------------------------------------------------

    def _load_settings(self) -> dict:
        path = settings_file()
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = {}
        return {**DEFAULT_SETTINGS, **{k: v for k, v in stored.items() if k in DEFAULT_SETTINGS}}

    def _save_settings(self) -> None:
        path = settings_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.settings, indent=1), encoding="utf-8")

    def _seed_chips(self, root: Path) -> None:
        try:
            from studio.chips import load_chips

            described = [{"id": c.id, "name": c.name, "summary": c.summary,
                          "licence_holder": c.license.holder} for c in load_chips(root / "skills")]
            self.store.seed_builtin_chips(described)
        except Exception as exc:  # noqa: BLE001 - a chip description is not worth failing start for
            print(f"[chips] could not describe built-in chips: {exc}", file=sys.stderr)

    # -- what is installed ------------------------------------------------------

    @staticmethod
    def _complete(build: catalogue.Build) -> bool:
        path = models_dir() / build.filename
        return path.is_file() and path.stat().st_size == build.bytes

    def installed_builds(self) -> list[catalogue.Build]:
        return [b for b in catalogue.BUILDS if self._complete(b)]

    def installed(self) -> bool:
        if self.engine != "local":
            return True
        return bool(self.installed_builds()) and catalogue.adapter_path().is_file()

    def current_build(self) -> catalogue.Build | None:
        chosen = [b for b in self.installed_builds() if b.key == self.settings["build"]]
        if chosen:
            return chosen[0]
        installed = self.installed_builds()
        return installed[0] if installed else None

    def ready(self) -> bool:
        return self.engine != "local" or self.state == "ready"

    def not_ready_reason(self) -> str:
        if self.state == "loading":
            return "The model is still loading. It takes up to a minute after the app starts."
        if self.state == "failed":
            return f"The model could not start: {self.error}"
        return "No model is installed yet. Open Model and downloads to get one."

    # -- the model server -----------------------------------------------------

    def start_model(self) -> None:
        if self.engine != "local":
            self.state = "ready"
            return
        build = self.current_build()
        if build is None or not catalogue.adapter_path().is_file():
            self.state = "missing"
            return

        def work() -> None:
            with self._lock:
                self.stop_model()
                self.state, self.error = "loading", ""
                try:
                    server = runtime.ModelServer(runtime.Settings(
                        model=models_dir() / build.filename,
                        adapter=catalogue.adapter_path(),
                        context=int(self.settings["context"]),
                        threads=int(self.settings["threads"]),
                        gpu_layers=int(self.settings["gpu_layers"]),
                    ))
                    server.start()
                    self.server = server
                except runtime.RuntimeError_ as exc:
                    self.state, self.error = "failed", str(exc)
                    return
            try:
                server.wait_until_ready()
            except runtime.RuntimeError_ as exc:
                self.state, self.error = "failed", str(exc)
                return
            os.environ["NIMBUS_LOCAL_BASE_URL"] = server.base_url
            self.state = "ready"

        threading.Thread(target=work, daemon=True, name="model-start").start()

    def stop_model(self) -> None:
        if self.server:
            self.server.stop()
            self.server = None
        if self.engine == "local":
            self.state = "missing" if not self.installed() else "loading"

    # -- downloads --------------------------------------------------------------

    def start_download(self, key: str) -> None:
        build = catalogue.build(key)
        with self._lock:
            if self.download and self.download.get("state") == "running":
                raise KeyError("a download is already running")
            self._cancel.clear()
            self.download = {"build": key, "state": "running", "done": 0,
                             "total": build.bytes, "speed": 0.0, "seconds_left": None, "error": ""}

        def progress(p: Progress) -> None:
            if self._cancel.is_set():
                raise Cancelled()
            self.download.update(done=p.done, total=p.total or build.bytes,
                                 speed=p.speed, seconds_left=p.seconds_left)

        def work() -> None:
            try:
                fetch(build.url, models_dir() / build.filename, expected_bytes=build.bytes,
                      sha256=build.sha256, on_progress=progress)
            except Cancelled:
                self.download.update(state="cancelled")
                return
            except DownloadError as exc:
                self.download.update(state="failed", error=str(exc))
                return
            self.download.update(state="done", done=build.bytes)
            # The first model a person installs is the one they meant to use.
            if self.state in ("missing", "failed") or self.current_build() is None:
                self.settings["build"] = key
                self._save_settings()
                self.start_model()

        threading.Thread(target=work, daemon=True, name="download").start()

    def cancel_download(self) -> None:
        self._cancel.set()

    def use(self, key: str) -> None:
        build = catalogue.build(key)
        if not self._complete(build):
            raise RuntimeError(f"{build.key} is not downloaded")
        self.settings["build"] = key
        self._save_settings()
        self.start_model()

    def delete(self, key: str) -> None:
        build = catalogue.build(key)
        running = self.server and self.current_build() and self.current_build().key == key
        if running and self.state in ("ready", "loading"):
            raise RuntimeError("this is the model in use; switch to another before deleting it")
        for path in (models_dir() / build.filename, models_dir() / (build.filename + ".part")):
            path.unlink(missing_ok=True)

    def open_folder(self, which: str) -> None:
        folder = {"models": models_dir(), "files": self.store.blobs, "data": data_dir()}.get(which)
        if folder is None:
            return
        folder.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(folder)  # noqa: S606 - a folder the app itself created
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])

    # -- what the setup page shows ---------------------------------------------------

    def status(self) -> dict:
        facts = runtime.machine()
        builds = []
        for build in catalogue.BUILDS:
            path = models_dir() / build.filename
            partial = path.with_name(path.name + ".part")
            builds.append({
                "key": build.key, "gigabytes": round(build.gigabytes, 2),
                "ram_hint_gb": build.ram_hint_gb, "label_en": build.label_en, "label_fa": build.label_fa,
                "installed": self._complete(build),
                "partial_bytes": partial.stat().st_size if partial.is_file() else 0,
                "bytes": build.bytes,
            })
        current = self.current_build()
        try:
            runtime_path = str(runtime.find_binary())
        except runtime.RuntimeError_:
            runtime_path = ""
        return {
            "engine": self.engine,
            "state": self.state if self.engine == "local" else "ready",
            "error": self.error,
            "current": current.key if current else None,
            "chosen": self.settings["build"],
            "builds": builds,
            "adapter": catalogue.adapter_path().is_file(),
            "runtime": runtime_path,
            "download": self.download,
            "machine": facts,
            "paths": {"models": str(models_dir()), "data": str(data_dir())},
            "log": self.server.tail(6) if self.server else "",
        }


# -- start ------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_for(url: str, seconds: float = 20.0) -> None:
    import urllib.request

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except Exception:
            time.sleep(0.1)
    raise SystemExit(f"the local server did not come up at {url}")


def run(*, engine: str = "local", browser: bool = False, port: int = 0) -> int:
    import uvicorn

    from .server import build

    controller = Controller(engine)
    port = port or _free_port()
    controller.self_url = f"http://127.0.0.1:{port}"
    web = build(controller)

    config = uvicorn.Config(web, host="127.0.0.1", port=port, log_level="warning",
                            timeout_keep_alive=30)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="web")
    thread.start()
    _wait_for(controller.self_url + "/local/status")

    controller.start_model()
    print(f"AhoosAI Studio (offline) at {controller.self_url}", flush=True)

    def shutdown() -> None:
        controller.stop_model()
        server.should_exit = True

    use_window = not browser
    if use_window:
        try:
            import webview  # pywebview
        except ImportError:
            use_window = False

    try:
        if use_window:
            from .paths import assets_dir

            # .ico on Windows, and not as a preference. pywebview's WinForms
            # backend hands the file to System.Drawing.Icon, which rejects a PNG
            # with a .NET exception -- raised on the GUI thread, outside Python,
            # so no except clause here sees it and the whole process exits
            # without a word. The first packaged build did exactly that.
            icon = assets_dir() / ("icon.ico" if os.name == "nt" else "icon.png")
            webview.create_window(
                "AhoosAI Studio", controller.self_url + "/",
                width=1280, height=840, min_size=(760, 560), text_select=True)
            try:
                webview.start(icon=str(icon) if icon.is_file() else None,
                              private_mode=False,
                              storage_path=str(data_dir() / "webview"))
            except Exception as exc:  # noqa: BLE001
                # pywebview imports fine and then finds no GUI backend -- a Linux
                # machine without WebKitGTK, most often. The studio is a web
                # page; the default browser is a perfectly good window for it.
                print(f"no native window ({exc}); opening the browser instead", flush=True)
                webbrowser.open(controller.self_url + "/")
                while thread.is_alive():
                    time.sleep(0.5)
        else:
            webbrowser.open(controller.self_url + "/")
            print("Press Ctrl+C to quit.", flush=True)
            while thread.is_alive():
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()
    return 0
