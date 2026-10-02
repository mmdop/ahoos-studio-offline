"""The offline studio's local web server.

One process, bound to 127.0.0.1, serving the studio page (desktop/ui) and the
endpoints behind it:

    /local/status, /local/settings, /local/select    the model and its settings
    /local/chat, /local/stop, /local/answer           a turn, streamed; its gate
    /local/battle, /local/battles                     base against adapter
    /local/hub/*, /local/jobs/*, /local/download      models from the Hub, the queue
    /local/folder/*, /local/shell, /local/open-*      the working folder and the system

WHY THE STREAMS ARE POSTED

A turn carries the message, its attachments and which tools it may use -- more
than a query string should. The page reads the answer as server-sent events
from a POST, with fetch(), and stopping is a separate POST that sets the turn's
cancel flag, so a stop is heard even while a tool is running.

A stream whose reader goes away (the window reloaded) stops its turn too; what
was written so far is kept, marked as stopped.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from pathlib import Path
from queue import Empty, Queue
from typing import TYPE_CHECKING, Callable

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

if TYPE_CHECKING:
    from .app import Controller

UI = Path(__file__).resolve().parent / "ui"
MAX_PROMPT = 32000
MAX_ATTACHMENT = 60000


def _frame(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def stream(work: Callable[[Callable[[str, dict], None], threading.Event], None],
           cancel: threading.Event) -> StreamingResponse:
    """Run `work(emit, cancel)` on a thread and relay what it emits as SSE."""
    queue: Queue = Queue()

    def emit(event: str, payload: dict) -> None:
        queue.put((event, payload))

    def worker() -> None:
        try:
            work(emit, cancel)
        except Exception as error:  # noqa: BLE001 - the person sees it, instead of a stream that stops
            emit("error", {"detail": str(error)})
        finally:
            queue.put(None)

    threading.Thread(target=worker, daemon=True, name="turn").start()

    async def frames():
        try:
            while True:
                try:
                    item = await asyncio.to_thread(queue.get, True, 1.0)
                except Empty:
                    yield ": keep-alive\n\n"
                    continue
                if item is None:
                    break
                yield _frame(*item)
        finally:
            cancel.set()

    return StreamingResponse(frames(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        body = {}
    return body if isinstance(body, dict) else {}


def build(controller: "Controller") -> FastAPI:
    app = FastAPI(title="AhoosAI Studio (offline)", docs_url=None, redoc_url=None, openapi_url=None)
    ui_root = Path(getattr(__import__("sys"), "_MEIPASS", "")) / "ui"
    ui = ui_root if ui_root.is_dir() else UI

    def bad(error: Exception, code: int = 400) -> HTTPException:
        return HTTPException(code, str(error))

    # -- pages -----------------------------------------------------------------

    @app.get("/")
    def home() -> RedirectResponse:
        return RedirectResponse("/studio.html")

    # -- the model and its settings -----------------------------------------------

    @app.get("/local/status")
    def status() -> dict:
        return controller.status()

    @app.post("/local/settings")
    async def settings(request: Request) -> dict:
        patch = await _body(request)
        try:
            restart = controller.update_settings(patch)
        except (TypeError, ValueError) as error:
            raise bad(error) from error
        if restart:
            controller.start_model()
        return controller.status()

    @app.post("/local/select")
    async def select(request: Request) -> dict:
        body = await _body(request)
        try:
            controller.select(str(body.get("base", "")), str(body.get("adapter", "")))
        except RuntimeError as error:
            raise bad(error) from error
        return controller.status()

    @app.post("/local/restart")
    def restart() -> dict:
        controller.start_model()
        return controller.status()

    # -- conversations ----------------------------------------------------------------

    @app.get("/local/chats")
    def chats() -> list:
        return controller.chats.listing()

    @app.get("/local/chats/{chat_id}")
    def chat(chat_id: str) -> dict:
        try:
            return controller.chats.get(chat_id)
        except KeyError as error:
            raise bad(error, 404) from error

    @app.post("/local/chats/{chat_id}")
    async def edit_chat(chat_id: str, request: Request) -> dict:
        body = await _body(request)
        fields = {}
        if isinstance(body.get("title"), str):
            fields["title"] = body["title"].strip()[:120]
        if "pinned" in body:
            fields["pinned"] = bool(body["pinned"])
        try:
            controller.chats.update(chat_id, **fields)
        except KeyError as error:
            raise bad(error, 404) from error
        return {"ok": True}

    @app.delete("/local/chats/{chat_id}")
    def delete_chat(chat_id: str) -> dict:
        try:
            controller.chats.delete(chat_id)
        except KeyError as error:
            raise bad(error, 404) from error
        return {"ok": True}

    @app.post("/local/chat")
    async def turn(request: Request) -> StreamingResponse:
        from .agent import Turn

        body = await _body(request)
        prompt = str(body.get("prompt", "")).strip()
        retry = bool(body.get("retry"))
        resume_asked = bool(body.get("resume"))
        if resume_asked and not prompt:
            prompt = "Continue." if controller.settings.get("language") != "fa" else "ادامه بده."
        if not prompt and not retry:
            raise HTTPException(400, "Empty message.")
        if len(prompt) > MAX_PROMPT:
            raise HTTPException(413, "The message is too long.")
        attachments = []
        for item in (body.get("attachments") or [])[:6]:
            text = str(item.get("text", ""))
            if text:
                attachments.append({"name": str(item.get("name", "file"))[:120], "text": text[:MAX_ATTACHMENT]})
        chat_id = str(body.get("chat_id") or "")
        try:
            chat = controller.chats.get(chat_id) if chat_id else controller.chats.create()
        except KeyError:
            chat = controller.chats.create()
        if not controller.ready():
            raise HTTPException(409, controller.not_ready_reason())

        run_id = uuid.uuid4().hex[:12]
        cancel = threading.Event()
        controller.runs[run_id] = cancel
        plan = bool(body.get("plan"))

        def work(emit, cancel_flag) -> None:
            try:
                history = list(chat["messages"])
                user = None
                if retry:
                    # "Answer again": the last answer goes, the message before it stays.
                    if history and history[-1].get("role") == "assistant":
                        controller.chats.replace_last(chat["id"], "assistant")
                        history.pop()
                    if not history or history[-1].get("role") != "user":
                        emit("error", {"detail": "There is no message to answer again."})
                        return
                    user = history.pop()
                llama = controller.llama()
                web = controller.web() if bool(body.get("web", controller.settings.get("web_on"))) else None
                # A plan the last turn left unfinished, when "continue" was pressed.
                resume = None
                if resume_asked:
                    last = next((m for m in reversed(history) if m.get("role") == "assistant"), None)
                    if last and isinstance(last.get("plan"), dict):
                        resume = last["plan"]
                folder = controller.folder_for(chat)
                runner = Turn(
                    llama=llama, profile=controller.profile(), settings=controller.settings,
                    folder=folder, inside=None, web=web, history=history,
                    request=user["content"] if user else prompt, plan=bool(user.get("plan")) if user else plan,
                    emit=emit, cancel=cancel_flag,
                    ask=lambda key, payload: controller.ask(emit, cancel_flag, key, payload),
                    needs_asking=controller.needs_asking, chips=controller.chips_text(),
                    first=not history, workspace=controller.workspace(),
                    on_folder=lambda path: controller.chats.update(chat["id"], folder=str(path)),
                    resume=resume, procs=controller.procs)
                if user is None:
                    sent = runner.sent_text(attachments)
                    user = controller.chats.append(chat["id"], {
                        "role": "user", "content": prompt, "sent": sent, "plan": plan,
                        "attachments": [{"name": a["name"], "chars": len(a["text"])} for a in attachments]})
                else:
                    sent = user.get("sent") or user["content"]
                emit("run", {"run_id": run_id, "chat_id": chat["id"], "user": user,
                             "title": controller.chats.get(chat["id"]).get("title", ""),
                             "profile": runner.profile.name, "folder": str(folder or ""),
                             "tools": [t.name for t in runner.tools]})
                message = runner.run(sent)
                saved = controller.chats.append(chat["id"], message)
                emit("done", {"message": saved})
            finally:
                controller.runs.pop(run_id, None)
                try:
                    from .checkpoints import prune

                    prune()
                except OSError:
                    pass

        return stream(work, cancel)

    @app.post("/local/undo")
    async def undo(request: Request) -> dict:
        """Put back every file one turn changed, and mark the turn as undone."""
        from . import checkpoints

        body = await _body(request)
        chat_id, message_id = str(body.get("chat_id", "")), str(body.get("message_id", ""))
        try:
            chat = controller.chats.get(chat_id)
            message = next(m for m in chat["messages"] if m.get("id") == message_id)
            if not message.get("checkpoint"):
                raise KeyError("that answer changed no files")
            restored = await run_in_threadpool(checkpoints.undo, message["checkpoint"])
            controller.chats.update_message(chat_id, message_id, undone=True)
        except (KeyError, StopIteration) as error:
            raise bad(error if isinstance(error, KeyError) else KeyError("no such answer"), 404) from error
        return {"restored": restored}

    @app.get("/local/procs")
    def procs() -> list:
        return controller.procs.public()

    @app.post("/local/procs/{proc_id}/stop")
    def stop_proc(proc_id: str) -> dict:
        return {"ok": controller.procs.stop(proc_id), "procs": controller.procs.public()}

    @app.post("/local/open-project")
    async def open_project(request: Request) -> dict:
        """A project folder in the system's file manager -- a folder only, never a file to run."""
        import os
        import subprocess
        import sys

        body = await _body(request)
        folder = Path(str(body.get("folder", ""))).expanduser()
        if not str(body.get("folder", "")).strip() or not folder.is_dir():
            raise HTTPException(404, "no such folder")
        if os.name == "nt":
            os.startfile(folder)  # noqa: S606 - a directory, checked above
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
        return {"ok": True}

    @app.post("/local/open-file")
    async def open_file(request: Request) -> dict:
        """A page or picture from a chat's project folder, in the person's browser."""
        from . import tools as T

        body = await _body(request)
        chat = None
        if body.get("chat_id"):
            try:
                chat = controller.chats.get(str(body["chat_id"]))
            except KeyError:
                chat = None
        root = Path(str(body["folder"])) if body.get("folder") else controller.folder_for(chat)
        if root is None or not root.is_dir():
            raise HTTPException(400, "there is no project folder for that")
        from .app import inside

        try:
            target = inside(root, str(body.get("path", "")))
        except RuntimeError as error:
            raise bad(error) from error
        if not target.is_file():
            raise HTTPException(404, "no such file")
        if target.suffix.lower() not in T.OPENABLE:
            raise HTTPException(400, "only pages, pictures and documents open this way")
        import webbrowser

        webbrowser.open(target.resolve().as_uri())
        return {"ok": True, "path": str(target)}

    @app.post("/local/stop")
    async def stop(request: Request) -> dict:
        body = await _body(request)
        run_id = str(body.get("run_id", ""))
        targets = [controller.runs.get(run_id)] if run_id else list(controller.runs.values())
        for cancel in targets:
            if cancel:
                cancel.set()
        return {"ok": True}

    @app.post("/local/answer")
    async def answer(request: Request) -> dict:
        body = await _body(request)
        try:
            controller.answer(str(body.get("ask_id", "")), str(body.get("answer", "deny")))
        except KeyError as error:
            raise bad(error, 404) from error
        return {"ok": True}

    # -- battle --------------------------------------------------------------------

    @app.get("/local/battles")
    def battles() -> dict:
        return {"battles": controller.battles.listing(), "score": controller.battles.score()}

    @app.post("/local/battle")
    async def battle(request: Request) -> StreamingResponse:
        from . import battle as arena

        body = await _body(request)
        prompt = str(body.get("prompt", "")).strip()
        if not prompt:
            raise HTTPException(400, "Empty message.")
        if not controller.ready():
            raise HTTPException(409, controller.not_ready_reason())
        profile = controller.profile()
        if not profile.has_adapter:
            raise HTTPException(409, "A battle needs a model with an adapter: choose one in the model menu.")
        preferences = controller.settings["battle"]
        mode = body.get("mode") if body.get("mode") in ("parallel", "sequential") else preferences["mode"]
        blind = bool(body.get("blind", preferences["blind"]))
        strength = max(0.1, min(2.0, float(body.get("strength", preferences["strength"]))))
        run_id = uuid.uuid4().hex[:12]
        cancel = threading.Event()
        controller.runs[run_id] = cancel

        def work(emit, cancel_flag) -> None:
            try:
                emit("run", {"run_id": run_id})
                result = arena.run(llama=controller.llama(), profile=profile, settings=controller.settings,
                                   prompt=prompt, mode=mode, blind=blind, strength=strength, emit=emit,
                                   cancel=cancel_flag)
                controller.battles.save(result)
                emit("done", {"battle": result, "score": controller.battles.score()})
            finally:
                controller.runs.pop(run_id, None)

        return stream(work, cancel)

    @app.post("/local/battles/{battle_id}/vote")
    async def vote(battle_id: str, request: Request) -> dict:
        body = await _body(request)
        try:
            result = controller.battles.vote(battle_id, str(body.get("vote", "")))
        except (KeyError, ValueError) as error:
            raise bad(error) from error
        return {"battle": result, "score": controller.battles.score()}

    @app.delete("/local/battles/{battle_id}")
    def delete_battle(battle_id: str) -> dict:
        controller.battles.delete(battle_id)
        return {"score": controller.battles.score()}

    @app.delete("/local/battles")
    def clear_battles() -> dict:
        controller.battles.delete(None)
        return {"score": controller.battles.score()}

    # -- models and downloads ---------------------------------------------------------

    @app.post("/local/download")
    def download(build: str) -> dict:
        try:
            controller.download_build(build)
        except KeyError as error:
            raise bad(error) from error
        return controller.status()

    @app.post("/local/hub/inspect")
    async def inspect(request: Request) -> dict:
        from .hub import HubError

        body = await _body(request)
        try:
            return await run_in_threadpool(controller.inspect, str(body.get("link", "")),
                                           "adapter" if body.get("kind") == "adapter" else "model")
        except HubError as error:
            raise bad(error) from error

    @app.post("/local/hub/add")
    async def add(request: Request) -> dict:
        body = await _body(request)
        try:
            controller.add_from_hub("adapter" if body.get("kind") == "adapter" else "model",
                                    str(body.get("repo", "")), str(body.get("choice", "")),
                                    str(body.get("name", "")).strip()[:80])
        except RuntimeError as error:
            raise bad(error) from error
        return controller.status()

    @app.post("/local/jobs/{job_id}/{action}")
    def job(job_id: str, action: str) -> dict:
        {"cancel": controller.jobs.cancel, "retry": controller.jobs.retry,
         "remove": controller.jobs.remove}.get(action, lambda _: None)(job_id)
        return controller.status()

    @app.post("/local/models/delete")
    async def delete_model(request: Request) -> dict:
        body = await _body(request)
        try:
            controller.delete_model(str(body.get("key", "")))
        except (KeyError, RuntimeError) as error:
            raise bad(error) from error
        return controller.status()

    # -- skills ------------------------------------------------------------------------

    @app.get("/local/chips")
    def chips() -> list:
        return [{k: v for k, v in c.items() if k != "content"} | {"chars": len(c.get("content", ""))}
                for c in controller.chips()]

    @app.post("/local/chips")
    async def add_chip(request: Request) -> dict:
        body = await _body(request)
        try:
            return controller.add_chip(str(body.get("name", "")), str(body.get("content", "")))
        except RuntimeError as error:
            raise bad(error) from error

    @app.delete("/local/chips/{chip_id}")
    def delete_chip(chip_id: str) -> dict:
        controller.delete_chip(chip_id)
        return {"ok": True}

    # -- the internet -------------------------------------------------------------------

    @app.post("/local/internet/test")
    async def test_internet() -> dict:
        from .web import Web, WebError

        internet = controller.settings.get("internet") or {}
        probe = Web({**internet, "mode": internet.get("mode") if internet.get("mode") != "off" else "direct"})
        try:
            return await run_in_threadpool(probe.test)
        except WebError as error:
            return {"ok": False, "error": str(error), "via": probe.label()}

    # -- the working folder, and the system ----------------------------------------------

    @app.post("/local/pick-folder")
    def pick_folder() -> dict:
        try:
            chosen = controller.pick_folder()
        except RuntimeError as error:
            raise bad(error) from error
        return {"folder": chosen}

    @app.post("/local/set-folder")
    async def set_folder(request: Request) -> dict:
        body = await _body(request)
        try:
            controller.set_folder(str(body.get("folder", "")))
        except RuntimeError as error:
            raise bad(error) from error
        # The folder chosen in a conversation is that conversation's project from then on.
        if body.get("chat_id"):
            try:
                controller.chats.update(str(body["chat_id"]), folder=str(controller.folder() or ""))
            except KeyError:
                pass
        return {"folder": str(controller.folder() or "")}

    @app.get("/local/folder/list")
    def list_folder(path: str = "") -> dict:
        try:
            target = controller.inside(path)
        except RuntimeError as error:
            raise bad(error) from error
        if not target.is_dir():
            raise HTTPException(404, "no such folder")
        entries = []
        for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))[:400]:
            try:
                size = item.stat().st_size if item.is_file() else 0
            except OSError:
                size = 0
            entries.append({"name": item.name, "dir": item.is_dir(), "bytes": size})
        return {"path": path, "entries": entries}

    @app.post("/local/folder/save")
    async def save_in_folder(request: Request) -> dict:
        """A code block saved into the working folder by the person's own click."""
        body = await _body(request)
        try:
            target = controller.inside(str(body.get("path", "")).strip())
        except RuntimeError as error:
            raise bad(error) from error
        if target.is_dir():
            raise HTTPException(400, "that is a folder")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(body.get("content", "")), encoding="utf-8", newline="")
        return {"path": str(target)}

    @app.post("/local/save-as")
    async def save_as(request: Request) -> dict:
        body = await _body(request)
        try:
            where = controller.save_as(str(body.get("name", "file.txt")), str(body.get("content", "")))
        except RuntimeError as error:
            raise bad(error) from error
        return {"path": where}

    @app.post("/local/open-folder")
    def open_folder(which: str = "models") -> dict:
        controller.open_folder(which)
        return {"ok": True}

    @app.post("/local/open-url")
    async def open_url(request: Request) -> dict:
        import webbrowser

        body = await _body(request)
        url = str(body.get("url", ""))
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, "only web addresses open in the browser")
        webbrowser.open(url)
        return {"ok": True}

    @app.post("/local/open-terminal")
    def open_terminal() -> dict:
        try:
            controller.open_terminal()
        except RuntimeError as error:
            raise bad(error) from error
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
                if message.get("stop"):
                    session.stop()
                    continue
                command = str(message.get("command", "")).strip()
                if not command:
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
        except Exception as error:  # noqa: BLE001 - the socket is the person's only view
            try:
                await socket.send_json({"error": str(error)})
            except Exception:  # noqa: BLE001
                pass
            session.stop()

    # Static files last: `/{name}.{ext}` would otherwise match the routes above.
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
    return FileResponse(path, headers={"Cache-Control": "no-store"})
