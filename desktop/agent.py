"""One turn of a conversation: the model looks, plans, builds, checks, and answers.

WHAT 2.5 GOT WRONG

Asked for a snake game, 2.5 wrote a 16-line page and a 59-line script in
seventeen minutes and called it done; asked for a game in a games folder, it
spent its ten steps trying to read `.exe` files. Three things did that:

- A whole file was the argument of a JSON tool call, written under a grammar --
  every newline an escape, every quote an escape -- and a small model writes very
  little code that way, slowly (1.1 tokens a second against 3 for plain text on
  the same machine).
- Nothing held the task together. The model did one plausible thing, then chose
  `reply`, and the turn was over whether the work was or not.
- Ten steps, and a command that does not end (a dev server) held the turn for
  two minutes and was then killed.

HOW 3.0 WORKS

1. Look. The model may read the folder, then decides with a grammar-bound call:
   `reply` for a question, or `plan` for anything to build, change or fix. The
   tools that write are not in that grammar: nothing changes before a plan.
2. Plan. The plan is the call's arguments: the goal, a folder for a new
   project, and the steps -- one per file to create or change, in order, then
   one to run or open the result. The person sees it as a checklist, and in
   "ask" mode approves it once instead of every file.
3. Build. The turn walks the plan. A file step is written as a code block --
   the generation starts inside an opened fence and ends at the closing one --
   with everything written so far in the conversation, so the script uses the
   ids the page has. A file that runs into the token limit is continued where it
   stopped. An existing file is changed by SEARCH/REPLACE blocks (writer.py). A
   run step is ordinary tool calls -- run it, read the error, fix, run again --
   until the model says the step is done.
4. Check. Each file written is checked without running it (checks.py): Python
   that does not compile, a page that loads a file that is not there, a script
   that looks up an element the page does not have. What is found goes back to
   the model to fix, twice at most.
5. Answer, in the person's language: what was made, and how to open or run it.

A turn that runs out of steps keeps its plan; "continue" picks it up at the
first step not done. Every file a turn changes is copied aside first
(checkpoints.py), so a whole turn can be undone with one click.

THE COST OF A PROMPT

On a CPU llama.cpp reads a prompt at ten to forty tokens a second. Everything
in a turn is one conversation that only grows at the end, so each call starts
from a prefix the server has already read. When it nears the context size, the
oldest file bodies in it are replaced by a line saying where they are.
"""

from __future__ import annotations

import json
import os
import platform
import re
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import __version__, checks, writer
from . import tools as T
from .engine import Cancelled, EngineError, Llama, Sampling, dial_sampling

DECISION_TOKENS = 600            # a tool and short arguments
PLAN_TOKENS = 1600               # a decision that is a plan
FILE_TOKENS = 8192               # one stretch of a file, before it is continued
FILE_STRETCHES = 4               # at most this many stretches to a file
EDIT_TOKENS = 4096
ANSWER_TOKENS = 3072
SUMMARY_TOKENS = 700
MAX_RESULT = 3500                # characters of one tool result the model reads back
LOOK_STEPS = 6                   # reads before deciding
STEP_ACTIONS = 8                 # actions within one step that is not a file
CHECK_ROUNDS = 2
COMPACT_AT = 0.72                # of the context, when old file bodies give way
PLAN_ASK = ("\n\nFirst write the plan you would follow, as a short numbered list, and nothing else. "
            "Do not carry it out yet.")


@dataclass
class Profile:
    """The model that is loaded, as a conversation needs to know it."""

    name: str
    engine: str                  # "apex" | "chat"
    system: str                  # the prompt it was trained under ("" for Apex: Controls writes it)
    has_adapter: bool
    base_name: str = ""
    adapter_name: str = ""


def os_name() -> str:
    system = platform.system()
    if system == "Windows":
        return f"Windows {platform.release()}"
    if system == "Darwin":
        return f"macOS {platform.mac_ver()[0]}"
    return f"{system} {platform.release()}"


def shell_name() -> str:
    return "Windows cmd.exe" if os.name == "nt" else "POSIX sh"


def language_of(text: str) -> str:
    """The language to answer in, when it is not English."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return ""
    arabic = sum(1 for c in letters if "؀" <= c <= "ۿ" or "ﭐ" <= c <= "﻿")
    if arabic / len(letters) > 0.3:
        persian = any(c in text for c in "پچژگکی")
        return "Persian" if persian or "ی" in text else "Arabic"
    return ""


def slug(name: str) -> str:
    """A folder name from whatever the model called the project."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:40].strip("-")


def within(root: Path, relative: str) -> Path:
    """A path inside `root`, or a refusal (the same rule as app.inside)."""
    root = root.resolve()
    target = (root / str(relative)).resolve()
    if target != root and root not in target.parents:
        raise RuntimeError(f"{relative} is outside the project folder")
    return target


# -- the plan ----------------------------------------------------------------------

@dataclass
class Step:
    title: str
    do: str                      # create | edit | run | other | fix
    path: str = ""
    detail: str = ""
    status: str = "pending"      # pending | active | done | failed | skipped
    note: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Plan:
    goal: str
    steps: list[Step] = field(default_factory=list)
    folder: str = ""
    request: str = ""            # the person's own words, which the page shows as the plan's heading

    def to_dict(self) -> dict:
        return {"goal": self.goal, "folder": self.folder, "request": self.request,
                "steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, data: dict) -> "Plan":
        steps = [Step(**{k: v for k, v in s.items() if k in Step.__dataclass_fields__})
                 for s in data.get("steps") or [] if isinstance(s, dict)]
        return cls(goal=str(data.get("goal") or ""), steps=steps, folder=str(data.get("folder") or ""),
                   request=str(data.get("request") or ""))

    @property
    def unfinished(self) -> bool:
        return any(s.status in ("pending", "active") for s in self.steps)

    def text(self) -> str:
        marks = {"done": "x", "failed": "!", "skipped": "-", "active": ">"}
        lines = [f"Plan: {self.goal}"]
        for number, step in enumerate(self.steps, 1):
            target = f" `{step.path}`" if step.path else ""
            lines.append(f"{number}. [{marks.get(step.status, ' ')}] {step.do}{target}: {step.detail or step.title}")
        return "\n".join(lines)


def plan_from_args(args: dict) -> Plan:
    steps = []
    for raw in (args.get("steps") or [])[:14]:
        if not isinstance(raw, dict):
            continue
        do = str(raw.get("do") or "other")
        do = do if do in ("create", "edit", "run", "other") else "other"
        path = str(raw.get("path") or "").strip().strip("'\"`").replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        if do in ("create", "edit") and not path:
            do = "other"
        detail = str(raw.get("detail") or "").strip()[:400]
        title = str(raw.get("title") or "").strip() or (path if path else detail[:80])
        steps.append(Step(title=title[:90], do=do, path=path[:160], detail=detail))
    return Plan(goal=str(args.get("goal") or "").strip()[:300], steps=steps, folder=str(args.get("folder") or ""))


def unprefix(plan: Plan, folder: str) -> None:
    """`todo-list/index.html`, planned for a project that goes into `todo-list/` itself.

    A model that names the project's folder writes it into the paths as well,
    and the files would land in `todo-list/todo-list/`. Only when every file
    step carries it: a project that really keeps its pages in a subfolder of
    the same name as its own has some file outside it, a README at least.
    """
    name = folder.lower()
    paths = [step.path for step in plan.steps if step.path]
    if not name or not paths or not all(path.lower().startswith(name + "/") for path in paths):
        return
    for step in plan.steps:
        if step.path:
            short = step.path[len(name) + 1:]
            if step.title == step.path:
                step.title = short
            step.detail = step.detail.replace(step.path, short)
            step.path = short


# -- what the model is told ----------------------------------------------------------------

def environment(profile: Profile, folder: Path | None, web_label: str, tools: list[T.Tool], *, files: bool,
                workspace: Path | None, unfinished: Plan | None = None, plan_only: bool = False) -> str:
    """Where the model is, what it can do, and how it works."""
    today = datetime.now().strftime("%A %d %B %Y")
    lines = ["", "# Where you are",
             f"You are {profile.name}, running on the user's own computer ({os_name()}) inside AhoosAI Studio "
             f"{__version__}. Nothing you do leaves this computer unless the internet is on. Today is {today}."]
    if folder:
        lines.append(f"Project folder: {folder} -- every path you write is relative to it: `index.html`, not "
                     f"`{folder.name}/index.html`. Commands run in it ({shell_name()}).")
        runnable = checks.env_tools()
        if runnable:
            lines.append("This computer can run: " + ", ".join(runnable) + ".")
    elif files and workspace:
        lines.append(f"No folder is connected. A new project gets its own folder in {workspace} when you plan it.")
    else:
        lines.append("No folder is connected, so you cannot see or change the user's files. If they ask you to, "
                     "tell them to connect one with the folder button at the top of the window.")
    lines.append(f"Internet: on (via {web_label}). Search when a question needs current or outside facts."
                 if web_label else "Internet: off. You cannot look things up; say so when a question needs it.")
    if not files and not tools:
        return "\n".join(lines)
    lines += ["", "# How you work",
              "You do the work yourself, with tools, one JSON call at a time. Never tell the user to do it."]
    if files:
        lines += [
            "- A question: answer it with reply (read the files first if you need to).",
            "- Anything to build, change or fix: call plan first. Its arguments:",
            "  goal: the request, in one sentence;",
            "  folder: for a new project, a short name for its own folder (letters, digits, dashes); \"\" to work "
            "on the files already in the project folder;",
            "  steps: in order -- one step for each file to create or change (do: create or edit; path; detail: one "
            "sentence on what the file does, naming the ids and functions it shares with the other files -- no "
            "code), then one step to run or open the result (do: run; path: \"\"; detail: what to run or open).",
            "  The steps are then carried out one by one, and each file is written in full at its step.",
            "- Write complete, working code: a game is playable, an app works. Never leave \"...\", TODO or "
            "\"add the rest here\".",
            "- Binary files (.exe, archives, images) cannot be read and are not code: leave them alone.",
            "- Only say something happened when a tool result shows it.",
        ]
        if unfinished is not None:
            lines.append("- Your last turn left a plan unfinished (below). If the user wants it carried on, call "
                         "resume.\n" + unfinished.text())
        if plan_only:
            lines.append("- This turn is for planning only: look around if you need to, then call plan.")
    lines += ["", "# Tools"]
    lines += [f"- {tool.signature()}: {tool.describe}" for tool in tools]
    if files:
        lines.append("- plan(goal, folder, steps): start a task to build, change or fix something")
    lines.append("- reply(): answer the user; it ends your turn")
    return "\n".join(lines)


def folder_glance(folder: Path, limit: int = 30) -> str:
    try:
        items = sorted(folder.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError:
        return ""
    names = []
    for p in items:
        if p.name.startswith(".") and p.name not in (".env.example", ".gitignore"):
            continue
        if p.is_dir():
            names.append(p.name + "/")
        else:
            names.append(p.name + (" (binary)" if p.suffix.lower() in T.BINARY_EXT else ""))
    more = f", … {len(names) - limit} more" if len(names) > limit else ""
    return ", ".join(names[:limit]) + more if names else "(empty)"


def actions_note(message: dict) -> str:
    """What a previous turn did, in a line the next turn can read."""
    steps = message.get("steps") or []
    done = [s["summary"] for s in steps if s.get("summary") and s.get("status") not in ("refused", "error")]
    refused = [s["summary"] for s in steps if s.get("status") == "refused"]
    parts = []
    plan = message.get("plan")
    if isinstance(plan, dict) and plan.get("steps"):
        finished = sum(1 for s in plan["steps"] if s.get("status") == "done")
        parts.append(f"plan \"{plan.get('goal', '')[:80]}\": {finished} of {len(plan['steps'])} steps done")
    if done:
        parts.append("done: " + "; ".join(done[:10]))
    if refused:
        parts.append("refused by the user: " + "; ".join(refused[:4]))
    if message.get("folder"):
        parts.append(f"in {message['folder']}")
    return f"\n\n({' | '.join(parts)})" if parts else ""


def partial_strings(text: str, keys: tuple[str, ...]) -> dict[str, str]:
    """The string arguments of a JSON object that is still being written."""
    found = {}
    for key in keys:
        match = re.search(r'"%s"\s*:\s*"' % re.escape(key), text)
        if not match:
            continue
        raw, index, escaped = [], match.end(), False
        while index < len(text):
            char = text[index]
            if escaped:
                raw.append(char)
                escaped = False
            elif char == "\\":
                raw.append(char)
                escaped = True
            elif char == '"':
                break
            else:
                raw.append(char)
            index += 1
        body = "".join(raw)
        for trim in range(0, 7):
            try:
                found[key] = json.loads('"' + (body[:len(body) - trim] if trim else body) + '"')
                break
            except ValueError:
                continue
    return found


EDIT_HOW = (
    "Write only the change, as one or more blocks like this:\n"
    "<<<<<<< SEARCH\n(the lines as they are now, copied exactly)\n=======\n(the lines to put instead)\n"
    ">>>>>>> REPLACE\n"
    "If most of the file changes, write the whole new file in one code block instead."
)


class Turn:
    """One message from the person, carried through to one answer."""

    def __init__(self, *, llama: Llama, profile: Profile, settings: dict, folder: Path | None,
                 inside: Callable[[str], Path] | None, web: Any, history: list[dict], request: str,
                 plan: bool, emit: Callable[[str, dict], None], cancel: threading.Event,
                 ask: Callable[[str, dict], str], needs_asking: Callable[[str], bool],
                 chips: str = "", first: bool = False, adapter: float = 1.0,
                 workspace: Path | None = None, on_folder: Callable[[Path], None] | None = None,
                 resume: dict | None = None, procs: Any = None, turn_id: str | None = None) -> None:
        self.llama, self.profile, self.settings = llama, profile, settings
        self.folder = folder.resolve() if folder else None
        self.web = web
        self.history, self.request, self.plan_only = history, request, plan
        self.emit, self.cancel, self.ask, self.needs_asking = emit, cancel, ask, needs_asking
        self.chips, self.first, self.adapter = chips, first, adapter
        self.workspace, self.on_folder, self.procs = workspace, on_folder, procs
        self.id = turn_id or uuid.uuid4().hex[:12]
        self.apex = profile.engine == "apex"
        agent = settings.get("agent") or {}
        self.files_on = bool(agent.get("files", True)) and (self.folder is not None or workspace is not None)
        self.commands_on = bool(agent.get("commands", True))
        self.max_actions = int(agent.get("max_steps", 40))
        self.auto_files = settings.get("permission") == "auto"
        self.resume_plan = Plan.from_dict(resume) if isinstance(resume, dict) and resume.get("steps") else None
        self.unfinished = self._last_unfinished() if self.resume_plan is None else None
        self.ctx = T.Context(folder=self.folder, inside=self._within, web=web, cancel=cancel,
                             emit=lambda event, data: None,
                             command_timeout=float(agent.get("command_timeout", T.FOREGROUND)),
                             before_change=self._before_change, procs=procs)
        self.steps: list[dict] = []      # every action, as the conversation keeps it
        self.plan: Plan | None = None
        self.plan_approved = False
        self.touched: list[str] = []
        self.problems: list[str] = []
        self.checkpoint = None
        self.waited = 0.0                # time spent waiting for the person, kept out of the timings
        self.think = ""
        self.tokens = 0
        self.gen_seconds = 0.0
        self.actions = 0
        self.ran_out = False
        self._last_progress = 0.0
        self._ending: dict = {}
        self.transcript: list[dict] = []
        self._prefix_said = ""
        self._made_folder = False
        self._pending: list[str] = []    # notes for the next user message
        self._shown: dict[str, str] = {}  # file -> the content the conversation last showed of it
        self._file_messages: list[tuple[int, str]] = []
        self._n_ctx = 0
        self.language = language_of(request)
        if self.apex:
            from nimbus2.controls import Controls

            self.controls = Controls(thinking_level=int(settings.get("level", 5)),
                                     temperature=int(settings.get("temperature", 5)))
            self.sampling = self._apex_sampling()
        else:
            self.controls = None
            self.sampling = dial_sampling(int(settings.get("temperature", 5)))
        self.code_sampling = Sampling(temperature=min(self.sampling.temperature, 0.5), top_p=0.95,
                                      top_k=40, min_p=0.05, repeat_penalty=1.0)

    # -- the folder ---------------------------------------------------------------------

    @property
    def tools(self) -> list[T.Tool]:
        """The tools this turn has, before it knows which phase it is in."""
        chosen = T.available(self.folder is not None and self.files_on, self.web is not None)
        if not self.commands_on:
            chosen = [t for t in chosen if t.name != "run_command"]
        if self.plan_only:
            chosen = [t for t in chosen if not t.gated]
        return chosen

    def _within(self, relative: str) -> Path:
        """A path as the model wrote it, made into the one it meant, then checked.

        Told it is working in `.../demo`, a small model writes `demo/hello.py`
        as often as `hello.py`. When the folder holds no `demo` of its own, the
        name is the folder itself and is dropped.
        """
        if self.folder is None:
            raise RuntimeError("there is no project folder yet")
        text = str(relative or ".").strip().strip("'\"`").replace("\\", "/")
        name = self.folder.name
        if text == name:
            text = "."
        elif text.startswith(name + "/") and not (self.folder / name).exists():
            text = text[len(name) + 1:]
        while text.startswith("./"):
            text = text[2:]
        return within(self.folder, text or ".")

    _inside = _within

    def _normalise(self, name: str, args: dict) -> dict:
        """The same correction as _within, applied to what is shown and run."""
        if self.folder is None:
            return args
        own = self.folder.name
        if (self.folder / own).exists():
            return args
        fixed = dict(args)
        path = fixed.get("path")
        if isinstance(path, str):
            text = path.strip().replace("\\", "/")
            if text == own:
                fixed["path"] = "."
            elif text.startswith(own + "/"):
                fixed["path"] = text[len(own) + 1:]
        command = fixed.get("command")
        if name == "run_command" and isinstance(command, str):
            fixed["command"] = re.sub(r"(?<![\w.\-/\\])" + re.escape(own) + r"[/\\]", "", command)
        return fixed

    def _before_change(self, path: Path) -> None:
        if self.folder is None:
            return
        if self.checkpoint is None:
            from .checkpoints import Checkpoint

            self.checkpoint = Checkpoint(self.id, self.folder, made_folder=self._made_folder)
        self.checkpoint.before(path)

    def _target_folder(self, plan: Plan) -> Path | None:
        """Where the plan will be carried out, before anything is made: None for the folder as it is.

        A new project in a folder that already holds other things -- a games
        folder, a home folder -- gets a folder of its own inside it. An empty
        folder, or one already named for the project, is used as it is.
        """
        name = slug(plan.folder) or (slug(plan.goal) if self.folder is None else "")
        if self.folder is None:
            base = self.workspace or Path.home() / "AhoosAI Studio"
            name = name or "project-" + datetime.now().strftime("%Y%m%d-%H%M")
            target = base / name
            counter = 2
            while target.exists() and any(target.iterdir()):
                target = base / f"{name}-{counter}"
                counter += 1
            return target
        empty = not any(p for p in self.folder.iterdir() if not p.name.startswith("."))
        if not name or empty or self.folder.name.lower() == name:
            return None
        return self.folder / name

    def _enter(self, target: Path) -> None:
        """Make the project's folder -- only once the plan is approved -- and work there."""
        self._made_folder = not target.exists()
        target.mkdir(parents=True, exist_ok=True)
        self.folder = target.resolve()
        self.ctx.folder = self.folder
        self.emit("folder", {"path": str(self.folder)})
        if self.on_folder:
            self.on_folder(self.folder)

    def _last_unfinished(self) -> Plan | None:
        for message in reversed(self.history[-4:]):
            if message.get("role") == "assistant":
                plan = message.get("plan")
                if isinstance(plan, dict) and plan.get("steps"):
                    found = Plan.from_dict(plan)
                    return found if found.unfinished else None
                return None
        return None

    def _apex_sampling(self) -> Sampling:
        s = self.controls.sampling()
        return Sampling(temperature=s.temperature, top_p=s.top_p, top_k=s.top_k, min_p=s.min_p,
                        repeat_penalty=s.repetition_penalty)

    # -- the prompt ------------------------------------------------------------------------

    def sent_text(self, attachments: list[dict]) -> str:
        """The message exactly as the model reads it -- stored, so the next turn's
        prompt repeats it byte for byte and llama.cpp's cache still matches."""
        parts = []
        if self.first and self.folder:
            parts.append(f"[Files in the project folder: {folder_glance(self.folder)}]")
        for item in attachments:
            parts.append(f"[Attached file {item['name']}]\n```\n{item['text']}\n```")
        parts.append(self.request)
        return "\n\n".join(parts)

    def system_prompt(self) -> str:
        base = self.controls.system_prompt() if self.apex else self.profile.system
        if self.chips:
            base += "\n\n" + self.chips
        label = self.web.label() if self.web is not None else ""
        return base + "\n" + environment(self.profile, self.folder, label, self.tools, files=self.files_on,
                                         workspace=self.workspace, unfinished=self.unfinished,
                                         plan_only=self.plan_only)

    def messages(self, sent: str) -> list[dict]:
        messages = [{"role": "system", "content": self.system_prompt()}]
        keep = int(self.settings.get("memory", 6))
        past = self.history[-keep * 2:] if keep else []
        for message in past:
            if message.get("role") == "user":
                messages.append({"role": "user", "content": message.get("sent") or message.get("content", "")})
            elif message.get("role") == "assistant" and (message.get("content") or message.get("steps")):
                messages.append({"role": "assistant",
                                 "content": (message.get("content") or "").strip() + actions_note(message)})
        # A template wants turns to alternate; an unanswered message (a stopped
        # turn) would put two user turns together.
        cleaned: list[dict] = []
        for message in messages:
            if cleaned and cleaned[-1]["role"] == message["role"] == "user":
                cleaned[-1] = message
            else:
                cleaned.append(message)
        if cleaned[-1]["role"] == "user":
            cleaned.pop()
        cleaned.append({"role": "user", "content": sent})
        return cleaned

    def render(self, messages: list[dict]) -> str:
        if self.apex:
            from .apex import chatml

            return chatml(messages) + "<|im_start|>assistant\n"
        return self.llama.apply_template(messages)

    def _prefix(self) -> str:
        return "<think>\n\n</think>\n\n" if self.apex else ""

    # -- the conversation within the turn ----------------------------------------------------

    def _say(self, text: str) -> None:
        """A message to the model, with whatever notes are waiting folded in."""
        body = "\n\n".join(self._pending + [text]) if self._pending else text
        self._pending = []
        if self.transcript and self.transcript[-1]["role"] == "user":
            self.transcript[-1] = {"role": "user", "content": self.transcript[-1]["content"] + "\n\n" + body}
        else:
            self.transcript.append({"role": "user", "content": body})

    def _note(self, text: str) -> None:
        self._pending.append(text)

    def _answered(self, content: str, file: str = "") -> None:
        self.transcript.append({"role": "assistant", "content": content})
        if file:
            self._file_messages.append((len(self.transcript) - 1, file))

    def _call_said(self, name: str, args: dict, prefix: str = "") -> None:
        shown = {k: v for k, v in args.items() if k not in ("content", "new")}
        call = json.dumps({"name": name, "arguments": shown} if shown or name not in (T.REPLY, T.RESUME)
                          else {"name": name}, ensure_ascii=False)
        self._answered(f"{prefix}<tool_call>\n{call}\n</tool_call>")

    def _tool_said(self, text: str) -> None:
        if len(text) > MAX_RESULT:
            text = text[:MAX_RESULT] + "\n… (cut)"
        self._say(f"<tool_response>\n{text}\n</tool_response>")

    # -- generation ------------------------------------------------------------------------

    def _context(self) -> int:
        if not self._n_ctx:
            try:
                self._n_ctx = int(self.llama.context_size())
            except Exception:  # noqa: BLE001 - an old server or the echo engine
                self._n_ctx = 8192
        return self._n_ctx

    def _count(self, text: str) -> int:
        try:
            return int(self.llama.count(text))
        except Exception:  # noqa: BLE001
            return len(text) // 3

    def _room(self, prompt: str, wanted: int) -> int:
        """Tokens that can still be generated after this prompt, up to `wanted`."""
        return max(64, min(wanted, self._context() - self._count(prompt) - 48))

    def _progress(self, done: int, total: int) -> None:
        now = time.monotonic()
        if now - self._last_progress > 0.3 or done >= total:
            self._last_progress = now
            self.emit("progress", {"done": done, "total": total})

    def _stream(self, prompt: str, **options):
        ending: dict = {}
        started = time.monotonic()
        yield from self.llama.stream(prompt, sampling=options.pop("sampling", self.sampling),
                                     adapter=self.adapter, cancel=self.cancel, ending=ending,
                                     on_prompt=self._progress, **options)
        timings = ending.get("timings") or {}
        self.tokens += int(timings.get("predicted_n") or 0)
        self.gen_seconds += float(timings.get("predicted_ms") or 0) / 1000 or (time.monotonic() - started)
        self._ending = ending

    def _limited(self) -> bool:
        return self._ending.get("stop_type") == "limit" or bool(self._ending.get("stopped_limit"))

    def _compact(self) -> None:
        """Make room: old file bodies become a line saying where they are.

        Done once the conversation passes COMPACT_AT of the context. The newest
        two files stay whole -- they are the ones the next file is most likely
        to need -- and anything can be read again from disk.
        """
        prompt = self.render(self.transcript)
        if self._count(prompt) < self._context() * COMPACT_AT:
            return
        for index, file in self._file_messages[:-2]:
            if index < len(self.transcript) and not self.transcript[index]["content"].startswith("(the file"):
                self.transcript[index] = {"role": "assistant",
                                          "content": f"(the file `{file}` was written here; it is on disk, and "
                                                     f"read_file shows it)"}
                self._shown.pop(file, None)
        for index, message in enumerate(self.transcript[:-4]):
            if message["role"] == "user" and index > 1 and len(message["content"]) > 1200:
                self.transcript[index] = {"role": "user", "content": message["content"][:600] + "\n… (cut)"}
        self.emit("compacted", {})

    def _think(self, prompt: str) -> str:
        """Apex's reasoning, up to the level's ceiling."""
        said: list[str] = []
        limit = self.controls.think_token_limit(self.request)
        for piece in self._stream(prompt + "<think>\n", n_predict=limit, stop=["</think>", "<|im_end|>"]):
            said.append(piece)
            self.emit("think", {"text": piece})
        reasoning = "".join(said).strip()
        from nimbus2.controls import reasoning_words

        self.emit("think_end", {"words": len(reasoning.split()), "forced": self._limited(),
                                "target": reasoning_words(self.controls.thinking_level)})
        self.think = reasoning
        return reasoning

    def _decide(self, schema: dict, call_id: str, prefix: str = "", n_predict: int = DECISION_TOKENS
                ) -> tuple[str, dict] | None:
        """One grammar-bound call. The UI learns which tool as soon as its name is written."""
        prompt = self.render(self.transcript) + (prefix or self._prefix())
        text = ""
        announced = False
        last = 0.0
        cooler = Sampling(**{**self.sampling.__dict__, "temperature": min(self.sampling.temperature, 0.5)})
        for piece in self._stream(prompt + "<tool_call>\n", n_predict=self._room(prompt, n_predict),
                                  json_schema=schema, stop=["</tool_call>"], sampling=cooler):
            text += piece
            if not announced:
                named = re.search(r'"name"\s*:\s*"([a-z_]+)"', text)
                if named and named.group(1) not in (T.REPLY, T.STEP_DONE, T.RESUME):
                    announced = named.group(1)
                    if announced == T.PLAN:
                        self.emit("planning", {})
                    else:
                        self.emit("tool_start", {"id": call_id, "name": announced})
            now = time.monotonic()
            if announced and now - last > 0.3:
                last = now
                if announced == T.PLAN:
                    args = partial_strings(text, ("goal",))
                    paths = re.findall(r'"path"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
                    self.emit("planning", {"goal": args.get("goal", "")[:300], "paths": paths[:14]})
                else:
                    args = partial_strings(text, ("path", "command", "query", "url", "change"))
                    self.emit("tool_args", {"id": call_id, **{k: v[:300] for k, v in args.items()}})
        try:
            call = json.loads(text)
        except ValueError:
            return None
        if not isinstance(call, dict):
            return None
        args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        return str(call.get("name") or ""), args

    def _free(self, prompt: str, n_predict: int, *, stops: list[str] | None = None, sampling: Sampling | None = None,
              on_piece: Callable[[str], None] | None = None) -> str:
        said = []
        for piece in self._stream(prompt, n_predict=self._room(prompt, n_predict), stop=stops or [],
                                  sampling=sampling or self.sampling):
            said.append(piece)
            if on_piece:
                on_piece(piece)
        return "".join(said)

    def _reply(self, prompt: str, limit: int | None = None) -> str:
        """Free text, streamed; a `<think>` block at its start goes to the reasoning panel."""
        said = ""
        thinking = None
        limit = limit or (self.controls.max_answer_tokens if self.apex else ANSWER_TOKENS)
        stops = ["<|im_end|>"] if self.apex else []
        stops.append("<tool_call>")
        buffer = ""
        for piece in self._stream(prompt, n_predict=self._room(prompt, limit), stop=stops):
            if thinking is None:
                buffer += piece
                if len(buffer) < 8 and "<think>".startswith(buffer.lstrip()[:7]):
                    continue
                if buffer.lstrip().startswith("<think>"):
                    thinking = True
                    piece = buffer.lstrip()[len("<think>"):]
                else:
                    thinking = False
                    piece = buffer
            if thinking:
                if "</think>" in piece:
                    before, after = piece.split("</think>", 1)
                    if before:
                        self.think += before
                        self.emit("think", {"text": before})
                    self.emit("think_end", {"words": len(self.think.split()), "forced": False})
                    thinking = False
                    piece = after.lstrip("\n")
                    if not piece:
                        continue
                else:
                    self.think += piece
                    self.emit("think", {"text": piece})
                    continue
            said += piece
            self.emit("delta", {"text": piece})
        return said.strip()

    # -- acting --------------------------------------------------------------------------

    def _asks(self, tool: T.Tool, args: dict) -> bool:
        """Whether this action is put to the person first."""
        if not tool.gated:
            return False
        if tool.name in ("write_file", "edit_file"):
            if self.auto_files:
                return False
            if self.plan_approved and self.plan is not None:
                path = str(args.get("path") or "").replace("\\", "/").lstrip("./")
                if any(s.path == path for s in self.plan.steps) or path in self.touched:
                    return False
        key = tool.key(args) if tool.key else tool.name
        return self.needs_asking(key)

    def _execute(self, tool: T.Tool, args: dict, call_id: str) -> tuple[T.Result, str]:
        self.ctx.emit = lambda event, data: self.emit(event, {"id": call_id, **data})
        if self._asks(tool, args):
            key = tool.key(args) if tool.key else tool.name
            try:
                shown = T.preview(self.ctx, tool, args)
            except (RuntimeError, OSError) as error:
                return T.Result(False, f"Error: {error}", f"{tool.name} failed: {error}"), "error"
            asked_at = time.monotonic()
            answer = self.ask(key, {"id": call_id, "name": tool.name, **shown})
            self.waited += time.monotonic() - asked_at
            if answer == "deny":
                return (T.Result(False, "The user refused this action. Do not try it again this turn; "
                                        "go on without it, or finish and ask what they would like instead.",
                                 f"{tool.name} {self._target(args)}".strip()), "refused")
        try:
            if tool.name == "edit_file" and "new" in args:
                result = T.replace_file(self.ctx, self._within(args["path"]), args["new"])
            else:
                result = tool.run(self.ctx, args)
        except Exception as error:  # noqa: BLE001 - the model is told, and so is the person
            return T.Result(False, f"Error: {error}", f"{tool.name} failed: {error}"), "error"
        return result, "done" if result.ok else "error"

    @staticmethod
    def _target(args: dict) -> str:
        for key in ("path", "command", "query", "url"):
            if args.get(key):
                return str(args[key])[:80]
        return ""

    def _record(self, call_id: str, name: str, args: dict, result: T.Result, status: str) -> None:
        kept = {k: (v[:4000] + "…" if isinstance(v, str) and len(v) > 4000 else v) for k, v in args.items()
                if k != "new"}
        step = {"id": call_id, "name": name, "args": kept, "ok": result.ok, "status": status,
                "summary": result.summary, "output": result.output[:20000], "meta": result.meta}
        self.steps.append(step)
        self.emit("tool_result", step)
        path = result.meta.get("path") if result.ok else None
        if path and name in ("write_file", "edit_file") and path not in self.touched:
            self.touched.append(path)

    def _tick(self) -> None:
        self.actions += 1
        if self.cancel.is_set():
            raise Cancelled()

    # -- writing files ---------------------------------------------------------------------

    def _compose(self, path: str, call_id: str) -> tuple[str, bool]:
        """The file, written as a code block, continued while it runs into the limit."""
        opening = writer.opening(path)
        prompt = self.render(self.transcript) + self._prefix() + opening
        text = ""
        last = [0.0]

        def show(piece: str) -> None:
            nonlocal text
            text += piece
            now = time.monotonic()
            if now - last[0] > 0.25:
                last[0] = now
                self.emit("tool_args", {"id": call_id, "path": path, "chars": len(text), "tail": text[-900:]})

        cut = False
        for stretch in range(FILE_STRETCHES):
            before = len(text)
            self._free(prompt + text, FILE_TOKENS, stops=[writer.closing(path)], sampling=self.code_sampling,
                       on_piece=show)
            cut = self._limited()
            if not cut or len(text) == before:
                break
            if self._room(prompt + text, 256) < 200:
                break
            self.emit("continued", {"id": call_id, "stretch": stretch + 2})
        self.emit("tool_args", {"id": call_id, "path": path, "chars": len(text), "tail": text[-900:]})
        content = writer.clean_file(text, path)
        self._answered(opening + content.rstrip("\n") + writer.closing(path), file=path)
        self._shown[path] = content
        return content, cut

    def _write(self, path: str, instruction: str) -> bool:
        """A file step: the instruction, the file written, the file saved."""
        path = self._normalise("write_file", {"path": path}).get("path", path)
        call_id = uuid.uuid4().hex[:8]
        self.emit("tool_start", {"id": call_id, "name": "write_file"})
        self.emit("tool_args", {"id": call_id, "path": path})
        self._say(instruction + "\nWrite the whole file now, in one code block: complete and working, with "
                  "everything the user asked for that belongs in this file -- no placeholders, nothing left for "
                  "later. Use the names (ids, functions, files) the other files of this project use.")
        self._tick()
        content, cut = self._compose(path, call_id)
        tool = T.by_name("write_file")
        if not content.strip():
            result, status = T.Result(False, f"Nothing was written for {path}.", f"{path}: nothing written"), "error"
        else:
            self.emit("tool_call", {"id": call_id, "name": "write_file", "args": {"path": path}})
            result, status = self._execute(tool, {"path": path, "content": content}, call_id)
        self._record(call_id, "write_file", {"path": path}, result, status)
        note = result.text
        if cut:
            note += " It was cut off at the length limit: the end is missing."
            self.problems.append(f"{path}: cut off at the length limit")
        left = writer.placeholders(content)
        if left and status == "done":
            self.problems.append(f"{path}: has placeholders instead of code: " + "; ".join(left[:3]))
        self._note(note)
        return status == "done" and not cut

    def _edit(self, path: str, instruction: str) -> bool:
        """A change to an existing file, as SEARCH/REPLACE blocks or a whole new file."""
        path = self._normalise("edit_file", {"path": path}).get("path", path)
        target = self._within(path)
        if not target.is_file():
            return self._write(path, instruction)
        if T.is_binary(target):
            self._note(f"{path} is a binary file and cannot be edited.")
            return False
        current = T.read_text(target)
        call_id = uuid.uuid4().hex[:8]
        self.emit("tool_start", {"id": call_id, "name": "edit_file"})
        self.emit("tool_args", {"id": call_id, "path": path})
        fence = writer.opening(path)
        for attempt in range(2):
            self._tick()
            if self._shown.get(path) == current:
                shown = f"(`{path}` is as written above.)"
            else:
                shown = f"`{path}` now:\n{fence}{current.rstrip()}{writer.closing(path)}"
                self._shown[path] = current
            self._say(f"{instruction}\n\n{shown}\n\n{EDIT_HOW}" if attempt == 0 else instruction)
            text = ""
            last = [0.0]

            def show(piece: str) -> None:
                nonlocal text
                text += piece
                now = time.monotonic()
                if now - last[0] > 0.25:
                    last[0] = now
                    self.emit("tool_args", {"id": call_id, "path": path, "chars": len(text), "tail": text[-900:]})

            out = self._free(self.render(self.transcript) + self._prefix(), EDIT_TOKENS,
                             stops=["<tool_call>"], sampling=self.code_sampling, on_piece=show)
            self._answered(out.strip())
            edits = writer.parse_edits(out)
            new, problems = current, []
            if edits:
                new, problems = writer.apply_edits(current, edits)
            else:
                block = re.search(r"```[\w+#.-]*\n(.*?)\n```", out, re.S)
                whole = writer.clean_file(block.group(1), path) if block else ""
                if whole and (whole.count("\n") >= current.count("\n") * 0.5 or current.count("\n") < 30):
                    new = whole
                else:
                    problems = ["no SEARCH/REPLACE blocks were found, and no whole file"]
            if new != current:
                tool = T.by_name("edit_file")
                self.emit("tool_call", {"id": call_id, "name": "edit_file", "args": {"path": path}})
                result, status = self._execute(tool, {"path": path, "new": new}, call_id)
                self._record(call_id, "edit_file", {"path": path}, result, status)
                if status == "done":
                    self._shown.pop(path, None)
                    extra = f" {len(problems)} block(s) could not be placed: " + "; ".join(problems) if problems else ""
                    self._note(result.text + extra)
                    return not problems
                self._note(result.text)
                return False
            instruction = ("That change could not be applied: " + "; ".join(problems) + ". Write it again: copy "
                           "the SEARCH lines exactly from the file, or write the whole new file in one code block.")
        result = T.Result(False, f"The change to {path} could not be applied.", f"edit of {path} did not apply")
        self._record(call_id, "edit_file", {"path": path}, result, "error")
        self._note(result.text)
        return False

    # -- the phases ------------------------------------------------------------------------

    def _look(self) -> str:
        """Read if needed, then decide: "reply", "plan" or "resume"."""
        read = [t for t in self.tools if t.name in T.READ_ONLY]
        seen: dict[str, int] = {}
        for step in range(LOOK_STEPS + 1):
            prefix = self._prefix()
            if self.apex and step == 0:
                prefix = "<think>\n" + self._think(self.render(self.transcript)) + "\n</think>\n\n"
            if step == LOOK_STEPS:
                return "reply"
            schema = T.decision_schema(read, plan=self.files_on, resume=self.unfinished is not None)
            call_id = uuid.uuid4().hex[:8]
            self._tick()
            decided = self._decide(schema, call_id, prefix,
                                   n_predict=PLAN_TOKENS if self.files_on else DECISION_TOKENS)
            if decided is None or decided[0] == T.REPLY:
                self._prefix_said = prefix
                return "reply"
            name, args = decided
            if name == T.PLAN:
                plan = plan_from_args(args)
                if not plan.steps:
                    return "reply"
                plan.request = self.request.strip()[:200]
                self._call_said(T.PLAN, args, prefix)
                self.plan = plan
                return "plan"
            if name == T.RESUME and self.unfinished is not None:
                self._call_said(T.RESUME, {}, prefix)
                self.plan = self.unfinished
                return "resume"
            tool = T.by_name(name)
            if tool is None or tool not in read:
                return "reply"
            args = self._normalise(name, args)
            signature = json.dumps([name, args], sort_keys=True, ensure_ascii=False)
            seen[signature] = seen.get(signature, 0) + 1
            self._call_said(name, args, prefix)
            if seen[signature] > 1:
                if seen[signature] > 2:
                    return "reply"
                self._tool_said("You already did exactly this; the result is above. Do the next thing.")
                continue
            self.emit("tool_call", {"id": call_id, "name": name, "args": {
                k: (v[:300] if isinstance(v, str) else v) for k, v in args.items()}})
            result, status = self._execute(tool, args, call_id)
            self._record(call_id, name, args, result, status)
            self._tool_said(result.text)
        return "reply"

    def _approve_plan(self) -> bool:
        """In "ask" mode the plan is put to the person once, instead of every file in it."""
        if self.auto_files:
            self.plan_approved = True
            return True
        # Continuing a plan that was approved and already partly carried out is
        # not a new question.
        if self.resume_plan is not None and any(s.status == "done" for s in self.resume_plan.steps):
            self.plan_approved = True
            return True
        if not self.needs_asking("plan"):
            self.plan_approved = True
            return True
        asked_at = time.monotonic()
        answer = self.ask("plan", {"id": "plan-" + self.id, "name": "plan", "title": "Carry out this plan?",
                                   "plan": self.plan.to_dict(), "folder": str(self.folder or "")})
        self.waited += time.monotonic() - asked_at
        self.plan_approved = answer != "deny"
        return self.plan_approved

    def _set(self, index: int, status: str, note: str = "") -> None:
        step = self.plan.steps[index]
        step.status, step.note = status, note or step.note
        self.emit("plan_step", {"index": index, "status": status, "note": step.note})

    def _step_text(self, index: int) -> str:
        """One step, said with the request beside it.

        The request is at the top of the conversation, thousands of tokens back
        by the third file; a small model writes to what it read last. Said again
        here, the score and the restart button the person asked for are in the
        game's script and not only in the plan.
        """
        step = self.plan.steps[index]
        asked = (self.plan.request or self.request).strip()
        asked = asked if len(asked) <= 400 else asked[:400] + "…"
        if step.do in ("create", "edit") and step.path:
            verb = "Write" if step.do == "create" else "Change"
            head = f"Step {index + 1} of {len(self.plan.steps)}: {verb.lower()} `{step.path}`."
            return f"{head}\nWhat it does: {step.detail}\nThe user asked for: {asked}"
        return f"Step {index + 1} of {len(self.plan.steps)}: {step.detail or step.title}\nThe user asked for: {asked}"

    def _act(self, instruction: str, allowed: tuple[str, ...], limit: int) -> bool:
        """A step that is not one file: tool calls until the model says it is done."""
        self._say(instruction + "\nDo it with tools; call step_done when it is done (or cannot be done).")
        tools = [t for t in self.tools if t.name in allowed]
        seen: dict[str, int] = {}
        ok = True
        for _ in range(limit):
            if self.actions >= self.max_actions:
                self.ran_out = True
                return False
            call_id = uuid.uuid4().hex[:8]
            self._tick()
            decided = self._decide(T.decision_schema(tools, step_done=True, reply=False), call_id)
            if decided is None:
                return ok
            name, args = decided
            if name == T.STEP_DONE:
                self._call_said(T.STEP_DONE, args)
                note = str(args.get("note") or "").strip()
                if note:
                    self._note(f"(noted: {note[:200]})")
                return ok
            tool = T.by_name(name)
            if tool is None or tool not in tools:
                return ok
            args = self._normalise(name, args)
            signature = json.dumps([name, args], sort_keys=True, ensure_ascii=False)
            seen[signature] = seen.get(signature, 0) + 1
            if seen[signature] > 2:
                return ok
            self._call_said(name, args)
            if name == "write_file":
                ok = self._write(str(args.get("path") or ""), f"Write `{args.get('path')}` now"
                                 + (f": {args['about']}" if args.get("about") else ".")) and ok
                continue
            if name == "edit_file":
                ok = self._edit(str(args.get("path") or ""), f"Make this change to `{args.get('path')}`: "
                                f"{args.get('change', '')}") and ok
                continue
            if seen[signature] > 1:
                self._tool_said("You already did exactly this; the result is above. Do the next thing, or call "
                                "step_done.")
                continue
            self.emit("tool_call", {"id": call_id, "name": name, "args": {
                k: (v[:300] if isinstance(v, str) else v) for k, v in args.items()}})
            result, status = self._execute(tool, args, call_id)
            self._record(call_id, name, args, result, status)
            ok = ok and status != "refused"
            self._tool_said(result.text)
        return ok

    def _build(self) -> None:
        """Carry out the plan, step by step."""
        plan = self.plan
        for index, step in enumerate(plan.steps):
            if step.status in ("done", "skipped"):
                continue
            if self.actions >= self.max_actions:
                self.ran_out = True
                break
            self._set(index, "active")
            self._compact()
            try:
                if step.do in ("create", "edit") and step.path:
                    target = self._within(step.path)
                    if step.do == "edit" and target.is_file():
                        ok = self._edit(step.path, self._step_text(index))
                    else:
                        ok = self._write(step.path, self._step_text(index))
                elif step.do == "run":
                    ok = self._act(self._step_text(index), T.WORK, 6)
                else:
                    ok = self._act(self._step_text(index), T.WORK, STEP_ACTIONS)
            except Cancelled:
                self._set(index, "pending")
                raise
            except RuntimeError as error:          # a path outside the folder, and the like
                self._note(f"Step {index + 1} could not be done: {error}")
                ok = False
            if self.ran_out and not ok:
                self._set(index, "pending")
                break
            self._set(index, "done" if ok else "failed")

    def _check(self) -> None:
        """Look at what was written without running it; hand what is wrong back, twice at most."""
        for round_number in range(CHECK_ROUNDS):
            if not self.touched or self.folder is None:
                return
            found = [p.text() for p in checks.check(self.folder, self.touched)]
            found += [p for p in self.problems if p not in found]
            self.problems = []
            self.emit("check", {"round": round_number + 1, "problems": found})
            if not found:
                return
            if self.actions >= self.max_actions:
                self.problems = found
                return
            index = len(self.plan.steps)
            title = "رفع ایرادهایی که بررسی پیدا کرد" if self.language == "Persian" else "Fix what the check found"
            self.plan.steps.append(Step(title=title, do="fix", detail="\n".join(found)))
            self.emit("plan", {"plan": self.plan.to_dict()})
            self._set(index, "active")
            # File by file, straight into an edit. Left to choose its own tools, a
            # small model calls the step done without touching anything; shown the
            # file and what is wrong with it, it writes the fix.
            ok = True
            for path, items in self._by_file(found).items():
                target = self._within(path) if path else None
                if target is not None and target.is_file():
                    ok = self._edit(path, f"A check of `{path}` found:\n- " + "\n- ".join(items)
                                    + f"\nFix every one of them in `{path}`.") and ok
                else:
                    ok = self._act("A check found:\n- " + "\n- ".join(items) + "\nFix it.", T.WORK, 4) and ok
            self._set(index, "done" if ok else "failed")
        if self.folder is not None and self.touched:
            self.problems = [p.text() for p in checks.check(self.folder, self.touched)]

    def _by_file(self, problems: list[str]) -> dict[str, list[str]]:
        """Problems grouped by the file they name ("game.js:12: ..." or "index.html: ...")."""
        grouped: dict[str, list[str]] = {}
        known = set(self.touched)
        for problem in problems:
            head, _, rest = problem.partition(": ")
            path = head.split(":", 1)[0].strip()
            if path not in known and not (self.folder and (self.folder / path).is_file()):
                path, rest = "", problem
            grouped.setdefault(path, []).append(rest or problem)
        return grouped

    def _finish(self) -> str:
        """The answer: what was made, and how to try it."""
        plan = self.plan
        done = [s for s in plan.steps if s.status == "done"]
        failed = [s for s in plan.steps if s.status == "failed"]
        pending = [s for s in plan.steps if s.status in ("pending", "active")]
        facts = [f"{len(done)} of {len(plan.steps)} steps are done."]
        if failed:
            facts.append("Not done: " + "; ".join(s.title for s in failed) + ".")
        if pending:
            facts.append("Left for later (the user can say continue): " + "; ".join(s.title for s in pending) + ".")
        if self.problems:
            facts.append("Problems still open: " + "; ".join(self.problems[:4]) + ".")
        if self.touched:
            facts.append("Files: " + ", ".join(self.touched[:12]) + f", in {self.folder}.")
        language = f" in {self.language}" if self.language else " in the user's language"
        self._say(" ".join(facts) + f"\nNow reply to the user{language}, briefly: what you made, the files, and "
                  "how to open or run it. Say plainly what is not done. No code.")
        return self._reply(self.render(self.transcript) + self._prefix(), SUMMARY_TOKENS)

    # -- the turn --------------------------------------------------------------------------

    def run(self, sent: str) -> dict:
        """Everything the turn produced, as the message to keep."""
        started = time.monotonic()
        answer = ""
        error = ""
        stopped = False
        try:
            answer = self._run(sent)
        except Cancelled:
            stopped = True
        except EngineError as problem:
            error = str(problem)
            self.emit("error", {"detail": error})
        except Exception as problem:  # noqa: BLE001 - kept with the turn, so its plan and files are not lost
            import traceback

            traceback.print_exc()
            error = f"{type(problem).__name__}: {problem}"
            self.emit("error", {"detail": error})
        message = {"role": "assistant", "content": answer, "model": self.profile.name,
                   "stats": {"seconds": round(time.monotonic() - started - self.waited, 1), "tokens": self.tokens,
                             "tps": round(self.tokens / self.gen_seconds, 2) if self.gen_seconds else 0}}
        if self.think:
            message["think"] = self.think
        if self.steps:
            message["steps"] = self.steps
        if self.plan is not None:
            message["plan"] = self.plan.to_dict()
            message["turn"] = self.id
        if self.checkpoint is not None and self.checkpoint.paths:
            message["checkpoint"] = self.id
            message["changes"] = self.checkpoint.paths
        if self.folder is not None and self.touched:
            message["folder"] = str(self.folder)
            message["try"] = checks.runnable(self.folder, self.touched)
        if self.problems:
            message["problems"] = self.problems[:10]
        if stopped:
            message["stopped"] = True
        if error:
            message["error"] = error
        if self.plan_only:
            message["plan_only"] = True
        if self.ran_out:
            message["ran_out"] = True
        return message

    def _run(self, sent: str) -> str:
        self.transcript = self.messages(sent)
        self._prefix_said = ""
        if not self.files_on and not self.tools:
            prompt = self.render(self.transcript)
            if self.apex:
                reasoning = self._think(prompt)
                return self._reply(prompt + "<think>\n" + reasoning + "\n</think>\n\n")
            return self._reply(prompt)

        if self.resume_plan is not None:
            self.plan = self.resume_plan
            self._call_said(T.RESUME, {})
            how = "resume"
        else:
            how = self._look()
        if how == "reply":
            prompt = self.render(self.transcript) + (getattr(self, "_prefix_said", "") or self._prefix())
            reply = self._reply(prompt)
            return reply

        if how == "resume" and self.folder is None and self.history:
            last = next((m.get("folder") for m in reversed(self.history) if m.get("folder")), "")
            if last and Path(last).is_dir():
                self.folder = Path(last).resolve()
                self.ctx.folder = self.folder
        target = self._target_folder(self.plan)
        unprefix(self.plan, (target or self.folder or Path()).name)
        self.emit("plan", {"plan": self.plan.to_dict(), "folder": str(target or self.folder or "")})
        if self.plan_only:
            return self._plan_reply()
        if not self._approve_plan():
            for index in range(len(self.plan.steps)):
                self._set(index, "skipped")
            self._say("<tool_response>\nThe user did not want this plan carried out.\n</tool_response>\n"
                      "Reply briefly: ask what they would like instead.")
            return self._reply(self.render(self.transcript) + self._prefix(), SUMMARY_TOKENS)
        if target is not None:
            self._enter(target)
        self._say(f"<tool_response>\nThe plan is set{' (resumed)' if how == 'resume' else ''}: "
                  f"{len(self.plan.steps)} steps, in {self.folder}. They are carried out one at a time.\n"
                  "</tool_response>")
        self._build()
        if not self.ran_out:
            self._check()
        return self._finish()

    def _plan_reply(self) -> str:
        lines = [self.plan.goal, ""]
        for number, step in enumerate(self.plan.steps, 1):
            target = f" — `{step.path}`" if step.path else ""
            lines.append(f"{number}. {step.title}{target}")
        text = "\n".join(lines)
        self.emit("delta", {"text": text})
        return text
