"""Nimbus 2 Apex on llama.cpp: the two dials, and the ceiling on thinking.

WHY THIS DOES NOT GO THROUGH THE STUDIO API

The studio's orchestrator was built for the Nimbus 1.1 family: a manager that
delegates to specialists, five words of effort, and sampling that is documented
as accepted and ignored (`studio/api/app.py`). Nimbus 2 Apex is a different
animal -- twenty numbered thinking levels it was trained on, a ten-position
sampling dial, and a hard ceiling that closes the reasoning when the level's
budget runs out. Threading those through shared code that also serves the online
studio would change that product to suit this one, and still not give the
ceiling.

So this speaks to llama.cpp directly, and `nimbus2/controls.py` -- standard
library only, on purpose -- decides what to send. One model, its own engine.

THE TWO PHASES

The prompt ends inside an open `<think>`, exactly as training left it. Phase one
writes reasoning until the model closes the block itself or the level's token
budget runs out. Phase two continues from a *closed* block, which is the shape
the adapter was trained to answer after:

    <|im_start|>assistant
    <think>
    ...reasoning...
    </think>

    ...the answer...

This is the same two-phase generation `benchmarks/nimbus-2/evaluate.py` runs
under vLLM, so what a person sees in the app is what the benchmark measured.

WHY THE TEMPLATE IS WRITTEN OUT HERE

llama.cpp's chat endpoint applies the template itself and gives no way to hand
it a half-written assistant turn, which is precisely what phase two is. The
native completion endpoint takes a raw prompt, so the template is built here --
Qwen3's, the one the adapter was trained under. `tests/test_apex.py` pins its
shape.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nimbus2.controls import Controls, reasoning_words  # noqa: E402

MODEL_ID = "nimbus-2-apex"
DISPLAY_NAME = "Nimbus 2 Apex"
OPEN_THINK = "<think>\n"
CLOSE_THINK = "\n</think>\n\n"
END_TURN = "<|im_end|>"
MAX_HISTORY_TURNS = 12          # what memory sends back, at most


class ApexError(RuntimeError):
    """The local server did not answer, or answered something unusable."""


@dataclass
class Turn:
    """One finished exchange, oldest first, as memory replays it."""

    role: str                   # "user" | "assistant"
    content: str


def prompt_for(system: str, history: list[Turn], request: str) -> str:
    """Qwen3's template, ending inside an open reasoning block.

    History is sent as plain turns with the reasoning stripped: the model was
    trained on its own thinking, not on reading last turn's thinking back.
    """
    parts = [f"<|im_start|>system\n{system}{END_TURN}\n"]
    for turn in history:
        text = turn.content
        if turn.role == "assistant" and "</think>" in text:
            text = text.split("</think>", 1)[1].strip()
        parts.append(f"<|im_start|>{turn.role}\n{text}{END_TURN}\n")
    parts.append(f"<|im_start|>user\n{request}{END_TURN}\n")
    parts.append(f"<|im_start|>assistant\n{OPEN_THINK}")
    return "".join(parts)


@dataclass
class Answer:
    think: str
    answer: str
    thinking_level: int
    temperature: int
    think_words: int
    think_limit: int
    forced_close: bool          # the ceiling ended the reasoning, not the model


class Apex:
    """The adapter's engine, pointed at a running llama-server."""

    def __init__(self, base_url: str, timeout: float = 3600.0) -> None:
        # base_url is llama-server's OpenAI-compatible root; the native
        # completion endpoint sits beside it, one level up.
        self.root = base_url.rstrip("/").removesuffix("/v1")
        self.timeout = timeout

    # -- one call to llama.cpp ------------------------------------------------

    def _complete(self, prompt: str, *, limit: int, stop: list[str],
                  controls: Controls, ending: dict | None = None) -> Iterator[str]:
        """Pieces of text as they arrive. `ending` is filled with the last chunk,
        which is how llama.cpp says *why* it stopped -- `stopped_limit` for the
        ceiling, `stopped_word` for a stop sequence the model reached itself.
        Guessing that from the length would call a short answer a forced one."""
        sampling = controls.sampling()
        body = {
            "prompt": prompt,
            "n_predict": limit,
            "stop": stop,
            "stream": True,
            "cache_prompt": True,           # phase two re-sends phase one's prompt
            "temperature": sampling.temperature,
            "top_p": sampling.top_p,
            "top_k": sampling.top_k,
            "min_p": sampling.min_p,
            "repeat_penalty": sampling.repetition_penalty,
        }
        request = urllib.request.Request(
            self.root + "/completion",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for raw in response:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        chunk = json.loads(line[5:])
                    except ValueError:
                        continue
                    piece = chunk.get("content") or ""
                    if piece:
                        yield piece
                    if chunk.get("stop"):
                        if ending is not None:
                            ending.update(chunk)
                        return
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            raise ApexError(f"the local model answered {error.code}: {detail}") from error
        except OSError as error:
            raise ApexError(f"the local model could not be reached: {error}") from error

    # -- the two phases -------------------------------------------------------

    def generate(self, request: str, controls: Controls, history: list[Turn] | None = None
                 ) -> Iterator[tuple[str, dict]]:
        """Yield (event, payload) as it goes, ending with ("answer", {...}).

        Events: `think` for each piece of reasoning, `closed` when the block
        ends (with whether the ceiling did it), `answer_delta` for each piece of
        the answer, and `answer` once, at the end, with everything.
        """
        history = history[-MAX_HISTORY_TURNS:] if history else []
        system = controls.system_prompt()
        head = prompt_for(system, history, request)
        limit = controls.think_token_limit(request)

        think: list[str] = []
        ending: dict = {}
        for piece in self._complete(head, limit=limit, stop=["</think>", END_TURN],
                                    controls=controls, ending=ending):
            think.append(piece)
            yield "think", {"text": piece}
        reasoning = "".join(think).strip()

        # llama.cpp's own word for it: the ceiling closed the block when the
        # token budget ran out, not when the model decided it was done.
        forced = bool(ending.get("stopped_limit"))
        words = len(reasoning.split())
        yield "closed", {"forced": forced, "words": words,
                         "target": reasoning_words(controls.thinking_level)}

        tail = head + reasoning + CLOSE_THINK
        answer: list[str] = []
        for piece in self._complete(tail, limit=controls.max_answer_tokens, stop=[END_TURN],
                                    controls=controls):
            answer.append(piece)
            yield "answer_delta", {"text": piece}

        yield "answer", Answer(
            think=reasoning,
            answer="".join(answer).strip(),
            thinking_level=controls.thinking_level,
            temperature=controls.temperature,
            think_words=words,
            think_limit=limit,
            forced_close=forced,
        ).__dict__
