"""The two dials of Nimbus 2 Apex, and the prompt they are trained against.

Standard library only: the server, the CLI, the tests and the data pipeline all
import this, and none of them should need torch to know what level 7 means.

THINKING LEVEL, 1-20

Taught by the training data, enforced by the engine. Every training row carries
the system prompt below with its level and a word target, and its reasoning is
about that long (datagen/verify.py rejects rows outside 0.35x-2.5x). So the
adapter learns what "level 7" means from thousands of examples, and at inference
the same prompt asks for it.

A model can still overrun on a hard question, so the engine also holds a hard
limit: at 2.5x the level's word target -- the same tolerance training data was
held to -- it closes the reasoning itself and the model goes on to answer. The
level is then a promise with a ceiling, not a hope.

TEMPERATURE, 1-10

Not trained: sampling happens after the model. Each step is a full sampling
profile rather than one number, because a reasoning model at a low raw
temperature does not become precise, it loops -- Qwen's own guidance is never to
decode thinking greedily. So low levels narrow the choice with top-p and top-k
and add a light repetition penalty, and high levels widen it with min-p, which
keeps variety without letting the tail of the distribution in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MODEL_ID = "nimbus-2-apex"
DISPLAY_NAME = "Nimbus 2 Apex"

# Word-for-word the prompt in datagen/generate.py, which every training row was
# written under. tests/test_nimbus2.py fails if the two drift apart: a model
# asked at inference in words it was not trained on is being asked something else.
SYSTEM = """You are Nimbus 2 Apex, an assistant built by AhoosAI.

First reason inside <think> and </think>, then write the answer after </think>.

Thinking level for this request: {level}/20. Level 1 is a sentence or two of reasoning;
level 20 is exhaustive -- weighing alternatives, checking edge cases, verifying the
result. At level {level}, write about {words} words of reasoning: no padding to reach
it, and no skipping steps the level calls for.

The answer is complete, correct and well structured, in the language the user wrote in.
Code is runnable. For maths, the answer ends with the final answer on its own line as:
Final answer: <answer>"""

LEVELS = range(1, 21)
TEMPERATURES = range(1, 11)
HARD_LIMIT = 2.5                       # x the level's word target
TOKENS_PER_WORD = {"en": 1.35, "fa": 2.3}
PERSIAN = re.compile(r"[؀-ۿ]")


def reasoning_words(level: int) -> int:
    return max(12, round(15 * level ** 1.5))


@dataclass(frozen=True)
class Sampling:
    temperature: float
    top_p: float
    top_k: int                 # 0 = off
    min_p: float
    repetition_penalty: float


# One row per dial position. 5 is Qwen3's recommended thinking profile.
SAMPLING = {
    1: Sampling(0.15, 0.80, 20, 0.00, 1.05),
    2: Sampling(0.25, 0.85, 20, 0.00, 1.05),
    3: Sampling(0.35, 0.90, 20, 0.00, 1.03),
    4: Sampling(0.50, 0.95, 20, 0.00, 1.00),
    5: Sampling(0.60, 0.95, 20, 0.00, 1.00),
    6: Sampling(0.70, 0.95, 40, 0.00, 1.00),
    7: Sampling(0.80, 0.95, 40, 0.00, 1.00),
    8: Sampling(0.95, 0.97, 40, 0.02, 1.00),
    9: Sampling(1.10, 0.98, 0, 0.05, 1.00),
    10: Sampling(1.30, 0.99, 0, 0.08, 1.00),
}


@dataclass
class Controls:
    thinking_level: int = 5
    temperature: int = 5
    persona: str = ""                    # extra system text: who the assistant is for this product
    max_answer_tokens: int = 4096

    def __post_init__(self) -> None:
        if self.thinking_level not in LEVELS:
            raise ValueError(f"thinking_level must be 1-20, got {self.thinking_level}")
        if self.temperature not in TEMPERATURES:
            raise ValueError(f"temperature must be 1-10, got {self.temperature}")

    def system_prompt(self) -> str:
        text = SYSTEM.format(level=self.thinking_level, words=reasoning_words(self.thinking_level))
        # Placed after the base prompt, as training placed a source's own system
        # text (datagen/public/mix.py), so personas were seen in that position.
        return text + ("\n\n" + self.persona.strip() if self.persona.strip() else "")

    def sampling(self) -> Sampling:
        return SAMPLING[self.temperature]

    def think_token_limit(self, request: str) -> int:
        language = "fa" if len(PERSIAN.findall(request)) > 20 else "en"
        words = reasoning_words(self.thinking_level) * HARD_LIMIT
        return max(48, round(words * TOKENS_PER_WORD[language]))


@dataclass
class Request:
    text: str
    controls: Controls = field(default_factory=Controls)
    model: str = MODEL_ID
    messages: list[dict] = field(default_factory=list)    # earlier turns, oldest first
    images: list[bytes] = field(default_factory=list)     # read by nimbus2/perception.py, never by the model


_FIELD = re.compile(r"(\w+)\s*=\s*(\(\s*\"(?:[^\"\\]|\\.)*\"\s*\)|\"(?:[^\"\\]|\\.)*\"|[^\s}]+)", re.S)
_ALIASES = {
    "model": "model",
    "thinking_level": "thinking_level", "tinking_level": "thinking_level", "level": "thinking_level",
    "temperature": "temperature", "tmprcher": "temperature", "temp": "temperature",
    "request": "request", "recuest": "request", "prompt": "request",
    "persona": "persona",
}


def parse_request(text: str) -> Request:
    """The compact form, for a terminal or a one-line integration:

        {model=nimbus-2-apex thinking_level=5/20 temperature=3/10 request=("How do I ...")}

    `5/20` and `5` are the same; the request may be ("..."), "..." or one word.
    """
    body = text.strip()
    if body.startswith("{") and body.endswith("}"):
        body = body[1:-1]
    values: dict[str, str] = {}
    for key, raw in _FIELD.findall(body):
        name = _ALIASES.get(key.lower())
        if name is None:
            raise ValueError(f"unknown field {key!r}")
        raw = raw.strip()
        if raw.startswith("("):
            raw = raw[1:-1].strip()
        if raw.startswith('"'):
            raw = bytes(raw[1:-1], "utf-8").decode("unicode_escape") if "\\" in raw else raw[1:-1]
        values[name] = raw
    if not values.get("request"):
        raise ValueError("request=(\"...\") is required")
    model = values.get("model", MODEL_ID)
    if model != MODEL_ID:
        raise ValueError(f"this engine serves {MODEL_ID}, not {model!r}")

    def dial(name: str, default: int, top: int) -> int:
        raw = values.get(name)
        if raw is None:
            return default
        number, _, scale = raw.partition("/")
        if scale and int(scale) != top:
            raise ValueError(f"{name} is out of {top}, not {scale}")
        return int(number)

    controls = Controls(thinking_level=dial("thinking_level", 5, 20), temperature=dial("temperature", 5, 10),
                        persona=values.get("persona", ""))
    return Request(text=values["request"], controls=controls, model=model)
