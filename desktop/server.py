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

import uuid

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool
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
        return RedirectResponse("/index.html" if controller.any_installed() else "/setup.html")

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

    def refuse(detail: str) -> StreamingResponse:
        body = _frame("error", {"detail": detail}) + _frame("closed", {})
        return StreamingResponse(iter([body]), media_type="text/event-stream")

    @app.post("/local/turn")
    async def prepare_turn(request: Request) -> dict:
        """Hand over a message and the turns before it, and get an id back.

        EventSource can only GET, and a conversation does not fit in a query
        string. So the turn is posted, kept for a moment, and collected by the
        stream that follows it. Nothing is stored on disk: this is a relay, and
        the chat itself is already saved by the page.
        """
        body = await request.json()
        prompt = str((body or {}).get("prompt", "")).strip()
        if not prompt:
            raise HTTPException(400, "Empty prompt.")
        if len(prompt) > MAX_PROMPT:
            raise HTTPException(413, "Prompt is too long (limit 8000 characters).")
        history = []
        for turn in (body.get("history") or [])[-40:]:
            role = str(turn.get("role", ""))
            content = str(turn.get("content", ""))
            if role in ("user", "assistant") and content.strip():
                history.append({"role": role, "content": content[:MAX_PROMPT]})
        token = uuid.uuid4().hex
        # Only a handful are ever in flight; the oldest go when there are too many.
        while len(controller.turns) > 8:
            controller.turns.pop(next(iter(controller.turns)), None)
        controller.turns[token] = {"prompt": prompt, "history": history}
        return {"turn": token}

    @app.get("/stream.php")
    def stream(prompt: str = "", effort: str = "", solo: str = "", turn: str = "") -> StreamingResponse:
        prepared = controller.turns.pop(turn, None) if turn else None
        if prepared:
            prompt = prepared["prompt"]
        prompt = prompt.strip()
        history = prepared["history"] if prepared else []

        if not prompt:
            return refuse("Empty prompt.")
        if len(prompt) > MAX_PROMPT:
            return refuse("Prompt is too long (limit 8000 characters).")
        if not controller.ready():
            return refuse(controller.not_ready_reason())

        if controller.model.engine == "apex":
            return _apex_stream(controller, prompt, history)

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

    @app.post("/local/setup/model")
    def use_model(model: str) -> dict:
        try:
            controller.use_model(model)
        except (KeyError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return controller.status()

    @app.post("/local/open-folder")
    def open_folder(which: str = "models") -> dict:
        controller.open_folder(which)
        return {"ok": True}

    # -- the controls, the folder, and the right to act in it ----------------------

    @app.post("/local/controls")
    async def controls(request: Request) -> dict:
        body = await request.json()
        body = body if isinstance(body, dict) else {}
        try:
            controller.set_dials(
                level=body.get("level"), temperature=body.get("temperature"),
                memory=body.get("memory"), mode=body.get("mode"),
                permission=body.get("permission"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return controller.status()["controls"]

    @app.post("/local/pick-folder")
    def pick_folder() -> dict:
        try:
            chosen = controller.pick_folder()
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"folder": chosen}

    @app.post("/local/set-folder")
    async def set_folder(request: Request) -> dict:
        body = await request.json()
        try:
            controller.set_folder(str((body or {}).get("folder", "")))
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"folder": str(controller.folder() or "")}

    @app.get("/local/folder/list")
    def list_folder(path: str = "") -> dict:
        """What is in the working folder, so the page can show where it is working."""
        try:
            target = controller.inside(path)
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not target.is_dir():
            raise HTTPException(404, "no such folder")
        entries = []
        for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))[:400]:
            entries.append({"name": item.name, "dir": item.is_dir(),
                            "bytes": item.stat().st_size if item.is_file() else 0})
        return {"path": path, "entries": entries}

    @app.post("/local/allow")
    async def allow(request: Request) -> dict:
        """Record that the person said yes, and for how long.

        The page asks the question; this only remembers the answer. `remember`
        is honoured when the mode is "session" and ignored when it is "ask",
        which is why the mode has no third setting.
        """
        body = await request.json()
        body = body if isinstance(body, dict) else {}
        key = str(body.get("key", "")).strip()
        if not key:
            raise HTTPException(400, "nothing to allow")
        remember = bool(body.get("remember")) and controller.settings.get("permission") == "session"
        controller.allow(key, remember=remember)
        return {"allowed": key, "remembered": remember}

    @app.post("/local/open-terminal")
    def open_terminal() -> dict:
        try:
            controller.open_terminal()
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @app.websocket("/local/shell")
    async def shell(socket: WebSocket) -> None:
        """One command at a time, in the working folder, after it was allowed.

        The socket carries {command, allowed} in and {line|code|error|cut} out.
        `allowed` is the page saying the person agreed; without it nothing runs,
        and without a working folder there is nowhere to run it.
        """
        from .terminal import Shell

        await socket.accept()
        folder = controller.folder()
        if folder is None:
            await socket.send_json({"error": "Choose a working folder first."})
            await socket.close()
            return
        session = Shell(folder)
        await socket.send_json({"folder": str(folder)})
        try:
            while True:
                message = await socket.receive_json()
                command = str(message.get("command", "")).strip()
                if not command:
                    continue
                if message.get("stop"):
                    session.stop()
                    continue
                key = f"shell:{command}"
                if not message.get("allowed") and controller.needs_asking(key):
                    await socket.send_json({"ask": command, "key": key})
                    continue
                controller.allow(key, remember=bool(message.get("remember")))
                await socket.send_json({"started": command})
                queue = session.run(command)
                while True:
                    item = await run_in_threadpool(queue.get)
                    if item is None:
                        break
                    await socket.send_json(item)
        except WebSocketDisconnect:
            session.stop()
        except Exception as exc:  # noqa: BLE001 - the socket is the person's only view
            try:
                await socket.send_json({"error": str(exc)})
            except Exception:  # noqa: BLE001
                pass
            session.stop()

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


def _apex_stream(controller: "Controller", prompt: str, history: list[dict]) -> StreamingResponse:
    """Nimbus 2 Apex, straight to llama.cpp, in the event names the page knows.

    `plan:done` carries the level, the temperature and whether the ceiling
    closed the reasoning, so the existing stage in the page tells the truth
    about the dials instead of about a manager that is not here. `apex:think`
    and `apex:delta` are extra; app.js ignores events it did not register, and
    desktop.js listens for them.
    """
    from .apex import Apex, ApexError, Turn
    from nimbus2.controls import Controls

    if controller.server is None:
        return StreamingResponse(iter([_frame("error", {"detail": "The model is not running."})
                                       + _frame("closed", {})]), media_type="text/event-stream")

    settings = controller.settings
    keep = int(settings.get("memory", 0))
    turns = [Turn(t["role"], t["content"]) for t in history][-(keep * 2):] if keep else []
    controls = Controls(thinking_level=int(settings.get("level", 5)),
                        temperature=int(settings.get("temperature", 5)))
    engine = Apex(controller.server.base_url)

    def frames() -> Iterator[bytes]:
        answer: dict = {}
        try:
            for event, payload in engine.generate(prompt, controls, turns):
                if event == "think":
                    yield _frame("apex:think", payload).encode()
                elif event == "closed":
                    yield _frame("plan:done", {
                        "handle_directly": True,
                        "rationale": (f"level {controls.thinking_level}/20, temperature "
                                      f"{controls.temperature}/10 — {payload['words']} words of "
                                      f"reasoning against a target of {payload['target']}"
                                      + (", closed at the ceiling" if payload["forced"] else "")),
                        "delegations": [],
                    }).encode()
                    yield _frame("apex:closed", payload).encode()
                elif event == "answer_delta":
                    yield _frame("apex:delta", payload).encode()
                else:
                    answer = payload
        except ApexError as error:
            yield _frame("error", {"detail": str(error)}).encode()
            yield _frame("closed", {}).encode()
            return
        except Exception as error:  # noqa: BLE001 - reported to the person, not swallowed
            yield _frame("error", {"detail": f"The local model did not answer: {error}"}).encode()
            yield _frame("closed", {}).encode()
            return
        if not answer.get("answer"):
            yield _frame("error", {"detail": "The local model returned nothing."}).encode()
        else:
            yield _frame("done", {"answer": answer["answer"], "apex": answer}).encode()
        yield _frame("closed", {}).encode()

    return StreamingResponse(frames(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


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
