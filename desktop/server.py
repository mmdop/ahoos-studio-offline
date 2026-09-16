"""The offline studio's local web server.

One process, bound to 127.0.0.1, serving three things:

    /             the studio page (desktop/ui), built from the web studio
    /api          the Nimbus API itself, loaded with the offline family and
                  every model pointed at the local llama.cpp server
    the rest      what the web studio's PHP host did, done locally: the three
                  .php endpoints app.js calls, the store behind
                  local-supabase.js, and the model setup screen

WHY THE .php NAMES SURVIVE

app.js asks for `stream.php`, `chips.php` and `session.php`. Serving
those paths here means app.js is the web studio's file, not a fork of it. The
names are a little odd on a desktop; a second copy of nine hundred lines of
client would be worse.

WHY stream.php RELAYS OVER LOOPBACK

The streaming endpoint lives inside create_app's closure, with its worker thread,
its queue and its event format. Calling it through HTTP on the same machine runs
exactly the code the online studio runs, and costs a loopback round trip that
nobody waiting on a CPU-bound 7B model will ever notice.
"""

from __future__ import annotations

import json
import mimetypes
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from .store import Store, StoreError

if TYPE_CHECKING:
    from .app import Controller

UI = Path(__file__).resolve().parent / "ui"
MAX_PROMPT = 8000
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _frame(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def build(controller: "Controller") -> FastAPI:
    store: Store = controller.store
    app = FastAPI(title="AhoosAI Studio (offline)", docs_url=None, redoc_url=None, openapi_url=None)
    active_chips: list[dict] = []
    chips_lock = threading.Lock()

    ui_root = Path(getattr(__import__("sys"), "_MEIPASS", "")) / "ui"
    ui = ui_root if ui_root.is_dir() else UI

    # -- pages ---------------------------------------------------------------

    @app.get("/")
    def home() -> RedirectResponse:
        # Straight to the studio once a model is installed; the setup page
        # otherwise, since the studio with nothing to answer is a page that
        # fails on the first message.
        return RedirectResponse("/index.html" if controller.installed() else "/setup.html")

    # -- what the PHP host did ------------------------------------------------

    @app.post("/session.php")
    def session() -> dict:
        # There is no second party to hand a session to: the person at this
        # computer is the only one, and signed in from the first moment.
        return {"ok": True}

    @app.post("/chips.php")
    async def chips(request: Request) -> dict:
        body = await request.json()
        chosen = body.get("chips") if isinstance(body, dict) else None
        chosen = chosen if isinstance(chosen, list) else []
        if len(chosen) > 5:
            raise HTTPException(413, "At most 5 chips can be active at once.")
        clean = []
        for chip in chosen:
            name = str(chip.get("name", "")).strip()[:80]
            content = str(chip.get("content", "")).strip()
            if not name or not content:
                continue
            if len(content) > MAX_PROMPT:
                raise HTTPException(413, f'Chip "{name}" is longer than 8000 characters.')
            clean.append({"name": name, "content": content})
        with chips_lock:
            active_chips[:] = clean
        return {"ok": True, "active": len(clean)}

    @app.get("/stream.php")
    def stream(prompt: str = "", effort: str = "", solo: str = "") -> StreamingResponse:
        prompt = prompt.strip()

        def refuse(detail: str) -> StreamingResponse:
            body = _frame("error", {"detail": detail}) + _frame("closed", {})
            return StreamingResponse(iter([body]), media_type="text/event-stream")

        if not prompt:
            return refuse("Empty prompt.")
        if len(prompt) > MAX_PROMPT:
            return refuse("Prompt is too long (limit 8000 characters).")
        if not controller.ready():
            return refuse(controller.not_ready_reason())

        payload: dict = {"prompt": prompt, "solo": True}
        if effort.lower() in EFFORTS:
            payload["thinking"] = effort.lower()
        with chips_lock:
            if active_chips:
                payload["chips"] = list(active_chips)

        def relay() -> Iterator[bytes]:
            request = urllib.request.Request(
                controller.self_url + "/api/v1/ask/stream",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            )
            saw_event = False
            try:
                with urllib.request.urlopen(request, timeout=3600) as response:
                    for line in response:
                        if line.startswith(b"event:"):
                            saw_event = True
                        yield line
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                yield _frame("error", {"detail": f"The studio API answered {exc.code}: {detail}"}).encode()
                saw_event = True
            except Exception as exc:  # noqa: BLE001 - reported to the person, not swallowed
                yield _frame("error", {"detail": f"The local model did not answer: {exc}"}).encode()
                saw_event = True
            if not saw_event:
                yield _frame("error", {"detail": "The local model returned nothing."}).encode()
            yield _frame("closed", {}).encode()

        return StreamingResponse(relay(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    # -- the store behind local-supabase.js --------------------------------------

    @app.post("/local/db/{table}")
    async def db(table: str, request: Request) -> JSONResponse:
        spec = await request.json()
        return JSONResponse(store.query(table, spec if isinstance(spec, dict) else {}))

    @app.put("/local/storage/{bucket}/{path:path}")
    async def upload(bucket: str, path: str, request: Request) -> dict:
        if bucket != "files":
            raise HTTPException(404, "no such bucket")
        try:
            store.put(path, await request.body())
        except StoreError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"path": path}

    @app.get("/local/storage/{bucket}/{path:path}")
    def download(bucket: str, path: str) -> Response:
        if bucket != "files":
            raise HTTPException(404, "no such bucket")
        try:
            data = store.get(path)
        except StoreError as exc:
            raise HTTPException(404, str(exc)) from exc
        kind = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return Response(data, media_type=kind)

    # -- setup: the model, its download, and the machine ---------------------------

    @app.get("/local/status")
    def status() -> dict:
        return controller.status()

    @app.post("/local/setup/download")
    def start_download(build: str) -> dict:
        try:
            controller.start_download(build)
        except KeyError as exc:
            raise HTTPException(400, str(exc)) from exc
        return controller.status()

    @app.post("/local/setup/cancel")
    def cancel_download() -> dict:
        controller.cancel_download()
        return controller.status()

    @app.post("/local/setup/use")
    def use_build(build: str) -> dict:
        try:
            controller.use(build)
        except (KeyError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return controller.status()

    @app.post("/local/setup/delete")
    def delete_build(build: str) -> dict:
        try:
            controller.delete(build)
        except (KeyError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return controller.status()

    @app.post("/local/open-folder")
    def open_folder(which: str = "models") -> dict:
        controller.open_folder(which)
        return {"ok": True}

    # The studio API, then the static files, last of all. Routes match in the
    # order they are declared, and `/{name}.{ext}` matches `/stream.php` too:
    # declared first, it answered every message with a 404 from the file server.
    app.mount("/api", controller.api)

    @app.get("/vendor/{name}")
    def vendor(name: str) -> FileResponse:
        return _static(ui / "vendor", name)

    @app.get("/{name}.{ext}")
    def page(name: str, ext: str) -> FileResponse:
        return _static(ui, f"{name}.{ext}")

    return app


def _static(folder: Path, name: str) -> FileResponse:
    # Names come from the URL. Only a plain file directly inside the folder is
    # served -- no separators, no dot-dot -- so nothing outside ui/ is reachable.
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(404)
    path = folder / name
    if not path.is_file():
        raise HTTPException(404)
    headers = {"Cache-Control": "no-store"}
    return FileResponse(path, headers=headers)
