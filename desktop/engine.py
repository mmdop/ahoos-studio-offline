"""Talking to llama-server: one stream, any prompt, the adapter at any strength.

Everything the studio generates -- a chat answer, an agent's next action, both
sides of a battle -- goes through `Llama.stream`, over llama.cpp's native
`/completion`. The chat endpoint would apply the template itself, but it gives
no way to hand the model a half-written turn, and three things here depend on
exactly that: Apex's reasoning phase, which ends inside an open `<think>`; the
agent's decision, which starts inside an open `<tool_call>`; and continuing an
answer after a tool has run.

WHY THE ADAPTER IS CHOSEN PER REQUEST

The server loads the adapter once, and every request says how strongly to apply
it -- `"lora": [{"id": 0, "scale": s}]`. Scale 0 is the base model exactly, so
a battle between the base and the adapter runs on one process and one copy of
the weights, and the two answers can be generated side by side in the server's
two slots.

TEMPLATES

Models other than Apex are rendered by the server's own `/apply-template`, from
the chat template inside the GGUF -- so a model imported from the Hub is spoken
to in its own format without this file knowing what that format is. Apex keeps
its hand-written Qwen3 template (`apex.py`), because its reasoning ceiling needs
the prompt to end mid-turn.
"""

from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterator


class EngineError(RuntimeError):
    """The local model did not answer, or answered something unusable."""


class Cancelled(Exception):
    """The person pressed stop."""


@dataclass(frozen=True)
class Sampling:
    temperature: float = 0.7
    top_p: float = 0.9
    top_k: int = 40
    min_p: float = 0.05
    repeat_penalty: float = 1.05


# The ten positions of the temperature dial, for every model but Apex (which
# has its own trained mapping in nimbus2/controls.py). Five is 0.7, what most
# instruction-tuned models are published with.
DIAL = (0.1, 0.2, 0.35, 0.5, 0.7, 0.85, 1.0, 1.15, 1.3, 1.5)


def dial_sampling(position: int) -> Sampling:
    return Sampling(temperature=DIAL[max(1, min(10, int(position))) - 1])


class Llama:
    """One running llama-server."""

    def __init__(self, root: str, *, has_adapter: bool, timeout: float = 3600.0) -> None:
        # The OpenAI-style root ends in /v1; the native endpoints sit above it.
        self.root = root.rstrip("/").removesuffix("/v1")
        self.has_adapter = has_adapter
        self.timeout = timeout

    def _post(self, path: str, body: dict, timeout: float = 60.0) -> dict:
        request = urllib.request.Request(
            self.root + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            raise EngineError(f"the local model answered {error.code}: {detail}") from error
        except OSError as error:
            raise EngineError(f"the local model could not be reached: {error}") from error

    def apply_template(self, messages: list[dict]) -> str:
        """The model's own chat template, ending where the assistant speaks next.

        Some templates refuse a system turn (Gemma's raises); the system text is
        then folded into the first user turn, which is what those models expect.
        """
        try:
            return self._post("/apply-template", {"messages": messages})["prompt"]
        except EngineError:
            if not messages or messages[0]["role"] != "system":
                raise
            system, rest = messages[0]["content"], [dict(m) for m in messages[1:]]
            for message in rest:
                if message["role"] == "user":
                    message["content"] = f"{system}\n\n{message['content']}"
                    break
            return self._post("/apply-template", {"messages": rest})["prompt"]

    def stream(self, prompt: str, *, n_predict: int, sampling: Sampling, stop: list[str] | None = None,
               adapter: float | None = None, json_schema: dict | None = None,
               cancel: threading.Event | None = None, ending: dict | None = None,
               on_prompt: Callable[[int, int], None] | None = None) -> Iterator[str]:
        """Pieces of text as they arrive.

        `ending` is filled with the last chunk -- llama.cpp's own word for why it
        stopped (`stop_type`: "limit", "word", "eos") and its timings.
        `on_prompt(done, total)` reports how far the prompt has been read, which
        on a CPU is the half-minute before the first word appears.
        """
        body = {
            "prompt": prompt,
            "n_predict": n_predict,
            "stop": stop or [],
            "stream": True,
            "cache_prompt": True,
            "return_progress": True,
            "temperature": sampling.temperature,
            "top_p": sampling.top_p,
            "top_k": sampling.top_k,
            "min_p": sampling.min_p,
            "repeat_penalty": sampling.repeat_penalty,
        }
        if self.has_adapter:
            body["lora"] = [{"id": 0, "scale": 1.0 if adapter is None else float(adapter)}]
        if json_schema is not None:
            body["json_schema"] = json_schema
        request = urllib.request.Request(
            self.root + "/completion", data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            raise EngineError(f"the local model answered {error.code}: {detail}") from error
        except OSError as error:
            raise EngineError(f"the local model could not be reached: {error}") from error
        # Closing the connection is how llama-server is told to stop: it
        # notices the client has gone and frees the slot.
        with response:
            for raw in response:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    chunk = json.loads(line[5:])
                except ValueError:
                    continue
                progress = chunk.get("prompt_progress")
                if progress and on_prompt:
                    total = int(progress.get("total") or 0)
                    on_prompt(int(progress.get("processed") or 0) + int(progress.get("cache") or 0), total)
                piece = chunk.get("content") or ""
                if piece:
                    yield piece
                if chunk.get("stop"):
                    if ending is not None:
                        ending.update(chunk)
                    return
        if cancel is not None and cancel.is_set():
            raise Cancelled()


class Echo(Llama):
    """No model at all: `python -m desktop run --engine echo`.

    For working on the interface. It answers in the shapes the real engine does
    -- a decision when asked for one, prose otherwise -- slowly enough that
    streaming can be seen, and never pretends its words mean anything.
    """

    def __init__(self) -> None:
        super().__init__("http://echo.invalid", has_adapter=True)

    def apply_template(self, messages: list[dict]) -> str:
        return "".join(f"<|{m['role']}|>{m['content']}\n" for m in messages) + "<|assistant|>"

    def stream(self, prompt: str, *, n_predict: int, sampling: Sampling, stop=None, adapter=None,
               json_schema=None, cancel=None, ending=None, on_prompt=None) -> Iterator[str]:
        if on_prompt:
            on_prompt(0, len(prompt) // 4)
            time.sleep(0.4)
            on_prompt(len(prompt) // 4, len(prompt) // 4)
        last_user = prompt.rsplit("<|user|>", 1)[-1]
        if json_schema is not None:
            names = [option["properties"]["name"]["const"] for option in json_schema.get("oneOf", [])]
            if "<tool_response>" not in last_user and "write_file" in names and any(
                    word in last_user.lower() for word in ("make", "create", "write", "بساز", "بنویس")):
                text = json.dumps({"name": "write_file", "arguments": {
                    "path": "echo.txt", "content": "Written by the echo engine.\n"}})
            else:
                text = json.dumps({"name": "reply"})
        elif prompt.endswith("<think>\n"):
            text = "The echo engine does not think. It repeats, slowly, so the page can be tested."
        else:
            flavour = "the base model" if adapter == 0 else "the adapter"
            text = (f"**Echo** ({flavour}). No model is loaded: this is the interface talking to "
                    "itself.\n\n```python\nprint('hello from echo')\n```\n\n"
                    f"Last message, as received:\n\n> {last_user.strip()[:200]}")
        words = text.split(" ")
        for index, word in enumerate(words):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            time.sleep(random.uniform(0.01, 0.05))
            yield word + (" " if index < len(words) - 1 else "")
        if ending is not None:
            ending.update({"stop_type": "eos", "timings": {"predicted_n": len(words),
                                                           "predicted_per_second": 25.0}})
