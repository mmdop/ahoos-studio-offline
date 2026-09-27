"""One turn of a conversation: the model reads, decides, acts, and answers.

WHY A MODEL THAT KNEW ITS TOOLS STILL ONLY PRINTED CODE

Asked to "make hello.py and run it", Nimbus 1.1 on a seven-billion-parameter
base, quantised to three bits, answered with a code block -- and once, with the
output of a run that never happened. Told about tools in its system prompt and
shown an example, it did the same. A small model's habit of answering in prose
is stronger than an instruction.

So the choice is not left to prose. Where a tool could help, each step of the
model's turn begins inside an open `<tool_call>`, and llama.cpp is handed a
grammar that admits exactly one thing: a JSON object naming a tool and its
arguments, or naming `reply`. The model still decides -- it picks `reply` for
"what is the capital of France" and `write_file` for "make hello.py" -- but it
decides by writing the decision, and the grammar makes the decision well-formed.
Only `reply` gives way to free text, and the answer is then written after the
tools, knowing what they did.

Measured on the model above: asked to make a file, it wrote the file; given the
result, it called run_command; asked a question, it replied. With no grammar,
none of the three.

WHERE IT IS

The system prompt says what it is running in, on which computer, which folder
it may touch, whether the internet is on, and what each tool does -- so "I
cannot access your files" stops being true or said when a folder is connected,
and "connect a folder first" is said when one is not.

WHAT IT MAY DO WITHOUT ASKING

Read. Anything that writes, deletes or runs is put to the person first, in the
conversation, with the diff or the command exactly as it will happen. The model
is told when it was refused.

THE COST OF A PROMPT

On the machines this runs on, the prompt is read at ten to forty tokens a
second. The tool list is kept to a line a tool, results are cut short, and the
prompt is laid out so the part that does not change -- the system prompt, the
conversation so far -- is a prefix llama.cpp has already read and cached.
"""

from __future__ import annotations

import json
import os
import platform
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import __version__, tools as T
from .engine import Cancelled, EngineError, Llama, Sampling, dial_sampling

DECISION_TOKENS = 4096           # a whole file, as the argument of write_file
ANSWER_TOKENS = 4096
MAX_RESULT = 3500                # characters of one tool result the model reads back
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


def environment(profile: Profile, folder: Path | None, web_label: str, tools: list[T.Tool],
                plan: bool) -> str:
    """What the model is told about where it is and what it can do."""
    today = datetime.now().strftime("%A %d %B %Y")
    lines = ["", "# Where you are",
             f"You are {profile.name}, running on the user's own computer ({os_name()}) inside "
             f"AhoosAI Studio {__version__}. Nothing you do leaves this computer unless the internet "
             f"is on. Today is {today}."]
    if folder:
        lines.append(f"Working folder: {folder} -- you can read, create and change files in it, and run "
                     f"commands there ({shell_name()}). Commands already run inside it, and paths are relative "
                     f"to it: write `hello.py`, not `{folder.name}/hello.py`.")
    else:
        lines.append("No working folder is connected, so you cannot see or change the user's files. If they "
                     "ask you to, tell them to connect one with the folder button at the top of the window.")
    if web_label:
        lines.append(f"Internet: on (via {web_label}). Search when a question needs current or outside facts.")
    else:
        lines.append("Internet: off. You cannot look things up; say so when a question needs it.")
    if not tools:
        return "\n".join(lines)
    lines += ["", "# Tools",
              "Each message you write is exactly one tool call in JSON. The tools:"]
    lines += [f"- {tool.signature()}: {tool.describe}" for tool in tools]
    lines.append("- reply(): write your answer to the user. It ends your turn.")
    lines += ["Rules:"]
    if any(t.name == "write_file" for t in tools):
        lines += ["- To make or change a file, call write_file or edit_file. Do not paste its code in a reply.",
                  "- To run something -- a script, tests, an install -- call run_command yourself.",
                  "- Every change and command is shown to the user to approve. If refused, do not retry it."]
    elif folder:
        lines.append("- You may look but not change anything this turn.")
    lines += ["- Only say you did something after a tool result shows it happened.",
              "- When the work is done, reply: briefly, in the user's language, what you did and what came of it."]
    if plan:
        lines.append("- This turn is for planning: look around if you need to, then reply with the plan.")
    return "\n".join(lines)


def folder_glance(folder: Path, limit: int = 40) -> str:
    try:
        items = sorted(folder.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError:
        return ""
    names = [p.name + ("/" if p.is_dir() else "") for p in items if not p.name.startswith(".")
             or p.name in (".env.example", ".gitignore")]
    more = f", … {len(names) - limit} more" if len(names) > limit else ""
    return ", ".join(names[:limit]) + more if names else "(empty)"


def actions_note(steps: list[dict]) -> str:
    """What a previous turn did, in a line the next turn can read."""
    done = [s["summary"] for s in steps if s.get("summary") and s.get("status") != "refused"]
    refused = [s["summary"] for s in steps if s.get("status") == "refused"]
    parts = []
    if done:
        parts.append("done: " + "; ".join(done[:8]))
    if refused:
        parts.append("refused by the user: " + "; ".join(refused[:4]))
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


class Turn:
    """One message from the person, carried through to one answer."""

    def __init__(self, *, llama: Llama, profile: Profile, settings: dict, folder: Path | None,
                 inside: Callable[[str], Path], web: Any, history: list[dict], request: str,
                 plan: bool, emit: Callable[[str, dict], None], cancel: threading.Event,
                 ask: Callable[[str, dict], str], needs_asking: Callable[[str], bool],
                 chips: str = "", first: bool = False, adapter: float = 1.0) -> None:
        self.llama, self.profile, self.settings = llama, profile, settings
        self.folder, self.inside, self.web = folder, inside, web
        self.history, self.request, self.plan = history, request, plan
        self.emit, self.cancel, self.ask, self.needs_asking = emit, cancel, ask, needs_asking
        self.chips, self.first, self.adapter = chips, first, adapter
        self.apex = profile.engine == "apex"
        agent = settings.get("agent") or {}
        self.tools = T.available(folder is not None and agent.get("files", True),
                                 web is not None, read_only=plan)
        if not agent.get("commands", True):
            self.tools = [t for t in self.tools if t.name != "run_command"]
        self.max_steps = int(agent.get("max_steps", 10))
        self.ctx = T.Context(folder=folder, inside=self._inside, web=web, cancel=cancel,
                             emit=lambda event, data: None,
                             command_timeout=float(agent.get("command_timeout", 120)))
        self.steps: list[dict] = []
        self.waited = 0.0            # time spent waiting for the person, kept out of the timings
        self.think = ""
        self.tokens = 0
        self.gen_seconds = 0.0
        self._last_progress = 0.0
        if self.apex:
            from nimbus2.controls import Controls

            self.controls = Controls(thinking_level=int(settings.get("level", 5)),
                                     temperature=int(settings.get("temperature", 5)))
            self.sampling = self._apex_sampling()
        else:
            self.controls = None
            self.sampling = dial_sampling(int(settings.get("temperature", 5)))

    def _inside(self, relative: str) -> Path:
        """A path as the model wrote it, made into the one it meant, then checked.

        Told it is working in `.../demo`, a small model writes `demo/hello.py`
        as often as `hello.py`. When the folder holds no `demo` of its own, the
        name is the folder itself and is dropped. Every result still goes
        through app.inside, which refuses anything outside the folder.
        """
        text = str(relative or ".").strip().strip("'\"").replace("\\", "/")
        if self.folder is not None:
            name = self.folder.name
            if text == name:
                text = "."
            elif text.startswith(name + "/") and not (self.folder / name).exists():
                text = text[len(name) + 1:]
        while text.startswith("./"):
            text = text[2:]
        return self.inside(text or ".")

    def _normalise(self, name: str, args: dict) -> dict:
        """The same correction as _inside, applied to what is shown and run.

        A model that writes `demo/hello.py` also runs `python demo/hello.py`.
        Correcting the file and not the command would put the file where the
        command cannot find it; so both are corrected, before the person is
        asked, and what they approve is what runs.
        """
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

    def _apex_sampling(self) -> Sampling:
        s = self.controls.sampling()
        return Sampling(temperature=s.temperature, top_p=s.top_p, top_k=s.top_k, min_p=s.min_p,
                        repeat_penalty=s.repetition_penalty)

    # -- the prompt --------------------------------------------------------------

    def sent_text(self, attachments: list[dict]) -> str:
        """The message exactly as the model reads it -- stored, so the next turn's
        prompt repeats it byte for byte and llama.cpp's cache still matches."""
        parts = []
        if self.first and self.folder:
            parts.append(f"[Files in the working folder: {folder_glance(self.folder)}]")
        for item in attachments:
            parts.append(f"[Attached file {item['name']}]\n```\n{item['text']}\n```")
        parts.append(self.request)
        text = "\n\n".join(parts)
        return text + (PLAN_ASK if self.plan else "")

    def system_prompt(self) -> str:
        base = self.controls.system_prompt() if self.apex else self.profile.system
        if self.chips:
            base += "\n\n" + self.chips
        label = self.web.label() if self.web is not None else ""
        return base + "\n" + environment(self.profile, self.folder, label, self.tools, self.plan)

    def messages(self, sent: str) -> list[dict]:
        messages = [{"role": "system", "content": self.system_prompt()}]
        keep = int(self.settings.get("memory", 6))
        past = self.history[-keep * 2:] if keep else []
        for message in past:
            if message.get("role") == "user":
                messages.append({"role": "user", "content": message.get("sent") or message.get("content", "")})
            elif message.get("role") == "assistant" and (message.get("content") or message.get("steps")):
                messages.append({"role": "assistant",
                                 "content": (message.get("content") or "").strip()
                                 + actions_note(message.get("steps") or [])})
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

    # -- generation --------------------------------------------------------------

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

    def _think(self, prompt: str) -> str:
        """Apex's reasoning, up to the level's ceiling."""
        said: list[str] = []
        limit = self.controls.think_token_limit(self.request)
        for piece in self._stream(prompt + "<think>\n", n_predict=limit, stop=["</think>", "<|im_end|>"]):
            said.append(piece)
            self.emit("think", {"text": piece})
        reasoning = "".join(said).strip()
        from nimbus2.controls import reasoning_words

        forced = self._ending.get("stop_type") == "limit" or bool(self._ending.get("stopped_limit"))
        self.emit("think_end", {"words": len(reasoning.split()), "forced": forced,
                                "target": reasoning_words(self.controls.thinking_level)})
        self.think = reasoning
        return reasoning

    def _decide(self, prompt: str, schema: dict, call_id: str) -> tuple[str, dict] | None:
        text = ""
        announced = False
        last = 0.0
        cooler = Sampling(**{**self.sampling.__dict__, "temperature": min(self.sampling.temperature, 0.6)})
        for piece in self._stream(prompt + "<tool_call>\n", n_predict=DECISION_TOKENS, json_schema=schema,
                                  stop=["</tool_call>"], sampling=cooler):
            text += piece
            if not announced:
                named = re.search(r'"name"\s*:\s*"([a-z_]+)"', text)
                if named and named.group(1) != T.REPLY:
                    announced = True
                    self.emit("tool_start", {"id": call_id, "name": named.group(1)})
            now = time.monotonic()
            if announced and now - last > 0.25:
                last = now
                args = partial_strings(text, ("path", "command", "query", "url", "content", "find"))
                content = args.pop("content", None)
                data = {"id": call_id, **{k: v[:300] for k, v in args.items()}}
                if content is not None:
                    data.update(chars=len(content), tail=content[-700:])
                self.emit("tool_args", data)
        try:
            call = json.loads(text)
        except ValueError:
            if self._ending.get("stop_type") == "limit":
                return "_cut", {}
            return None
        return str(call.get("name") or ""), call.get("arguments") if isinstance(call.get("arguments"), dict) else {}

    def _reply(self, prompt: str) -> str:
        """Free text, streamed; a `<think>` block at its start goes to the reasoning panel."""
        said = ""
        thinking = None
        limit = self.controls.max_answer_tokens if self.apex else ANSWER_TOKENS
        stops = ["<|im_end|>"] if self.apex else []
        if self.tools:
            stops.append("</tool_call>")
        buffer = ""
        for piece in self._stream(prompt, n_predict=limit, stop=stops):
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

    # -- acting ------------------------------------------------------------------

    def _execute(self, tool: T.Tool, args: dict, call_id: str) -> tuple[T.Result, str]:
        self.ctx.emit = lambda event, data: self.emit(event, {"id": call_id, **data})
        if tool.gated:
            key = tool.key(args) if tool.key else tool.name
            if self.needs_asking(key):
                try:
                    shown = T.preview(self.ctx, tool, args)
                except (RuntimeError, OSError) as error:
                    return T.Result(False, f"Error: {error}", f"{tool.name} failed: {error}"), "error"
                asked_at = time.monotonic()
                answer = self.ask(key, {"id": call_id, "name": tool.name, **shown})
                self.waited += time.monotonic() - asked_at
                if answer == "deny":
                    return (T.Result(False, "The user refused this action. Do not try it again this turn; "
                                            "reply and ask what they would like instead.",
                                     f"{tool.name} {self._target(args)}".strip()), "refused")
        try:
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
        kept = {k: (v[:4000] + "…" if isinstance(v, str) and len(v) > 4000 else v) for k, v in args.items()}
        step = {"id": call_id, "name": name, "args": kept, "ok": result.ok, "status": status,
                "summary": result.summary, "output": result.output[:20000], "meta": result.meta}
        self.steps.append(step)
        self.emit("tool_result", step)

    # -- the turn ------------------------------------------------------------------

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
        message = {"role": "assistant", "content": answer, "model": self.profile.name,
                   "stats": {"seconds": round(time.monotonic() - started - self.waited, 1), "tokens": self.tokens,
                             "tps": round(self.tokens / self.gen_seconds, 2) if self.gen_seconds else 0}}
        if self.think:
            message["think"] = self.think
        if self.steps:
            message["steps"] = self.steps
        if stopped:
            message["stopped"] = True
        if error:
            message["error"] = error
        if self.plan:
            message["plan"] = True
        return message

    def _run(self, sent: str) -> str:
        messages = self.messages(sent)
        base_prompt = self.render(messages)
        if not self.tools:
            if self.apex:
                reasoning = self._think(base_prompt)
                return self._reply(base_prompt + "<think>\n" + reasoning + "\n</think>\n\n")
            return self._reply(base_prompt)

        schema = T.decision_schema(self.tools)
        seen: dict[str, int] = {}
        prefix = ""
        for step in range(self.max_steps + 1):
            prompt = self.render(messages) if step else base_prompt
            if self.apex:
                if step == 0:
                    prefix = "<think>\n" + self._think(prompt) + "\n</think>\n\n"
                else:
                    prefix = "<think>\n\n</think>\n\n"
            if step == self.max_steps:
                break
            call_id = uuid.uuid4().hex[:8]
            decided = self._decide(prompt + prefix, schema, call_id)
            if decided is None or decided[0] == T.REPLY:
                break
            name, args = decided
            args = self._normalise(name, args)
            if name == "_cut":
                messages += [{"role": "assistant", "content": prefix + "<tool_call>\n(cut off)\n</tool_call>"},
                             {"role": "user", "content": "<tool_response>\nYour call was too long and was cut off. "
                              "Write a shorter file, or write it in parts with edit_file.\n</tool_response>"}]
                continue
            tool = T.by_name(name)
            if tool is None or tool not in self.tools:
                break
            signature = json.dumps([name, args], sort_keys=True, ensure_ascii=False)
            seen[signature] = seen.get(signature, 0) + 1
            if seen[signature] > 1:
                result, status = (T.Result(False, "You already did exactly this in this turn; the result is above. "
                                                  "Do not repeat it: do the next thing, or reply.",
                                           f"repeated {name}"), "error")
                if seen[signature] > 2:
                    break
            else:
                self.emit("tool_call", {"id": call_id, "name": name, "args": {
                    k: (v[:300] if isinstance(v, str) else v) for k, v in args.items() if k != "content"}})
                result, status = self._execute(tool, args, call_id)
                self._record(call_id, name, args, result, status)
            text = result.text if len(result.text) <= MAX_RESULT else result.text[:MAX_RESULT] + "\n… (cut)"
            call = json.dumps({"name": name, "arguments": args}, ensure_ascii=False)
            messages += [{"role": "assistant", "content": f"{prefix}<tool_call>\n{call}\n</tool_call>"},
                         {"role": "user", "content": f"<tool_response>\n{text}\n</tool_response>"}]
            if self.cancel.is_set():
                raise Cancelled()

        reply = self._reply(self.render(messages) + prefix)
        # A call written into the answer instead of made: made now, once, and
        # the answer written again after it -- rather than shown as JSON.
        found = T.call_in_text(reply) if self.tools else None
        if found and len(self.steps) < self.max_steps:
            name, args, rest = found
            args = self._normalise(name, args)
            tool = T.by_name(name)
            if tool in self.tools:
                self.emit("reply_reset", {"text": ""})
                call_id = uuid.uuid4().hex[:8]
                self.emit("tool_start", {"id": call_id, "name": name})
                self.emit("tool_call", {"id": call_id, "name": name, "args": {
                    k: (v[:300] if isinstance(v, str) else v) for k, v in args.items() if k != "content"}})
                result, status = self._execute(tool, args, call_id)
                self._record(call_id, name, args, result, status)
                call = json.dumps({"name": name, "arguments": args}, ensure_ascii=False)
                messages += [{"role": "assistant", "content": f"{prefix}<tool_call>\n{call}\n</tool_call>"},
                             {"role": "user", "content": f"<tool_response>\n{result.text[:MAX_RESULT]}\n"
                                                         "</tool_response>"}]
                reply = self._reply(self.render(messages) + ("<think>\n\n</think>\n\n" if self.apex else ""))
                again = T.call_in_text(reply)
                if again:
                    reply = again[2]
        return reply
