"""What the model can do, not just describe: the tools behind the agent.

Each tool is a small function over the working folder or the web, with the
text the model reads back and the summary the person sees. The ones that change
anything -- writing, editing, deleting, running -- are marked `gated`: the agent
puts each call to the person before it happens unless the person has said not
to ask (see agent.py and checkpoints.py, which makes every change undoable).

WHAT 3.0 CHANGED

A tool call is now short. `write_file` names a file and says what it is for;
the file itself is written afterwards, as a code block (writer.py) -- a
seven-billion-parameter model writes a few dozen characters of code inside a
JSON string and a whole program outside one. `edit_file` names a file and the
change; the change is written as SEARCH/REPLACE blocks the same way.

A command that does not end -- a dev server -- runs in the background instead
of holding the turn until its time is up (procs.py). Binary files are named as
such in a listing and refused by name when read, so a model shown a games folder
does not spend its turn reading `.exe` files.

WHY THE RESULTS ARE SHORT

On the CPUs this app runs on, llama.cpp reads a prompt at ten to forty tokens a
second. A tool result is prompt: a whole file read back is a minute of the
person watching nothing. So every result is cut to what the next step needs and
says that it was cut, so the model can ask for the rest.

PATHS

Every path goes through `app.inside`, which resolves it first and refuses
anything that lands outside the working folder.
"""

from __future__ import annotations

import difflib
import os
import queue
import re
import subprocess
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import tether

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env", ".idea", ".vscode",
             ".next", ".nuxt", "dist", "build", ".mypy_cache", ".pytest_cache", ".tox", "target",
             ".gradle", "Pods", ".cache", ".ahoos"}
BINARY_EXT = {".exe", ".dll", ".so", ".dylib", ".bin", ".dat", ".pak", ".rar", ".zip", ".7z", ".gz", ".tar",
              ".iso", ".msi", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".mp3", ".wav", ".ogg",
              ".mp4", ".mkv", ".avi", ".mov", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
              ".ttf", ".otf", ".woff", ".woff2", ".pyc", ".class", ".jar", ".gguf", ".safetensors", ".pt",
              ".pth", ".onnx", ".db", ".sqlite", ".lnk", ".sys", ".cab", ".apk", ".psd"}
READ_LIMIT = 6000                # characters of a file the model reads at once
OUTPUT_LIMIT = 2500              # characters of a command's output sent back
LIST_LIMIT = 150                 # entries in one listing
SEARCH_LIMIT = 40                # matching lines in one search
TEXT_LIMIT = 1_000_000           # files larger than this are not read as text
FOREGROUND = 120.0               # seconds a command may hold the turn
OPENABLE = {".html", ".htm", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".txt", ".md", ".pdf", ".csv"}


@dataclass
class Result:
    ok: bool
    text: str                    # what the model reads
    summary: str                 # one line for the person
    output: str = ""             # what the card shows when opened
    meta: dict = field(default_factory=dict)


@dataclass
class Context:
    """What a tool runs against."""

    folder: Path | None
    inside: Callable[[str], Path]
    web: Any = None                              # web.Web, when the internet is on
    cancel: threading.Event = field(default_factory=threading.Event)
    emit: Callable[[str, dict], None] = lambda event, data: None
    command_timeout: float = FOREGROUND
    shell: Any = None
    before_change: Callable[[Path], None] = lambda path: None   # checkpoints.Checkpoint.before
    procs: Any = None                                          # procs.Procs


@dataclass(frozen=True)
class Tool:
    name: str
    needs: str                   # "folder" | "web" | ""
    gated: bool
    params: tuple[tuple[str, str, bool], ...]    # (name, description, required)
    describe: str                # the one line the model reads
    run: Callable[[Context, dict], Result] | None
    key: Callable[[dict], str] | None = None     # what "allow for this session" remembers

    def schema(self) -> dict:
        """The decision grammar's branch for this tool."""
        option: dict = {"type": "object", "properties": {"name": {"const": self.name}}, "required": ["name"]}
        if self.params:
            option["properties"]["arguments"] = {
                "type": "object",
                "properties": {name: {"type": "string", "maxLength": 600 if name in ("change", "about", "detail")
                                      else 400} for name, _, _ in self.params},
                "required": [name for name, _, required in self.params if required],
            }
            option["required"].append("arguments")
        return option

    def signature(self) -> str:
        args = ", ".join(name + ("" if required else "?") for name, _, required in self.params)
        return f"{self.name}({args})"


# -- helpers --------------------------------------------------------------------

def _rel(ctx: Context, path: Path) -> str:
    root = ctx.folder.resolve() if ctx.folder else path.parent
    try:
        text = str(path.resolve().relative_to(root))
    except ValueError:
        text = str(path)
    return "." if text == "." else text.replace("\\", "/")


def is_binary(path: Path) -> bool:
    if path.suffix.lower() in BINARY_EXT:
        return True
    try:
        with path.open("rb") as handle:
            head = handle.read(8192)
    except OSError:
        return False
    return b"\x00" in head


def _is_text(path: Path) -> bool:
    return not is_binary(path)


def read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


_read_text = read_text


def _human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} B"


def diff(old: str, new: str, name: str, limit: int = 8000) -> str:
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{name}", f"b/{name}", lineterm="", n=2)
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit] + "\n… (diff cut)"


def arg(args: dict, name: str, default: str = "") -> str:
    value = args.get(name, default)
    return value if isinstance(value, str) else json_text(value)


def json_text(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def count_lines(content: str) -> int:
    return content.count("\n") + (0 if content.endswith("\n") or not content else 1)


# -- the folder -----------------------------------------------------------------

def list_files(ctx: Context, args: dict) -> Result:
    path = ctx.inside(arg(args, "path", ".") or ".")
    if not path.is_dir():
        return Result(False, f"{_rel(ctx, path)} is not a folder.", f"no folder {_rel(ctx, path)}")
    lines: list[str] = []

    def walk(folder: Path, depth: int, prefix: str) -> None:
        try:
            items = sorted(folder.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return
        for item in items:
            if len(lines) >= LIST_LIMIT:
                return
            if item.name.startswith(".") and item.name not in (".env.example", ".gitignore"):
                continue
            if item.is_dir():
                if item.name in SKIP_DIRS:
                    lines.append(f"{prefix}{item.name}/ (skipped)")
                    continue
                lines.append(f"{prefix}{item.name}/")
                if depth < 2:
                    walk(item, depth + 1, prefix + "  ")
            else:
                try:
                    size = item.stat().st_size
                except OSError:
                    size = 0
                tag = ", binary" if item.suffix.lower() in BINARY_EXT else ""
                lines.append(f"{prefix}{item.name}  ({_human(size)}{tag})")

    walk(path, 0, "")
    where = _rel(ctx, path)
    if not lines:
        return Result(True, f"{where} is empty.", f"{where}: empty")
    cut = "\n… (more entries not shown)" if len(lines) >= LIST_LIMIT else ""
    body = "\n".join(lines) + cut
    return Result(True, f"{where}:\n{body}", f"listed {where} ({len(lines)} entries)", body)


def read_file(ctx: Context, args: dict) -> Result:
    path = ctx.inside(arg(args, "path"))
    name = _rel(ctx, path)
    if not path.is_file():
        return Result(False, f"There is no file {name}.", f"no file {name}")
    if path.stat().st_size > TEXT_LIMIT or is_binary(path):
        return Result(False, f"{name} is a binary file (a program, an archive, an image or similar). It cannot "
                             "be read as text and is not part of the code; do not try to read it again.",
                      f"{name} is not text", meta={"binary": True})
    text = read_text(path)
    lines = text.splitlines()
    try:
        start = max(1, int(str(args.get("from_line") or "1").strip() or 1))
    except ValueError:
        start = 1
    chunk, used = [], 0
    for number in range(start, len(lines) + 1):
        line = f"{number}: {lines[number - 1]}"
        if used + len(line) > READ_LIMIT and chunk:
            break
        chunk.append(line)
        used += len(line) + 1
    end = start + len(chunk) - 1
    more = (f"\n… lines {end + 1}-{len(lines)} not shown; read again with from_line={end + 1}"
            if end < len(lines) else "")
    body = "\n".join(chunk)
    return Result(True, f"{name} (lines {start}-{end} of {len(lines)}):\n{body}{more}",
                  f"read {name} ({len(lines)} lines)", body, {"path": name})


def search_files(ctx: Context, args: dict) -> Result:
    needle = arg(args, "text").strip()
    if not needle:
        return Result(False, "Say what text to search for.", "nothing to search for")
    root = ctx.inside(arg(args, "path", ".") or ".")
    found: list[str] = []
    lowered = needle.lower()
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            path = Path(folder) / name
            try:
                if path.stat().st_size > TEXT_LIMIT or is_binary(path):
                    continue
                for number, line in enumerate(read_text(path).splitlines(), 1):
                    if lowered in line.lower():
                        found.append(f"{_rel(ctx, path)}:{number}: {line.strip()[:160]}")
                        if len(found) >= SEARCH_LIMIT:
                            break
            except OSError:
                continue
            if len(found) >= SEARCH_LIMIT:
                break
        if len(found) >= SEARCH_LIMIT or ctx.cancel.is_set():
            break
    if not found:
        return Result(True, f'No lines contain "{needle}".', f'"{needle}": no matches')
    body = "\n".join(found)
    return Result(True, body, f'"{needle}": {len(found)} matches', body)


def write_file(ctx: Context, args: dict) -> Result:
    """Write `content` to `path`. The agent supplies `content` after the model writes it."""
    path = ctx.inside(arg(args, "path"))
    name = _rel(ctx, path)
    if path.is_dir():
        return Result(False, f"{name} is a folder.", f"{name} is a folder")
    content = arg(args, "content")
    existed = path.is_file()
    old = read_text(path) if existed and not is_binary(path) else ""
    ctx.before_change(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")
    lines = count_lines(content)
    verb = "updated" if existed else "created"
    shown = diff(old, content, name) if existed else content[:12000]
    return Result(True, f"{verb} {name} ({lines} lines).", f"{verb} {name} · {lines} lines", shown,
                  {"path": name, "lines": lines, "created": not existed})


def replace_file(ctx: Context, path: Path, new: str) -> Result:
    """Put an edited version of an existing file in place."""
    name = _rel(ctx, path)
    old = read_text(path)
    ctx.before_change(path)
    path.write_text(new, encoding="utf-8", newline="")
    return Result(True, f"edited {name} ({count_lines(new)} lines now).", f"edited {name}", diff(old, new, name),
                  {"path": name, "lines": count_lines(new)})


def edit_file(ctx: Context, args: dict) -> Result:
    """The 2.5 form -- find and replace one exact piece -- kept for calls that carry them."""
    path = ctx.inside(arg(args, "path"))
    name = _rel(ctx, path)
    find, replace = arg(args, "find"), arg(args, "replace")
    if not path.is_file():
        return Result(False, f"There is no file {name}; use write_file to create it.", f"no file {name}")
    text = read_text(path)
    count = text.count(find) if find else 0
    if count != 1:
        why = "does not appear" if count == 0 else f"appears {count} times"
        return Result(False, f"The text to replace {why} in {name}. Read the file and give text that "
                             "appears exactly once.", f"edit of {name} did not match")
    return replace_file(ctx, path, text.replace(find, replace, 1))


def delete_file(ctx: Context, args: dict) -> Result:
    path = ctx.inside(arg(args, "path"))
    name = _rel(ctx, path)
    if path.is_dir():
        try:
            path.rmdir()
        except OSError:
            return Result(False, f"{name} is a folder that is not empty; only files and empty folders "
                                 "can be deleted.", f"{name} not deleted")
        return Result(True, f"deleted the empty folder {name}.", f"deleted {name}/")
    if not path.is_file():
        return Result(False, f"There is no file {name}.", f"no file {name}")
    ctx.before_change(path)
    path.unlink()
    return Result(True, f"deleted {name}.", f"deleted {name}")


# -- commands -------------------------------------------------------------------

def _yes(value: Any) -> bool:
    return str(value or "").strip().lower() in ("yes", "true", "1", "y", "background")


def run_command(ctx: Context, args: dict) -> Result:
    from .procs import PROCS, address_in, looks_like_server
    from .terminal import kill_tree, shell_for

    command = arg(args, "command").strip()
    if not command:
        return Result(False, "The command was empty.", "empty command")
    if ctx.folder is None:
        return Result(False, "There is no project folder to run commands in yet.", "no folder")
    procs = ctx.procs or PROCS

    if _yes(args.get("background")) or looks_like_server(command):
        proc = procs.start(command, ctx.folder)
        procs.wait_for_start(proc)
        tail = "\n".join(list(proc.lines)[-30:])
        for line in list(proc.lines)[-30:]:
            ctx.emit("tool_output", {"line": line})
        if not proc.running:
            text = f"$ {command}\n{tail or '(no output)'}\n[it stopped at once, exit code {proc.process.poll()}]"
            return Result(False, text, f"`{command[:60]}` stopped at once", tail,
                          {"code": proc.process.poll(), "background": proc.id})
        where = f" at {proc.url}" if proc.url else ""
        text = (f"$ {command}\n{tail or '(no output yet)'}\n[running in the background{where} as [{proc.id}]; "
                f"stop it with stop_command(id=\"{proc.id}\")]")
        return Result(True, text, f"started `{command[:60]}` in the background{where}", tail,
                      {"background": proc.id, "url": proc.url, "code": None})

    try:
        process = subprocess.Popen(
            shell_for(command), cwd=str(ctx.folder), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        tether.tie(process)
    except OSError as error:
        return Result(False, f"Could not run it: {error}", f"`{command[:60]}` could not start")
    lines: list[str] = []
    feed: queue.Queue = queue.Queue()
    moved: dict = {}

    def pump() -> None:
        for line in process.stdout or []:
            line = line.rstrip("\n")[:4000]
            if moved.get("proc") is not None:
                procs.feed(moved["proc"], line)
            else:
                feed.put(line)
        feed.put(None)

    threading.Thread(target=pump, daemon=True, name="command").start()
    started = time.monotonic()
    deadline = started + max(5.0, ctx.command_timeout)
    finished = False
    url = ""
    while time.monotonic() < deadline:
        if ctx.cancel.is_set():
            kill_tree(process)
            break
        try:
            item = feed.get(timeout=0.25)
        except queue.Empty:
            continue
        if item is None:
            finished = True
            break
        lines.append(item)
        url = url or address_in(item)
        ctx.emit("tool_output", {"line": item})
        if len(lines) > 4000:
            kill_tree(process)
            break
    seconds = time.monotonic() - started
    output = "\n".join(lines)
    tail = output if len(output) <= OUTPUT_LIMIT else "… (earlier output cut)\n" + output[-OUTPUT_LIMIT:]
    if not finished and process.poll() is None and not ctx.cancel.is_set():
        if url:
            # Quiet and still running, having said where it is: a server. Kept.
            proc = procs.adopt(command, ctx.folder, process, lines, reader_running=True)
            moved["proc"] = proc
            text = (f"$ {command}\n{tail}\n[still running after {seconds:.0f}s at {url}; moved to the background "
                    f"as [{proc.id}]]")
            return Result(True, text, f"`{command[:60]}` is running at {url}", output[-20000:],
                          {"background": proc.id, "url": url, "code": None, "seconds": round(seconds, 1)})
        kill_tree(process)
        text = (f"$ {command}\n{tail or '(no output)'}\n[stopped after {seconds:.0f} seconds. If it is meant to "
                "keep running -- a server, a game window -- run it with background=\"yes\"]")
        return Result(False, text, f"`{command[:60]}` stopped after {seconds:.0f}s", output[-20000:],
                      {"code": None, "seconds": round(seconds, 1)})
    try:
        code = process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        kill_tree(process)
        code = None
    status = f"exit code {code}" if code is not None else "stopped"
    text = f"$ {command}\n{tail or '(no output)'}\n[{status}, {seconds:.1f}s]"
    return Result(code == 0, text, f"ran `{command[:60]}` · {status}", output[-20000:],
                  {"code": code, "seconds": round(seconds, 1)})


def stop_command(ctx: Context, args: dict) -> Result:
    from .procs import PROCS

    procs = ctx.procs or PROCS
    ident = arg(args, "id").strip().strip("[]")
    proc = procs.get(ident)
    if proc is None:
        running = ", ".join(f"[{p.id}] {p.command[:40]}" for p in procs.running()) or "none"
        return Result(False, f"No background command [{ident}]. Running: {running}.", f"no command [{ident}]")
    procs.stop(ident)
    return Result(True, f"stopped [{ident}] {proc.command}.", f"stopped `{proc.command[:60]}`")


def open_file(ctx: Context, args: dict) -> Result:
    """A page or a picture, in the person's own browser or viewer -- never a program."""
    path = ctx.inside(arg(args, "path"))
    name = _rel(ctx, path)
    if not path.is_file():
        return Result(False, f"There is no file {name}.", f"no file {name}")
    if path.suffix.lower() not in OPENABLE:
        return Result(False, f"{name} cannot be opened this way: only pages, pictures and documents can. To run "
                             "a program, use run_command.", f"{name} not opened")
    webbrowser.open(path.resolve().as_uri())
    return Result(True, f"opened {name} for the user.", f"opened {name}", meta={"path": name})


# -- the web --------------------------------------------------------------------

def web_search(ctx: Context, args: dict) -> Result:
    query = arg(args, "query").strip()
    if not query:
        return Result(False, "The search was empty.", "empty search")
    results = ctx.web.search(query)
    if not results:
        return Result(True, f'The search for "{query}" found nothing.', f'"{query}": nothing found')
    lines = [f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet'][:240]}" for i, r in enumerate(results, 1)]
    body = "\n".join(lines)
    return Result(True, body, f'searched "{query}" · {len(results)} results', body,
                  {"results": results, "via": ctx.web.label()})


def fetch_url(ctx: Context, args: dict) -> Result:
    url = arg(args, "url").strip()
    page = ctx.web.fetch(url)
    body = page["text"]
    return Result(True, f"{page['title']}\n{page['url']}\n\n{body}", f"read {page['host']}",
                  body, {"url": page["url"], "title": page["title"]})


# -- the list the model sees ------------------------------------------------------

def _path_key(verb: str) -> Callable[[dict], str]:
    return lambda args: f"{verb}:{arg(args, 'path').strip().replace(chr(92), '/')}"


TOOLS: tuple[Tool, ...] = (
    Tool("list_files", "folder", False, (("path", "folder inside the project", False),),
         "list a folder and its subfolders (\".\" is the project folder)", list_files),
    Tool("read_file", "folder", False, (("path", "file", True), ("from_line", "line to start at", False)),
         "read a text file, with line numbers", read_file),
    Tool("search_files", "folder", False, (("text", "text to find", True), ("path", "folder", False)),
         "find the lines that contain some text", search_files),
    Tool("write_file", "folder", True, (("path", "file", True), ("about", "what the file is for", False)),
         "create a file, or replace one whole; you write its content right after", write_file,
         _path_key("write")),
    Tool("edit_file", "folder", True, (("path", "file", True), ("change", "what to change", True)),
         "change part of an existing file; you write the change right after", None, _path_key("edit")),
    Tool("delete_file", "folder", True, (("path", "file", True),),
         "delete a file or an empty folder", delete_file, _path_key("delete")),
    Tool("run_command", "folder", True, (("command", "command line", True),
                                         ("background", "\"yes\" for a server or anything that keeps running",
                                          False)),
         "run a command in the project folder and get its output", run_command,
         lambda args: "shell:" + arg(args, "command").strip()),
    Tool("stop_command", "folder", False, (("id", "the background command's id", True),),
         "stop a command running in the background", stop_command),
    Tool("open_file", "folder", False, (("path", "a page, picture or document", True),),
         "open a page (index.html) or picture for the user to see", open_file),
    Tool("web_search", "web", False, (("query", "what to search for", True),),
         "search the internet", web_search),
    Tool("fetch_url", "web", False, (("url", "http or https address", True),),
         "read a web page as text", fetch_url),
)

REPLY = "reply"
PLAN = "plan"
RESUME = "resume"
STEP_DONE = "step_done"
READ_ONLY = ("list_files", "read_file", "search_files", "web_search", "fetch_url")
WORK = ("list_files", "read_file", "search_files", "write_file", "edit_file", "delete_file", "run_command",
        "stop_command", "open_file", "web_search", "fetch_url")


def available(folder: bool, web: bool, read_only: bool = False) -> list[Tool]:
    chosen = [t for t in TOOLS if (t.needs == "folder" and folder) or (t.needs == "web" and web)]
    if read_only:
        chosen = [t for t in chosen if not t.gated]
    return chosen


def by_name(name: str) -> Tool | None:
    return next((t for t in TOOLS if t.name == name), None)


# The plan, as the arguments of one call. Lengths are capped by the grammar so a
# small model cannot spend its turn on one endless step.
PLAN_ARGS = {
    "type": "object",
    "properties": {
        "goal": {"type": "string", "maxLength": 160},
        "folder": {"type": "string", "maxLength": 40},
        "steps": {
            "type": "array", "minItems": 1, "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "do": {"enum": ["create", "edit", "run", "other"]},
                    "path": {"type": "string", "maxLength": 120},
                    "detail": {"type": "string", "maxLength": 240},
                },
                "required": ["do", "path", "detail"],
            },
        },
    },
    "required": ["goal", "folder", "steps"],
}


def decision_schema(tools: list[Tool], *, plan: bool = False, resume: bool = False, step_done: bool = False,
                    reply: bool = True) -> dict:
    """One JSON object: a tool and its arguments, or a word that ends a phase.

    Handed to llama.cpp as a grammar, so the model cannot answer with anything
    else -- which is the whole trick. Asked in words, a small model prints the
    code it was asked to save; given only this to write, it saves it.
    """
    options = [t.schema() for t in tools]
    if plan:
        options.append({"type": "object", "properties": {"name": {"const": PLAN}, "arguments": PLAN_ARGS},
                        "required": ["name", "arguments"]})
    if resume:
        options.append({"type": "object", "properties": {"name": {"const": RESUME}}, "required": ["name"]})
    if step_done:
        options.append({"type": "object", "properties": {"name": {"const": STEP_DONE}, "arguments": {
            "type": "object", "properties": {"note": {"type": "string", "maxLength": 300}}, "required": []}},
            "required": ["name"]})
    if reply:
        options.append({"type": "object", "properties": {"name": {"const": REPLY}}, "required": ["name"]})
    return {"oneOf": options}


def preview(ctx: Context, tool: Tool, args: dict) -> dict:
    """What the person is shown before a gated tool runs."""
    if tool.name == "write_file":
        path = ctx.inside(arg(args, "path"))
        name = _rel(ctx, path)
        content = arg(args, "content")
        if path.is_file() and not is_binary(path):
            return {"title": f"Replace {name}?", "path": name, "diff": diff(read_text(path), content, name)}
        return {"title": f"Create {name}?", "path": name, "content": content[:12000],
                "lines": content.count("\n") + 1}
    if tool.name == "edit_file":
        path = ctx.inside(arg(args, "path"))
        name = _rel(ctx, path)
        if path.is_file() and "new" in args:
            return {"title": f"Edit {name}?", "path": name, "diff": diff(read_text(path), arg(args, "new"), name)}
        if path.is_file():
            old = read_text(path)
            new = old.replace(arg(args, "find"), arg(args, "replace"), 1)
            return {"title": f"Edit {name}?", "path": name, "diff": diff(old, new, name)}
        return {"title": f"Edit {name}?", "path": name}
    if tool.name == "delete_file":
        return {"title": f"Delete {_rel(ctx, ctx.inside(arg(args, 'path')))}?"}
    if tool.name == "run_command":
        return {"title": "Run this command?", "command": arg(args, "command"),
                "background": _yes(args.get("background"))}
    return {"title": f"{tool.name}?"}


# -- reading a call out of prose ---------------------------------------------------

_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*(?:</tool_call>|$)", re.S)
_FENCED = re.compile(r"```(?:json)?\s*(\{\s*\"name\".*?\})\s*```", re.S)


def call_in_text(text: str) -> tuple[str, dict, str] | None:
    """A tool call the model wrote into its answer instead of making it.

    Returns (name, arguments, the text around it). Only a well-formed call to a
    real tool counts: a code sample that happens to be JSON is left alone.
    """
    import json

    for pattern in (_CALL, _FENCED):
        match = pattern.search(text)
        if not match:
            continue
        try:
            call = json.loads(match.group(1))
        except ValueError:
            continue
        name = call.get("name") if isinstance(call, dict) else None
        if isinstance(name, str) and by_name(name):
            arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
            rest = (text[:match.start()] + text[match.end():]).strip()
            return name, arguments, rest
    return None
