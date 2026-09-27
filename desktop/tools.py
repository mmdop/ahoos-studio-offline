"""What the model can do, not just describe: the tools behind the agent.

Each tool is a small function over the working folder or the web, with the
text the model reads back and the summary the person sees. The ones that change
anything -- writing, editing, deleting, running -- are marked `gated`: the agent
puts each call to the person before it happens (see agent.py), exactly as the
terminal always has.

WHY THE RESULTS ARE SHORT

On the CPUs this app runs on, llama.cpp reads a prompt at ten to forty tokens a
second. A tool result is prompt: a whole file read back is a minute of the
person watching nothing. So every result is cut to what the next step needs --
the head of a file, the tail of a command's output, the first screen of a page
-- and says that it was cut, so the model can ask for the rest.

PATHS

Every path goes through `app.inside`, which resolves it first and refuses
anything that lands outside the working folder. A tool never sees a path that
was not checked.
"""

from __future__ import annotations

import difflib
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env", ".idea", ".vscode",
             ".next", ".nuxt", "dist", "build", ".mypy_cache", ".pytest_cache", ".tox", "target",
             ".gradle", "Pods", ".cache"}
READ_LIMIT = 4000                # characters of a file the model reads at once
OUTPUT_LIMIT = 2000              # characters of a command's output sent back
LIST_LIMIT = 120                 # entries in one listing
SEARCH_LIMIT = 40                # matching lines in one search
TEXT_LIMIT = 1_000_000           # files larger than this are not read as text


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
    command_timeout: float = 120.0
    shell: Any = None


@dataclass(frozen=True)
class Tool:
    name: str
    needs: str                   # "folder" | "web" | ""
    gated: bool
    params: tuple[tuple[str, str, bool], ...]    # (name, description, required)
    describe: str                # the one line the model reads
    run: Callable[[Context, dict], Result]
    key: Callable[[dict], str] | None = None     # what "allow for this session" remembers

    def schema(self) -> dict:
        """The decision grammar's branch for this tool."""
        option: dict = {"type": "object", "properties": {"name": {"const": self.name}}, "required": ["name"]}
        if self.params:
            option["properties"]["arguments"] = {
                "type": "object",
                "properties": {name: {"type": "string"} for name, _, _ in self.params},
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


def _is_text(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            head = handle.read(8192)
    except OSError:
        return False
    return b"\x00" not in head


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} B"


def diff(old: str, new: str, name: str, limit: int = 6000) -> str:
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{name}", f"b/{name}", lineterm="", n=2)
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit] + "\n… (diff cut)"


def arg(args: dict, name: str, default: str = "") -> str:
    value = args.get(name, default)
    return value if isinstance(value, str) else json_text(value)


def json_text(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


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
            if item.is_dir():
                if item.name in SKIP_DIRS:
                    lines.append(f"{prefix}{item.name}/ (skipped)")
                    continue
                lines.append(f"{prefix}{item.name}/")
                if depth < 1:
                    walk(item, depth + 1, prefix + "  ")
            else:
                try:
                    size = item.stat().st_size
                except OSError:
                    size = 0
                lines.append(f"{prefix}{item.name}  ({_human(size)})")

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
    if path.stat().st_size > TEXT_LIMIT or not _is_text(path):
        return Result(False, f"{name} is binary or too large to read as text.", f"{name} is not text")
    text = _read_text(path)
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
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = Path(folder) / name
            try:
                if path.stat().st_size > TEXT_LIMIT or not _is_text(path):
                    continue
                for number, line in enumerate(_read_text(path).splitlines(), 1):
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
    path = ctx.inside(arg(args, "path"))
    name = _rel(ctx, path)
    content = arg(args, "content")
    existed = path.is_file()
    old = _read_text(path) if existed and _is_text(path) else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")
    lines = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
    verb = "updated" if existed else "created"
    shown = diff(old, content, name) if existed else content[:6000]
    return Result(True, f"{verb} {name} ({lines} lines).", f"{verb} {name} · {lines} lines", shown,
                  {"path": name, "lines": lines, "created": not existed})


def edit_file(ctx: Context, args: dict) -> Result:
    path = ctx.inside(arg(args, "path"))
    name = _rel(ctx, path)
    find, replace = arg(args, "find"), arg(args, "replace")
    if not path.is_file():
        return Result(False, f"There is no file {name}; use write_file to create it.", f"no file {name}")
    text = _read_text(path)
    count = text.count(find) if find else 0
    if count != 1:
        why = "does not appear" if count == 0 else f"appears {count} times"
        return Result(False, f"The text to replace {why} in {name}. Read the file and give text that "
                             "appears exactly once.", f"edit of {name} did not match")
    new = text.replace(find, replace, 1)
    path.write_text(new, encoding="utf-8", newline="")
    return Result(True, f"edited {name}.", f"edited {name}", diff(text, new, name), {"path": name})


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
    path.unlink()
    return Result(True, f"deleted {name}.", f"deleted {name}")


def run_command(ctx: Context, args: dict) -> Result:
    from .terminal import Shell

    command = arg(args, "command").strip()
    if not command:
        return Result(False, "The command was empty.", "empty command")
    shell = ctx.shell or Shell(ctx.folder)
    queue = shell.run(command, timeout=ctx.command_timeout)
    lines: list[str] = []
    code = None
    error = ""
    started = time.monotonic()
    while True:
        if ctx.cancel.is_set():
            shell.stop()
        item = queue.get()
        if item is None:
            break
        if "line" in item:
            lines.append(item["line"])
            ctx.emit("tool_output", {"line": item["line"]})
        elif "code" in item:
            code = item["code"]
        elif "error" in item:
            error = item["error"]
            ctx.emit("tool_output", {"line": error, "bad": True})
    seconds = time.monotonic() - started
    output = "\n".join(lines)
    tail = output if len(output) <= OUTPUT_LIMIT else "… (earlier output cut)\n" + output[-OUTPUT_LIMIT:]
    status = f"exit code {code}" if code is not None else (error or "stopped")
    text = f"$ {command}\n{tail or '(no output)'}\n[{status}, {seconds:.1f}s]"
    return Result(code == 0, text, f"ran `{command[:60]}` · {status}", output[-20000:],
                  {"code": code, "seconds": round(seconds, 1)})


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
    Tool("list_files", "folder", False, (("path", "folder inside the working folder", False),),
         "list a folder (\".\" is the working folder itself)", list_files),
    Tool("read_file", "folder", False, (("path", "file", True), ("from_line", "line to start at", False)),
         "read a text file", read_file),
    Tool("search_files", "folder", False, (("text", "text to find", True), ("path", "folder", False)),
         "find the lines that contain some text", search_files),
    Tool("write_file", "folder", True, (("path", "file", True), ("content", "the whole file", True)),
         "create or replace a file; give its complete content", write_file, _path_key("write")),
    Tool("edit_file", "folder", True,
         (("path", "file", True), ("find", "exact text now in the file", True),
          ("replace", "text to put instead", True)),
         "change one exact piece of an existing file", edit_file, _path_key("edit")),
    Tool("delete_file", "folder", True, (("path", "file", True),),
         "delete a file or an empty folder", delete_file, _path_key("delete")),
    Tool("run_command", "folder", True, (("command", "command line", True),),
         "run a command in the working folder and get its output", run_command,
         lambda args: "shell:" + arg(args, "command").strip()),
    Tool("web_search", "web", False, (("query", "what to search for", True),),
         "search the internet", web_search),
    Tool("fetch_url", "web", False, (("url", "http or https address", True),),
         "read a web page as text", fetch_url),
)

REPLY = "reply"


def available(folder: bool, web: bool, read_only: bool = False) -> list[Tool]:
    chosen = [t for t in TOOLS if (t.needs == "folder" and folder) or (t.needs == "web" and web)]
    if read_only:
        chosen = [t for t in chosen if not t.gated]
    return chosen


def by_name(name: str) -> Tool | None:
    return next((t for t in TOOLS if t.name == name), None)


def decision_schema(tools: list[Tool]) -> dict:
    """One JSON object: a tool and its arguments, or `reply`.

    Handed to llama.cpp as a grammar, so the model cannot answer with anything
    else -- which is the whole trick. Asked in words, a small model prints the
    code it was asked to save; given only this to write, it saves it.
    """
    options = [t.schema() for t in tools]
    options.append({"type": "object", "properties": {"name": {"const": REPLY}}, "required": ["name"]})
    return {"oneOf": options}


def preview(ctx: Context, tool: Tool, args: dict) -> dict:
    """What the person is shown before a gated tool runs."""
    if tool.name == "write_file":
        path = ctx.inside(arg(args, "path"))
        name = _rel(ctx, path)
        content = arg(args, "content")
        if path.is_file() and _is_text(path):
            return {"title": f"Replace {name}?", "path": name, "diff": diff(_read_text(path), content, name)}
        return {"title": f"Create {name}?", "path": name, "content": content[:8000],
                "lines": content.count("\n") + 1}
    if tool.name == "edit_file":
        path = ctx.inside(arg(args, "path"))
        name = _rel(ctx, path)
        if path.is_file():
            old = _read_text(path)
            new = old.replace(arg(args, "find"), arg(args, "replace"), 1)
            return {"title": f"Edit {name}?", "path": name, "diff": diff(old, new, name)}
        return {"title": f"Edit {name}?", "path": name}
    if tool.name == "delete_file":
        return {"title": f"Delete {_rel(ctx, ctx.inside(arg(args, 'path')))}?"}
    if tool.name == "run_command":
        return {"title": "Run this command?", "command": arg(args, "command")}
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
