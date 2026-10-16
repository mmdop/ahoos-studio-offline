"""Online models: a service that speaks OpenAI's Chat Completions, a key for it, and a model.

The studio's own models run on this computer; these run elsewhere -- AhoosAI's own Nimbus 2 Apex
first, then the services most people have a key for. A chat with one is a conversation: the
reasoning when the service sends it, the answer as it comes, the web search AhoosAI's engine runs
on its side. Files and commands in a project folder stay with the model on this computer: the
studio's agent drives it under a grammar (agent.py), which no online service offers.

Keys are kept in this computer's settings file, beside the search keys, and are shown back to the
page masked, never whole. Every call goes through this computer's connection -- its VPN, or the
proxy set under Internet.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Callable, Iterator

from .today import FORMAT, today_text

PROVIDERS = {
    "ahoosai": {"name": "AhoosAI", "base": "https://ahoos-ai.site/services/api/v1",
                "site": "https://ahoos-ai.site/services/platforme/", "hint": "nim_…", "context": 40960,
                "models": ["nimbus-2-apex"], "apex": True, "usage": False},
    "openai": {"name": "OpenAI", "base": "https://api.openai.com/v1", "site": "https://platform.openai.com/api-keys",
               "hint": "sk-…", "context": 128000, "models": ["gpt-4o-mini", "gpt-4o"], "usage": True},
    "openrouter": {"name": "OpenRouter", "base": "https://openrouter.ai/api/v1", "site": "https://openrouter.ai/keys",
                   "hint": "sk-or-…", "context": 128000, "models": [], "usage": True},
    "groq": {"name": "Groq", "base": "https://api.groq.com/openai/v1", "site": "https://console.groq.com/keys",
             "hint": "gsk_…", "context": 32768, "models": [], "usage": True},
    "gemini": {"name": "Google Gemini", "base": "https://generativelanguage.googleapis.com/v1beta/openai",
               "site": "https://aistudio.google.com/apikey", "hint": "AIza…", "context": 1000000,
               "models": ["gemini-2.0-flash"], "usage": False},
    "deepseek": {"name": "DeepSeek", "base": "https://api.deepseek.com/v1", "site": "https://platform.deepseek.com/api_keys",
                 "hint": "sk-…", "context": 64000, "models": ["deepseek-chat", "deepseek-reasoner"], "usage": True},
    "custom": {"name": "Custom", "base": "", "site": "", "hint": "", "context": 32768, "models": [], "usage": False},
}

DEFAULT = {
    "keys": {},                  # provider -> key
    "base": "",                  # the custom service's address
    "models": {},                # provider -> the models put in the model picker
    "active": {"provider": "", "model": ""},   # set: chats go to this model instead of the local one
}

TIMEOUT = 900                    # an answer may take minutes; AhoosAI's first after a quiet spell, one or two
GENERIC_SYSTEM = ("You are a helpful, precise assistant. You answer in the language you are asked in, and you say "
                  "plainly when you do not know something.")


class OnlineError(RuntimeError):
    """The service refused, or could not be reached."""


def merged(stored: dict | None) -> dict:
    data = {**DEFAULT, **(stored or {})}
    data["keys"] = {k: str(v) for k, v in (data.get("keys") or {}).items() if k in PROVIDERS and v}
    data["models"] = {k: [str(m) for m in v][:30] for k, v in (data.get("models") or {}).items() if k in PROVIDERS}
    data["active"] = {"provider": str((data.get("active") or {}).get("provider") or ""),
                      "model": str((data.get("active") or {}).get("model") or "")}
    return data


def mask(key: str) -> str:
    return key[:4] + "…" + key[-3:] if len(key) > 10 else "set"


def public(settings: dict) -> dict:
    """What the page may see: which keys are set (masked), the models chosen, the one in use."""
    data = merged(settings)
    return {
        "providers": [{"id": pid, "name": p["name"], "site": p["site"], "hint": p["hint"], "context": p["context"],
                       "key": mask(data["keys"][pid]) if pid in data["keys"] else "",
                       "models": data["models"].get(pid) or p["models"], "apex": bool(p.get("apex"))}
                      for pid, p in PROVIDERS.items()],
        "base": data["base"],
        "active": data["active"] if data["active"]["provider"] in PROVIDERS and data["active"]["model"] else
        {"provider": "", "model": ""},
    }


class Online:
    """Calls to one service, through this computer's connection (or the Internet proxy)."""

    def __init__(self, settings: dict, provider: str, proxy: str = "") -> None:
        if provider not in PROVIDERS:
            raise OnlineError(f"unknown service {provider!r}")
        self.data = merged(settings)
        self.provider = provider
        self.spec = PROVIDERS[provider]
        self.base = (self.data["base"] if provider == "custom" else self.spec["base"]).rstrip("/")
        self.key = self.data["keys"].get(provider, "")
        self.proxy = proxy.strip()
        if not self.base:
            raise OnlineError("the custom service has no address yet (Settings → Online models)")
        if not self.key:
            raise OnlineError(f"no API key for {self.spec['name']} yet (Settings → Online models)")

    def _opener(self) -> urllib.request.OpenerDirector:
        handlers = [urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})] if self.proxy else []
        return urllib.request.build_opener(*handlers)

    def _request(self, path: str, body: dict | None = None, timeout: float = 30):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method="POST" if data else "GET", headers={
            "Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
            "Accept": "text/event-stream" if body and body.get("stream") else "application/json",
            "User-Agent": "AhoosAI-Studio"})
        try:
            return self._opener().open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            detail = error.read(600).decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("error", {}).get("message") or detail
            except (ValueError, AttributeError):
                pass
            raise OnlineError(f"{self.spec['name']} answered {error.code}: {str(detail)[:300]}") from error
        except (urllib.error.URLError, OSError) as error:
            raise OnlineError(f"could not reach {self.spec['name']}: {getattr(error, 'reason', error)}") from error

    def models(self) -> list[str]:
        with self._request("/models") as response:
            answer = json.loads(response.read().decode("utf-8", "replace"))
        found = [str(m.get("id")) for m in answer.get("data") or [] if isinstance(m, dict) and m.get("id")]
        return sorted(found)[:400]

    def stream(self, model: str, messages: list[dict], *, level: int, temperature: int, web: bool,
               max_tokens: int, cancel: threading.Event, extra: dict | None = None) -> Iterator[tuple[str, dict]]:
        """(kind, payload): ("think", text), ("answer", text), ("tool", AhoosAI's search as it goes),
        ("usage", counts), ("finish", {"reason"}) -- read from the service's stream. `extra` is
        merged into the request last (stop words, a JSON reply, a temperature)."""
        body = {"model": model, "messages": messages, "stream": True, "max_tokens": max_tokens}
        if self.spec.get("apex"):
            body.update({"thinking_level": level, "temperature_dial": temperature, "nimbus_format": True})
            if web:
                body["nimbus_tools"] = ["web_search", "fetch_url"]
        else:
            body["temperature"] = round(temperature / 5, 2)      # the dial's 5 is a temperature of 1.0
        if self.spec.get("usage"):
            body["stream_options"] = {"include_usage": True}
        body.update(extra or {})
        with self._request("/chat/completions", body, timeout=TIMEOUT) as response:
            for raw in response:
                if cancel.is_set():
                    return
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if chunk.get("error"):
                    raise OnlineError(str(chunk["error"].get("message") or chunk["error"])[:300])
                if isinstance(chunk.get("nimbus_tool"), dict):
                    yield "tool", chunk["nimbus_tool"]
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                thought = delta.get("reasoning_content") or delta.get("reasoning")
                if thought:
                    yield "think", {"text": thought}
                if delta.get("content"):
                    yield "answer", {"text": delta["content"]}
                if choice.get("finish_reason"):
                    yield "finish", {"reason": choice["finish_reason"]}
                if chunk.get("usage"):
                    yield "usage", chunk["usage"]


def history_messages(history: list[dict], memory: int, system: str) -> list[dict]:
    """The conversation as the service reads it: a compaction's summary in place of what it covers,
    then the last `memory` exchanges, alternating."""
    messages = [{"role": "system", "content": system}]
    start, summary = 0, None
    for index, message in enumerate(history):
        if message.get("summary"):
            start, summary = index + 1, message
    if summary is not None:
        messages += [{"role": "user", "content": "Summary of our conversation so far, in place of the earlier "
                                                 "messages:\n\n" + summary.get("content", "")},
                     {"role": "assistant", "content": "Understood."}]
    past = [m for m in history[start:] if not m.get("summary")]
    past = past[-memory * 2:] if memory else []
    for message in past:
        role = message.get("role")
        content = (message.get("sent") or message.get("content") or "") if role == "user" else (message.get("content") or "")
        if role in ("user", "assistant") and content.strip():
            if len(messages) > 1 and messages[-1]["role"] == role:
                messages[-1] = {"role": role, "content": content}
            else:
                messages.append({"role": role, "content": content})
    if messages[-1]["role"] == "user":
        messages.pop()
    return messages


class OnlineTurn:
    """One message to an online model, carried through to one answer -- agent.Turn's shape, for chat."""

    def __init__(self, *, settings: dict, provider: str, model: str, proxy: str, history: list[dict],
                 emit: Callable[[str, dict], None], cancel: threading.Event, chips: str = "", web: bool = False,
                 plan: bool = False) -> None:
        self.client = Online(settings.get("online") or {}, provider, proxy)
        self.settings, self.model, self.history = settings, model, history
        self.emit, self.cancel, self.web, self.plan = emit, cancel, web, plan
        self.system = GENERIC_SYSTEM + ("\n\n" + chips if chips else "")
        if provider != "ahoosai":                       # AhoosAI's engine says the date and the look itself
            self.system += today_text(local=True) + "\n\n" + FORMAT
        self.name = model if provider == "ahoosai" else f"{model} · {PROVIDERS[provider]['name']}"

    def _answer(self, messages: list[dict], *, level: int, max_tokens: int) -> dict:
        started = time.monotonic()
        think, answer, steps, usage, closed = [], [], [], {}, False
        error = ""
        try:
            for kind, payload in self.client.stream(self.model, messages, level=level,
                                                    temperature=int(self.settings.get("temperature", 5)),
                                                    web=self.web, max_tokens=max_tokens, cancel=self.cancel):
                if kind == "think":
                    think.append(payload["text"])
                    self.emit("think", payload)
                elif kind == "answer":
                    if think and not closed:
                        closed = True
                        self.emit("think_end", {"words": len("".join(think).split())})
                    answer.append(payload["text"])
                    self.emit("delta", payload)
                elif kind == "tool":
                    steps.append(self._tool(payload))
                elif kind == "usage":
                    usage = payload
        except OnlineError as problem:
            error = str(problem)
            self.emit("error", {"detail": error})
        if think and not closed:
            self.emit("think_end", {"words": len("".join(think).split())})
        seconds = round(time.monotonic() - started, 1)
        tokens = int(usage.get("completion_tokens") or 0)
        message = {"role": "assistant", "content": "".join(answer).strip(), "model": self.name,
                   "stats": {"seconds": seconds, "tokens": tokens, "tps": round(tokens / seconds, 2) if seconds and tokens else 0}}
        if think:
            message["think"] = "".join(think).strip()
        if steps:
            message["steps"] = [s for s in steps if s]
        if usage.get("total_tokens"):
            message["context"] = {"used": int(usage["total_tokens"]), "limit": int(PROVIDERS[self.client.provider]["context"])}
        if self.cancel.is_set():
            message["stopped"] = True
        if error:
            message["error"] = error
        return message

    def _tool(self, tool: dict) -> dict | None:
        """AhoosAI's web search, in the shape of the studio's own tool steps (agent.Turn._record)."""
        ident = str(tool.get("id") or uuid.uuid4().hex[:8])
        name, args = str(tool.get("name") or ""), tool.get("arguments") or {}
        if tool.get("status") == "running":
            self.emit("tool_start", {"id": ident, "name": name})
            self.emit("tool_call", {"id": ident, "name": name, "args": args})
            return None
        meta = {"results": tool.get("results")} if tool.get("results") else {}
        if tool.get("url"):
            meta["url"] = tool["url"]
        status = "error" if tool.get("error") else "done"
        record = {"id": ident, "name": name, "args": args, "status": status, "meta": meta, "output": tool.get("error") or ""}
        self.emit("tool_result", record)
        return record

    def run(self, sent: str) -> dict:
        messages = history_messages(self.history, int(self.settings.get("memory", 6)), self.system)
        if self.plan:
            # Plan mode, as the online studio asks it: the plan only, and a question to go ahead.
            messages[0] = {"role": "system", "content": self.system + "\n\nPlan mode. Do not carry out the task in this "
                           "answer. Reply with a plan: one sentence on the goal as you understand it, then numbered steps, "
                           "then how the result will be checked. Keep it short. End by asking the user to confirm."}
        messages.append({"role": "user", "content": sent})
        return self._answer(messages, level=int(self.settings.get("level", 5)), max_tokens=4096)

    def summarise(self, instruction: str) -> str:
        messages = history_messages(self.history, 1000, GENERIC_SYSTEM)
        messages.append({"role": "user", "content": instruction})
        self.web = False
        message = self._answer(messages, level=2, max_tokens=1500)
        if message.get("error"):
            raise OnlineError(message["error"])
        return message["content"]


# -- an online model with the local engine's face -------------------------------------------------------
#
# The agent (agent.Turn) works a local model through four calls -- apply_template, count,
# context_size, stream -- on a prompt it renders itself and then extends: "<tool_call>" and a
# grammar for each decision, an opened code fence for a file, a file's first stretch to continue.
# OnlineEngine answers the same four calls with a chat service, so everything the studio does with
# a local model -- files, the terminal, plans, the web, checks, undo -- it does with an online one,
# with no second agent to keep in step. The rendered prompt carries the messages themselves; what
# the agent adds after them becomes a request the service understands: a decision is asked for as
# one JSON object (the service's JSON mode where it has one), read whole, checked, and asked again
# once if it is not one; a continuation is asked for as the rest of the assistant's last message.

MESSAGES = "\u0002ahoos-messages\u0002"
MESSAGES_END = "\u0003"
JSON_MODE = {"openai", "groq", "deepseek", "gemini"}          # response_format json_object, as documented
DECIDE = ("Decide your next step now. Reply with ONE JSON object and nothing else -- no prose, no code fence -- "
          "matching one of these JSON Schema shapes:\n{schema}\n"
          "To use a tool: {{\"name\": \"<tool>\", \"arguments\": {{...}}}}. To answer the user in words: "
          "{{\"name\": \"reply\"}}.")
CONTINUE = ("Continue your last message exactly where it stops, as if it had not been interrupted. Write only the "
            "continuation: repeat nothing already written, and add no comment before or after it.")


def first_json_object(text: str) -> str:
    """The first balanced {...} in a reply that parses (inside a code fence or after a sentence), or ""."""
    start = text.find("{")
    while start >= 0:
        depth, quoted, escaped = 0, False, False
        for index in range(start, len(text)):
            ch = text[index]
            if quoted:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    quoted = False
                continue
            if ch == '"':
                quoted = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:index + 1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return ""


class OnlineEngine:
    """engine.Llama's four calls, answered by an online model (see above)."""

    def __init__(self, settings: dict, provider: str, model: str, proxy: str = "",
                 emit: Callable[[str, dict], None] | None = None) -> None:
        self.client = Online(settings.get("online") or {}, provider, proxy)
        self.provider, self.model, self.emit = provider, model, emit
        self.level = int(settings.get("level", 5))
        self.dial = int(settings.get("temperature", 5))
        self.has_adapter = False

    @property
    def label(self) -> str:
        return self.model if self.provider == "ahoosai" else f"{self.model} · {PROVIDERS[self.provider]['name']}"

    def context_size(self) -> int:
        return int(PROVIDERS[self.provider].get("context") or 32768)

    def count(self, text: str) -> int:
        return max(1, len(text) // 3)                      # a service's tokenizer is not at hand: about 3 characters

    def apply_template(self, messages: list[dict]) -> str:
        return MESSAGES + json.dumps(messages, ensure_ascii=False) + MESSAGES_END

    @staticmethod
    def _split(prompt: str) -> tuple[list[dict], str]:
        if not prompt.startswith(MESSAGES):
            return [{"role": "user", "content": prompt}], ""
        head, _, tail = prompt[len(MESSAGES):].partition(MESSAGES_END)
        return json.loads(head), tail

    @staticmethod
    def _ask(messages: list[dict], text: str) -> list[dict]:
        """A user turn added -- folded into the last one when that is the user's, as some services want."""
        if messages and messages[-1].get("role") == "user":
            return messages[:-1] + [{"role": "user", "content": f"{messages[-1]['content']}\n\n{text}"}]
        return messages + [{"role": "user", "content": text}]

    def _run(self, messages: list[dict], max_tokens: int, extra: dict, cancel: threading.Event | None,
             on_piece: Callable[[str], bool]) -> tuple[str, str, int]:
        """One request: (the reply, why it ended, tokens written). Reasoning is shown as it comes;
        on_piece returning True ends the request early (a stop word)."""
        from .engine import Cancelled, EngineError

        reply, reason, tokens, thought = [], "", 0, False
        flag = cancel or threading.Event()
        try:
            for kind, payload in self.client.stream(self.model, messages, level=self.level, temperature=self.dial,
                                                    web=False, max_tokens=max_tokens, cancel=flag, extra=extra):
                if flag.is_set():
                    raise Cancelled()
                if kind == "think" and self.emit:
                    thought = True
                    self.emit("think", payload)
                elif kind == "answer":
                    if thought and self.emit:
                        self.emit("think_end", {})
                        thought = False
                    reply.append(payload["text"])
                    if on_piece(payload["text"]):
                        reason = "stop"
                        break
                elif kind == "finish":
                    reason = payload["reason"]
                elif kind == "usage":
                    tokens = int(payload.get("completion_tokens") or 0)
        except OnlineError as error:
            raise EngineError(str(error)) from error
        if thought and self.emit:
            self.emit("think_end", {})
        if flag.is_set():
            raise Cancelled()
        return "".join(reply), reason, tokens

    def stream(self, prompt: str, *, n_predict: int, sampling, stop: list[str] | None = None,
               adapter: float | None = None, json_schema: dict | None = None,
               cancel: threading.Event | None = None, ending: dict | None = None,
               on_prompt: Callable[[int, int], None] | None = None) -> Iterator[str]:
        messages, tail = self._split(prompt)
        tail = tail.replace("<think>\n\n</think>\n\n", "")
        started = time.monotonic()
        size = self.count(prompt)
        if on_prompt:
            on_prompt(0, size)
        extra: dict = {}
        if not PROVIDERS[self.provider].get("apex"):
            extra["temperature"] = round(float(getattr(sampling, "temperature", 0.7)), 2)
        max_tokens = max(16, min(int(n_predict), 16384))

        if json_schema is not None:
            # A decision: one JSON object, read whole and checked; once more if it was not one.
            ask = self._ask(messages, DECIDE.format(schema=json.dumps(json_schema, ensure_ascii=False)))
            if self.provider in JSON_MODE:
                extra["response_format"] = {"type": "json_object"}
            found, tokens = "", 0
            for _ in range(2):
                text, _reason, tokens = self._run(ask, max(max_tokens, 1024), extra, cancel, lambda piece: False)
                found = first_json_object(text)
                if found and isinstance(json.loads(found), dict) and json.loads(found).get("name"):
                    break
                ask = ask + [{"role": "assistant", "content": text or "(nothing)"},
                             {"role": "user", "content": "That was not one JSON object of those shapes. Reply with the "
                                                         "JSON object only."}]
                found = ""
            found = found or json.dumps({"name": "reply"})
            if on_prompt:
                on_prompt(size, size)
            yield found
            if ending is not None:
                ending.update({"stop_type": "word", "timings": {"predicted_n": tokens or self.count(found),
                                                                "predicted_ms": (time.monotonic() - started) * 1000}})
            return

        # Free text; after the agent's own beginning (an opened code fence, a file's first stretch), the
        # rest of that message.
        ask = messages + [{"role": "assistant", "content": tail}, {"role": "user", "content": CONTINUE}] \
            if tail.strip() else messages
        words = [w for w in (stop or []) if w]
        if words:
            extra["stop"] = words[:4]
        pieces: queue.Queue = queue.Queue()
        state = {"text": "", "sent": 0, "cut": False}
        result: dict = {}

        def on_piece(text: str) -> bool:
            # The stop words kept here too (a service may not honour them): the text up to the first one.
            state["text"] += text
            cut = min((state["text"].find(w) for w in words if w in state["text"]), default=-1)
            end = cut if cut >= 0 else len(state["text"])
            if end > state["sent"]:
                pieces.put(state["text"][state["sent"]:end])
                state["sent"] = end
            state["cut"] = cut >= 0
            return state["cut"]

        def worker() -> None:
            try:
                result["value"] = self._run(ask, max_tokens, extra, cancel, on_piece)
            except BaseException as error:  # noqa: BLE001 - raised again in the reader below
                result["error"] = error
            pieces.put(None)

        threading.Thread(target=worker, daemon=True).start()
        first = True
        while (item := pieces.get()) is not None:
            if first and on_prompt:
                on_prompt(size, size)
                first = False
            yield item
        if "error" in result:
            raise result["error"]
        _text, reason, tokens = result["value"]
        if ending is not None:
            ending.update({"stop_type": "word" if state["cut"] else "limit" if reason == "length" else "eos",
                           "timings": {"predicted_n": tokens or self.count(state["text"][:state["sent"]]),
                                       "predicted_ms": (time.monotonic() - started) * 1000}})
