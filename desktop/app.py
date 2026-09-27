"""AhoosAI Studio, offline: the program a person double-clicks.

    python -m desktop run                 the app, in its own window
    python -m desktop run --browser       the same, in the default browser
    python -m desktop run --engine echo   the whole interface with no model --
                                          for working on the app, not for use

What happens on start:

  1. the local web server comes up on 127.0.0.1, on a free port
  2. if a model is installed, llama-server starts loading it in the background
  3. a window opens on the studio -- on its Models page, if there is no model
     yet, where the person picks one or pastes a Hugging Face link

The window never waits for the model. Loading five gigabytes takes up to a
minute on a slow disk, and a window that appears after a minute of nothing looks
like a program that did not start. The page says "loading" instead.

WHAT IS RUNNING

One base model -- a build from the catalogue or a GGUF imported from the Hub --
and at most one adapter over it: ours, or one imported. Nimbus 1.1 and Nimbus 2
Apex are those two things together, with the prompts and dials they were
trained under. Any other pairing is run as a plain chat model.
"""

from __future__ import annotations

import copy
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from . import __version__, catalogue, family, runtime
from .agent import Profile
from .battle import Battles
from .chats import Chats
from .engine import Echo, Llama
from .jobs import Jobs
from .library import Library, gguf_header
from .paths import data_dir, models_dir, settings_file
from .web import DEFAULT as WEB_DEFAULT
from .web import Web

GENERIC_SYSTEM = ("You are a helpful, precise assistant. You answer in the language you are asked in, and "
                  "you say plainly when you do not know something.")

DEFAULT_SETTINGS = {
    # What runs: a base file and at most one adapter. "build:<key>" is a
    # catalogue build, "lib:<id>" an import; "builtin:<model>" is one of ours.
    "active": {"base": "", "adapter": ""},
    "model": catalogue.DEFAULT_MODEL,          # 2.0's choice, read once to fill `active`
    "build": catalogue.DEFAULT_BUILD,
    "context": 8192,
    "threads": 0,
    "gpu_layers": 0,
    # Apex's two dials. Level 5 and temperature 5 are what the model was trained
    # and measured at. Other models read the temperature as a plain dial (engine.DIAL).
    "level": 5,
    "temperature": 5,
    "memory": 6,                # turns of the conversation sent back with each message
    # "ask" every time, or "session" to stop asking again for something already
    # allowed once. There is deliberately no "never".
    "permission": "ask",
    "folder": "",
    "mode": "normal",           # normal | plan
    "web_on": False,            # the globe in the composer: search in this message
    "internet": WEB_DEFAULT,
    "agent": {"files": True, "commands": True, "max_steps": 10, "command_timeout": 120},
    "hf_token": "",
    "chips_on": ["builtin-rtl-web"],
    "battle": {"mode": "parallel", "blind": False, "strength": 1.0},
    "language": "",             # "" follows the system
    "theme": "system",          # system | light | dark
    "motion": "full",           # full | reduced
}

BUILTIN_ARCH = {"nimbus-1.1-prime-ee": "qwen2", "nimbus-2-apex": "qwen3"}


class Cancelled(Exception):
    """Raised from the progress callback to stop a download where it is."""


def inside(root: Path, relative: str) -> Path:
    """A path within the working folder, or a refusal.

    Resolved first and checked afterwards: a name is text from somewhere else
    until it has been proved to land where it claims. `a/../../b` only shows
    what it is once the dots are gone, and on Windows a symbolic link or a short
    name resolves to somewhere else entirely.
    """
    root = root.resolve()
    target = (root / str(relative)).resolve()
    if target != root and root not in target.parents:
        raise RuntimeError(f"{relative} is outside the working folder")
    return target


def _merge(defaults: dict, stored: dict) -> dict:
    out = copy.deepcopy(defaults)
    for key, value in (stored or {}).items():
        if key not in defaults:
            continue
        if isinstance(defaults[key], dict) and isinstance(value, dict):
            out[key] = _merge(defaults[key], value) if key != "internet" else {**out[key], **value}
        else:
            out[key] = value
    return out


class Controller:
    """Everything the page asks about: the model, its downloads, its server."""

    def __init__(self, engine: str = "local") -> None:
        self.engine = engine
        self.settings = self._load_settings()
        self.state = "missing"            # missing | loading | ready | failed
        self.error = ""
        self.server: runtime.ModelServer | None = None
        self._lock = threading.RLock()
        self.self_url = ""
        self.allowed: set[str] = set()    # allowed for this session; forgotten on close
        self.runs: dict[str, threading.Event] = {}
        self.asks: dict[str, dict] = {}
        self.window = None                # pywebview's window, for native dialogs
        self.library = Library()
        self.chats = Chats(data_dir() / "chats", legacy=data_dir() / "studio")
        self.battles = Battles(data_dir() / "battles.json")
        self.jobs = Jobs(self._finish_job)
        self.inspected: dict[str, dict] = {}
        self._skills_root = family.root()
        self._fill_active()

    # -- settings ------------------------------------------------------------

    def _load_settings(self) -> dict:
        try:
            stored = json.loads(settings_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = {}
        return _merge(DEFAULT_SETTINGS, stored)

    def save_settings(self) -> None:
        path = settings_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.settings, indent=1, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def update_settings(self, patch: dict) -> bool:
        """Apply a patch from the page. Returns whether the model must restart."""
        restart = False
        numbers = {"level": (1, 20), "temperature": (1, 10), "memory": (0, 30), "context": (2048, 131072),
                   "threads": (0, 256), "gpu_layers": (0, 999)}
        for key, (low, high) in numbers.items():
            if key in patch:
                value = max(low, min(high, int(patch[key])))
                restart |= key in ("context", "threads", "gpu_layers") and value != self.settings[key]
                self.settings[key] = value
        choices = {"permission": ("ask", "session"), "mode": ("normal", "plan"),
                   "language": ("", "fa", "en"), "theme": ("system", "light", "dark"),
                   "motion": ("full", "reduced")}
        for key, allowed in choices.items():
            if patch.get(key) in allowed:
                self.settings[key] = patch[key]
                if key == "permission" and patch[key] == "ask":
                    self.allowed.clear()
        if "web_on" in patch:
            self.settings["web_on"] = bool(patch["web_on"])
        if isinstance(patch.get("hf_token"), str):
            self.settings["hf_token"] = patch["hf_token"].strip()
        if isinstance(patch.get("chips_on"), list):
            self.settings["chips_on"] = [str(c) for c in patch["chips_on"]][:5]
        if isinstance(patch.get("internet"), dict):
            internet = self.settings["internet"]
            given = patch["internet"]
            if given.get("mode") in ("off", "direct", "provider", "custom"):
                internet["mode"] = given["mode"]
            if given.get("provider") in ("tavily", "brave", "exa", "serper", "jina"):
                internet["provider"] = given["provider"]
            if isinstance(given.get("keys"), dict):
                internet["keys"] = {**internet.get("keys", {}),
                                    **{k: str(v).strip() for k, v in given["keys"].items()
                                       if k in ("tavily", "brave", "exa", "serper", "jina")}}
            if isinstance(given.get("custom"), dict):
                internet["custom"] = {**internet.get("custom", {}),
                                      **{k: str(v) for k, v in given["custom"].items()
                                         if k in WEB_DEFAULT["custom"]}}
            if "proxy" in given:
                internet["proxy"] = str(given["proxy"] or "").strip()
            if "allow_local" in given:
                internet["allow_local"] = bool(given["allow_local"])
        if isinstance(patch.get("agent"), dict):
            agent = self.settings["agent"]
            for key in ("files", "commands"):
                if key in patch["agent"]:
                    agent[key] = bool(patch["agent"][key])
            if "max_steps" in patch["agent"]:
                agent["max_steps"] = max(1, min(30, int(patch["agent"]["max_steps"])))
            if "command_timeout" in patch["agent"]:
                agent["command_timeout"] = max(10, min(3600, int(patch["agent"]["command_timeout"])))
        if isinstance(patch.get("battle"), dict):
            battle = self.settings["battle"]
            if patch["battle"].get("mode") in ("parallel", "sequential"):
                battle["mode"] = patch["battle"]["mode"]
            if "blind" in patch["battle"]:
                battle["blind"] = bool(patch["battle"]["blind"])
            if "strength" in patch["battle"]:
                battle["strength"] = max(0.1, min(2.0, float(patch["battle"]["strength"])))
        self.save_settings()
        return restart

    # -- what is installed -------------------------------------------------------

    @staticmethod
    def _complete(build: catalogue.Build) -> bool:
        path = models_dir() / build.filename
        return path.is_file() and path.stat().st_size == build.bytes

    def bases(self) -> list[dict]:
        """Every base model on this computer, ours and imported."""
        found = []
        for build in catalogue.BUILDS:
            if self._complete(build):
                model = catalogue.model(build.model)
                found.append({"key": f"build:{build.key}", "name": model.base_repo.split("/")[-1].removesuffix("-GGUF"),
                              "quant": build.key.split("_", 1)[-1].upper() if build.key.startswith("apex_")
                              else build.key.upper(), "arch": BUILTIN_ARCH[build.model],
                              "bytes": build.bytes, "path": str(models_dir() / build.filename),
                              "family": build.model, "source": model.base_repo})
        for item in self.library.items("models"):
            found.append({"key": f"lib:{item['id']}", "name": item["name"], "quant": item.get("quant", ""),
                          "arch": item.get("architecture", ""), "bytes": item["bytes"], "path": item["file"],
                          "family": "", "source": item.get("repo", "")})
        return found

    def adapters(self) -> list[dict]:
        found = []
        for model in catalogue.MODELS:
            path = catalogue.adapter_path(model.id)
            if path.is_file():
                found.append({"key": f"builtin:{model.id}", "name": model.name, "arch": BUILTIN_ARCH[model.id],
                              "bytes": path.stat().st_size, "path": str(path), "builtin": True,
                              "summary_en": model.summary_en, "summary_fa": model.summary_fa})
        for item in self.library.items("adapters"):
            found.append({"key": f"lib:{item['id']}", "name": item["name"], "arch": item.get("architecture", ""),
                          "bytes": item["bytes"], "path": item["file"], "builtin": False,
                          "source": item.get("repo", "")})
        return found

    def _fill_active(self) -> None:
        """Make `active` name something installed, starting from 2.0's settings."""
        active = self.settings["active"]
        bases = {b["key"]: b for b in self.bases()}
        if active.get("base") not in bases:
            preferred = [b for b in bases.values() if b["family"] == self.settings.get("model")]
            chosen = next((b for b in preferred if b["key"] == f"build:{self.settings.get('build')}"),
                          (preferred or list(bases.values()) or [None])[0])
            if chosen is None:
                active.update(base="", adapter="")
                return
            active["base"] = chosen["key"]
            active["adapter"] = f"builtin:{chosen['family']}" if chosen["family"] else ""
        adapters = {a["key"]: a for a in self.adapters()}
        if active.get("adapter") and active["adapter"] not in adapters:
            active["adapter"] = ""
        self.save_settings()

    def active(self) -> tuple[dict | None, dict | None]:
        bases = {b["key"]: b for b in self.bases()}
        adapters = {a["key"]: a for a in self.adapters()}
        active = self.settings["active"]
        return bases.get(active.get("base", "")), adapters.get(active.get("adapter", ""))

    def profile(self) -> Profile:
        base, adapter = self.active()
        if self.engine != "local" and base is None:
            # The echo engine has no files but behaves as if a base and an
            # adapter were loaded, so every page -- battle too -- can be worked on.
            return Profile("Echo", "chat", GENERIC_SYSTEM, True, "Echo base", "Echo adapter")
        base_name = f"{base['name']} · {base['quant']}".strip(" ·") if base else ""
        if adapter and adapter["key"] == "builtin:nimbus-2-apex":
            return Profile("Nimbus 2 Apex", "apex", "", True, base_name, adapter["name"])
        if adapter and adapter["key"] == "builtin:nimbus-1.1-prime-ee":
            # The prompt the adapter was trained under, from the assembled family
            # root -- which a packaged build carries and a checkout builds.
            system = self._skills_root / "models" / "nimbus-1.1-prime-ee" / "system.md"
            text = system.read_text(encoding="utf-8").strip() if system.is_file() else GENERIC_SYSTEM
            return Profile("Nimbus 1.1 Prime-EE", "chat", text, True, base_name, adapter["name"])
        if adapter:
            return Profile(adapter["name"], "chat", GENERIC_SYSTEM, True, base_name, adapter["name"])
        return Profile(base["name"] if base else "a local model", "chat", GENERIC_SYSTEM, False, base_name, "")

    def select(self, base_key: str, adapter_key: str) -> None:
        bases = {b["key"]: b for b in self.bases()}
        adapters = {a["key"]: a for a in self.adapters()}
        if base_key not in bases:
            raise RuntimeError("that model is not on this computer")
        if adapter_key and adapter_key not in adapters:
            raise RuntimeError("that adapter is not on this computer")
        base, adapter = bases[base_key], adapters.get(adapter_key)
        if adapter and base["arch"] and adapter["arch"] and base["arch"] != adapter["arch"]:
            raise RuntimeError(f"{adapter['name']} was made for {adapter['arch']} models and "
                               f"{base['name']} is {base['arch']}; they cannot be combined")
        self.settings["active"] = {"base": base_key, "adapter": adapter_key}
        if base["family"]:
            self.settings["model"] = base["family"]
            self.settings["build"] = base_key.split(":", 1)[1]
        self.save_settings()
        self.start_model()

    def ready(self) -> bool:
        return self.engine != "local" or self.state == "ready"

    def not_ready_reason(self) -> str:
        if self.state == "loading":
            return "The model is still loading. It takes up to a minute after the app starts."
        if self.state == "failed":
            return f"The model could not start: {self.error}"
        return "No model is installed yet. Open Models to get one."

    def llama(self) -> Llama:
        if self.engine != "local":
            return Echo()
        if self.server is None:
            raise RuntimeError(self.not_ready_reason())
        _, adapter = self.active()
        return Llama(self.server.base_url, has_adapter=adapter is not None)

    # -- the model server -------------------------------------------------------

    def start_model(self) -> None:
        if self.engine != "local":
            self.state = "ready"
            return
        base, adapter = self.active()
        if base is None:
            self.stop_model()
            self.state = "missing"
            return

        def work() -> None:
            with self._lock:
                self.stop_model()
                self.state, self.error = "loading", ""
                try:
                    server = runtime.ModelServer(runtime.Settings(
                        model=Path(base["path"]),
                        adapter=Path(adapter["path"]) if adapter else None,
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
                if self.server is server:
                    self.state, self.error = "failed", str(exc)
                return
            if self.server is server:
                self.state = "ready"

        threading.Thread(target=work, daemon=True, name="model-start").start()

    def stop_model(self) -> None:
        if self.server:
            self.server.stop()
            self.server = None
        if self.engine == "local":
            self.state = "missing"

    # -- downloads --------------------------------------------------------------

    def download_build(self, key: str) -> dict:
        build = catalogue.build(key)
        model = catalogue.model(build.model)
        return self.jobs.add(title=f"{model.name} · {key}", kind="build", folder=models_dir(),
                             files=[{"url": build.url, "path": build.filename, "bytes": build.bytes,
                                     "sha256": build.sha256}], extra={"build": key})

    def _token_headers(self) -> dict:
        token = str(self.settings.get("hf_token") or "")
        return {"Authorization": f"Bearer {token}"} if token else {}

    def inspect(self, link: str, kind: str) -> dict:
        from . import hub

        found = hub.inspect(link, "adapter" if kind == "adapter" else "model", self.settings.get("hf_token", ""))
        self.inspected[f"{kind}:{found['repo']}"] = found
        installed = {Path(b["path"]).name for b in self.bases()} | {Path(a["path"]).name for a in self.adapters()}
        for choice in found["choices"]:
            choice["installed"] = any(Path(f["path"]).name in installed for f in choice["files"][:1])
        return found

    def add_from_hub(self, kind: str, repo: str, choice_id: str, name: str = "") -> dict:
        from . import hub

        found = self.inspected.get(f"{kind}:{repo}")
        if found is None:
            raise RuntimeError("inspect the link again; the list it came from has expired")
        choice = next((c for c in found["choices"] if c["id"] == choice_id), None)
        if choice is None:
            raise RuntimeError("that file is not in the list for this link")
        folder = Library.model_folder(repo) if kind == "model" else Library.adapter_folder(repo)
        headers = self._token_headers()
        files = [{"url": hub.file_url(repo, found["revision"], f["path"]), "path": Path(f["path"]).name,
                  "bytes": f["bytes"], "sha256": f.get("sha256", ""), "headers": headers}
                 for f in choice["files"]]
        job_kind = "model" if kind == "model" else ("adapter-peft" if choice["format"] == "peft" else "adapter")
        title = name or (choice["name"] if kind == "model" else f"{repo.split('/')[-1]} ({choice['name']})")
        return self.jobs.add(title=title, kind=job_kind, folder=folder, files=files,
                             extra={"repo": repo, "name": name or choice["name"].removesuffix(".gguf"),
                                    "base_model": choice.get("base_model") or found.get("base_model", ""),
                                    "quant": choice.get("quant", "")})

    def _finish_job(self, job: dict) -> None:
        """Runs on the download thread when a job's files have all arrived."""
        folder = Path(job["folder"])
        extra = job.get("extra") or {}
        first = folder / job["files"][0]["path"]
        if job["kind"] == "build":
            if self.state in ("missing", "failed") or not self.settings["active"].get("base"):
                build = catalogue.build(extra["build"])
                self.settings["active"] = {"base": f"build:{build.key}", "adapter": f"builtin:{build.model}"}
                self.save_settings()
                self.start_model()
            return
        if job["kind"] == "model":
            header = gguf_header(first)
            kind = "adapters" if header["type"] == "adapter" else "models"
            entry = self.library.add(kind, name=extra.get("name") or first.stem, repo=extra.get("repo", ""),
                                     file=first, source="hub")
            if kind == "models" and (self.state in ("missing", "failed") or not self.settings["active"].get("base")):
                self.settings["active"] = {"base": f"lib:{entry['id']}", "adapter": ""}
                self.save_settings()
                self.start_model()
            return
        if job["kind"] == "adapter":
            header = gguf_header(first)
            kind = "adapters" if header["type"] == "adapter" else "models"
            self.library.add(kind, name=extra.get("name") or first.stem, repo=extra.get("repo", ""), file=first,
                             source="hub", extra={"base_model": extra.get("base_model", "")})
            return
        if job["kind"] == "adapter-peft":
            from . import hub
            from .lora import convert

            base_repo = extra.get("base_model") or ""
            if not base_repo:
                raise RuntimeError("the adapter does not say which base model it was made for")
            config = hub.base_config(base_repo, self.settings.get("hf_token", ""))
            out = folder / (Path(extra.get("repo", "adapter")).name + ".gguf")
            convert(folder / "adapter_config.json", folder / "adapter_model.safetensors", config, out)
            for name in ("adapter_model.safetensors",):
                (folder / name).unlink(missing_ok=True)
            self.library.add("adapters", name=extra.get("name") or out.stem, repo=extra.get("repo", ""), file=out,
                             source="hub (converted from PEFT)", extra={"base_model": base_repo})

    def delete_model(self, key: str) -> None:
        base, adapter = self.active()
        if (base and base["key"] == key) or (adapter and adapter["key"] == key):
            if self.state in ("ready", "loading"):
                raise RuntimeError("this is in use; switch to another model before deleting it")
        kind, _, ident = key.partition(":")
        if kind == "build":
            build = catalogue.build(ident)
            for path in (models_dir() / build.filename, models_dir() / (build.filename + ".part")):
                path.unlink(missing_ok=True)
        elif kind == "lib":
            if not self.library.remove("models", ident):
                self.library.remove("adapters", ident)
        else:
            raise RuntimeError("the adapters that come with the app cannot be deleted")
        self._fill_active()

    # -- skills --------------------------------------------------------------------

    def chips(self) -> list[dict]:
        """Built-in chips from the skills folder, and the person's own."""
        import tomllib

        found = []
        for manifest in sorted((self._skills_root / "skills").glob("*/chip.toml")):
            try:
                spec = tomllib.loads(manifest.read_text(encoding="utf-8"))
                files = spec.get("apply", {}).get("files", [])
                content = "\n\n".join((manifest.parent / f).read_text(encoding="utf-8") for f in files)
                found.append({"id": f"builtin-{spec['chip']['id']}", "name": spec["chip"].get("name"),
                              "summary": spec["chip"].get("summary", ""), "builtin": True, "content": content})
            except (OSError, KeyError, ValueError):
                continue
        for chip in self._own_chips():
            found.append({**chip, "builtin": False})
        on = set(self.settings.get("chips_on") or [])
        for chip in found:
            chip["on"] = chip["id"] in on
        return found

    def _chips_path(self) -> Path:
        return data_dir() / "chips.json"

    def _own_chips(self) -> list[dict]:
        try:
            return json.loads(self._chips_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def add_chip(self, name: str, content: str) -> dict:
        name, content = name.strip()[:80], content.strip()
        if not name or not content:
            raise RuntimeError("a skill needs a name and some text")
        if len(content) > 8000:
            raise RuntimeError("a skill can be at most 8000 characters")
        chip = {"id": uuid.uuid4().hex[:10], "name": name, "summary": content[:120], "content": content}
        chips = self._own_chips() + [chip]
        self._chips_path().write_text(json.dumps(chips, ensure_ascii=False, indent=1), encoding="utf-8")
        return chip

    def delete_chip(self, chip_id: str) -> None:
        chips = [c for c in self._own_chips() if c["id"] != chip_id]
        self._chips_path().write_text(json.dumps(chips, ensure_ascii=False, indent=1), encoding="utf-8")

    def chips_text(self) -> str:
        chosen = [c for c in self.chips() if c["on"]]
        return "\n\n".join(f"# Skill: {c['name']}\n{c['content']}" for c in chosen)

    # -- the working folder, and the right to act in it ---------------------------

    def folder(self) -> Path | None:
        raw = str(self.settings.get("folder") or "")
        if not raw:
            return None
        path = Path(raw)
        return path if path.is_dir() else None

    def pick_folder(self) -> str:
        """Ask the person for a folder, through the window's own native dialog."""
        if self.window is None:
            raise RuntimeError("there is no window to open a dialog from; run without --browser to choose "
                               "a folder")
        import webview

        chosen = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        if not chosen:
            return ""
        self.set_folder(chosen[0])
        return str(self.folder() or "")

    def set_folder(self, raw: str) -> None:
        if not str(raw).strip():
            self.settings["folder"] = ""
            self.allowed.clear()
            self.save_settings()
            return
        path = Path(str(raw)).expanduser()
        if not path.is_dir():
            raise RuntimeError(f"not a folder: {path}")
        self.settings["folder"] = str(path.resolve())
        self.allowed.clear()           # a new folder is a new set of questions
        self.save_settings()

    def inside(self, relative: str) -> Path:
        root = self.folder()
        if root is None:
            raise RuntimeError("no working folder has been chosen yet")
        return inside(root, relative)

    def needs_asking(self, key: str) -> bool:
        if self.settings.get("permission") != "session":
            return True
        return key not in self.allowed

    def allow(self, key: str, *, remember: bool = False) -> None:
        if remember:
            self.allowed.add(key)

    def ask(self, emit, cancel: threading.Event, key: str, payload: dict) -> str:
        """Put a gated action to the person, and wait for them.

        The question goes out as an event in the conversation's stream; the
        answer comes back as a POST to /local/answer. Stopping the turn is a no.
        """
        ask_id = uuid.uuid4().hex[:12]
        waiting = {"event": threading.Event(), "answer": "deny", "key": key}
        self.asks[ask_id] = waiting
        emit("ask", {**payload, "ask_id": ask_id, "session": self.settings.get("permission") == "session"})
        deadline = time.monotonic() + 3600
        while not waiting["event"].wait(0.4):
            if cancel.is_set() or time.monotonic() > deadline:
                break
        self.asks.pop(ask_id, None)
        answer = waiting["answer"] if waiting["event"].is_set() else "deny"
        if answer == "session":
            self.allow(key, remember=True)
        emit("asked", {"ask_id": ask_id, "answer": answer})
        return answer

    def answer(self, ask_id: str, answer: str) -> None:
        waiting = self.asks.get(ask_id)
        if waiting is None:
            raise KeyError("that question is no longer waiting")
        waiting["answer"] = answer if answer in ("allow", "session", "deny") else "deny"
        waiting["event"].set()

    def web(self) -> Web | None:
        internet = self.settings.get("internet") or {}
        if internet.get("mode", "off") == "off":
            return None
        return Web(internet)

    def open_terminal(self) -> None:
        """The system's own terminal, in the working folder."""
        folder = self.folder() or data_dir()
        if os.name == "nt":
            for argv in (["wt.exe", "-d", str(folder)], ["cmd.exe", "/c", "start", "cmd.exe"]):
                try:
                    subprocess.Popen(argv, cwd=str(folder))
                    return
                except OSError:
                    continue
            raise RuntimeError("no terminal could be started")
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-a", "Terminal", str(folder)])
            return
        for argv in (["x-terminal-emulator"], ["gnome-terminal"], ["konsole"], ["xterm"]):
            try:
                subprocess.Popen(argv, cwd=str(folder))
                return
            except OSError:
                continue
        raise RuntimeError("no terminal could be started")

    def open_folder(self, which: str) -> None:
        folder = {"models": models_dir(), "data": data_dir(), "work": self.folder()}.get(which)
        if folder is None:
            return
        folder.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(folder)  # noqa: S606 - a folder the app itself created or the person chose
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])

    def save_as(self, name: str, content: str) -> str:
        """A file the model wrote, saved wherever the person says, through the native dialog."""
        if self.window is None:
            raise RuntimeError("no window")
        import webview

        chosen = self.window.create_file_dialog(webview.SAVE_DIALOG, save_filename=Path(name).name or "file.txt")
        if not chosen:
            return ""
        target = Path(chosen if isinstance(chosen, str) else chosen[0])
        target.write_text(content, encoding="utf-8")
        return str(target)

    # -- what the page shows -------------------------------------------------------

    def status(self) -> dict:
        facts = runtime.machine()
        base, adapter = self.active()
        profile = self.profile()
        builds = []
        for build in catalogue.BUILDS:
            path = models_dir() / build.filename
            partial = path.with_name(path.name + ".part")
            builds.append({
                "key": build.key, "model": build.model, "gigabytes": round(build.gigabytes, 2),
                "ram_hint_gb": build.ram_hint_gb, "label_en": build.label_en, "label_fa": build.label_fa,
                "installed": self._complete(build),
                "partial_bytes": partial.stat().st_size if partial.is_file() else 0,
                "bytes": build.bytes,
            })
        models = [{"id": m.id, "name": m.name, "engine": m.engine, "summary_en": m.summary_en,
                   "summary_fa": m.summary_fa, "base_repo": m.base_repo} for m in catalogue.MODELS]
        try:
            runtime_path = str(runtime.find_binary())
        except runtime.RuntimeError_:
            runtime_path = ""
        internet = copy.deepcopy(self.settings["internet"])
        # Keys are shown as set or not, never sent back whole.
        internet["keys"] = {k: (v[:4] + "…" + v[-3:] if len(v) > 10 else "set") for k, v in
                            (internet.get("keys") or {}).items() if v}
        if internet.get("custom", {}).get("key"):
            internet["custom"]["key"] = "set"
        settings = {k: v for k, v in self.settings.items() if k not in ("internet", "hf_token", "model", "build")}
        return {
            "version": __version__,
            "engine": self.engine,
            "state": self.state if self.engine == "local" else "ready",
            "error": self.error,
            "profile": {"name": profile.name, "engine": profile.engine, "base": profile.base_name,
                        "adapter": profile.adapter_name, "has_adapter": profile.has_adapter},
            "active": {"base": base["key"] if base else "", "adapter": adapter["key"] if adapter else ""},
            "bases": self.bases(),
            "adapters": self.adapters(),
            "builds": builds,
            "models": models,
            "jobs": self.jobs.public(),
            "runtime": runtime_path,
            "machine": facts,
            "paths": {"models": str(models_dir()), "data": str(data_dir())},
            "log": self.server.tail(6) if self.server else "",
            "settings": settings,
            "internet": internet,
            "hf_token": bool(self.settings.get("hf_token")),
            "folder": str(self.folder() or ""),
            "allowed": len(self.allowed),
            "window": self.window is not None,
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

    # The loopback must never go through a proxy the person set for the internet.
    os.environ["NO_PROXY"] = ",".join(filter(None, [os.environ.get("NO_PROXY", ""), "127.0.0.1", "localhost"]))
    controller = Controller(engine)
    port = port or _free_port()
    controller.self_url = f"http://127.0.0.1:{port}"
    web = build(controller)

    config = uvicorn.Config(web, host="127.0.0.1", port=port, log_level="warning", timeout_keep_alive=30)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="web")
    thread.start()
    _wait_for(controller.self_url + "/local/status")

    controller.start_model()
    print(f"AhoosAI Studio {__version__} (offline) at {controller.self_url}", flush=True)

    def shutdown() -> None:
        for cancel in list(controller.runs.values()):
            cancel.set()
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
            # with a .NET exception raised on the GUI thread, outside Python --
            # the whole process exits without a word.
            icon = assets_dir() / ("icon.ico" if os.name == "nt" else "icon.png")
            controller.window = webview.create_window(
                f"AhoosAI Studio {__version__}", controller.self_url + "/",
                width=1360, height=880, min_size=(820, 580), text_select=True,
                background_color="#000000")
            try:
                webview.start(icon=str(icon) if icon.is_file() else None, private_mode=False,
                              storage_path=str(data_dir() / "webview"))
            except Exception as exc:  # noqa: BLE001
                # pywebview found no GUI backend -- a Linux machine without
                # WebKitGTK, most often. The default browser works the same.
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
