"""DeepSeek engine.

DeepSeek speaks the OpenAI chat-completions shape, so this talks to it over plain
HTTP with the standard library — no new dependency, nothing to install.

Two things DeepSeek does not have, and how each is handled honestly rather than
faked:

**No effort parameter.** Anthropic exposes five thinking levels as one knob.
DeepSeek instead has two models: `deepseek-chat` reasons little, `deepseek-reasoner`
always reasons. The five-level ladder therefore collapses to two, and the mapping is
an approximation — `low`/`medium` are genuinely the same request here, as are
`high`/`xhigh`/`max`. `EngineResult.notes` records which model actually ran so a
result is never mistaken for a five-level measurement.

**No server-side search.** `supports_search` is False, tools are dropped rather
than sent, and `searches` stays 0. A deep-search evaluation run against this engine
measures a model with no search, and the harness is told so instead of quietly
scoring it as though search had happened.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..spec import EngineConfig
from ._http import extract_json, post_json, usage_from
from .base import EngineError, EngineResult

# Re-exported: the parser lives in _http now, shared with the OpenAI-compatible
# engine, but importing it from here still works.
__all__ = ["DeepSeekEngine", "extract_json"]

DEFAULT_BASE_URL = "https://api.deepseek.com"
# `deepseek-chat` and `deepseek-reasoner` were retired on 2026-07-24 and now 404.
# These are their replacements; override with DEEPSEEK_CHAT_MODEL if they move again.
CHAT_MODEL = "deepseek-v4-flash"
REASONER_MODEL = "deepseek-v4-pro"
RETIRED_MODELS = {"deepseek-chat", "deepseek-reasoner"}

# The five-level ladder mapped onto the two models DeepSeek offers. Deliberately
# coarse; see the module docstring.
THINKING_MAP: dict[str, str] = {
    "low": CHAT_MODEL,
    "medium": CHAT_MODEL,
    "high": REASONER_MODEL,
    "xhigh": REASONER_MODEL,
    "max": REASONER_MODEL,
}

JSON_INSTRUCTION = """\

Respond with a single JSON object and nothing else — no prose before or after it, no
markdown code fence. It must conform exactly to this JSON Schema:

{schema}
"""

class DeepSeekEngine:
    """Calls DeepSeek's chat-completions endpoint."""

    name = "deepseek"
    # No server-side web search or fetch. Callers must not pass tools.
    supports_search = False

    def __init__(self, config: EngineConfig) -> None:
        self.api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not self.api_key:
            raise EngineError(
                "DEEPSEEK_API_KEY is not set. Put it in .env or export it."
            )

        self.base_url = os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        self.chat_model = os.environ.get("DEEPSEEK_CHAT_MODEL", CHAT_MODEL)
        self.reasoner_model = os.environ.get("DEEPSEEK_REASONER_MODEL", REASONER_MODEL)
        self.config = config

    # -- public ------------------------------------------------------------

    def model_for(self, thinking: str) -> str:
        """Which DeepSeek model a thinking level resolves to."""
        target = THINKING_MAP.get(thinking, REASONER_MODEL)
        return self.chat_model if target == CHAT_MODEL else self.reasoner_model

    def generate(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        thinking: str,
        tools: list[dict[str, Any]] | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> EngineResult:
        model_id = self.model_for(thinking)
        payload_messages = [{"role": "system", "content": system}]
        payload_messages.extend(
            {"role": m.get("role", "user"), "content": str(m.get("content", ""))}
            for m in messages
        )

        if json_schema is not None:
            # No schema-constrained decoding here, so the schema goes in the prompt
            # and the reply is parsed back out.
            payload_messages[-1]["content"] += JSON_INSTRUCTION.format(
                schema=json.dumps(json_schema, indent=2)
            )

        body: dict[str, Any] = {
            "model": model_id,
            "messages": payload_messages,
            "max_tokens": max_tokens,
            "stream": False,
        }

        data = post_json(
            f"{self.base_url}/chat/completions",
            body,
            headers={"Authorization": f"Bearer {self.api_key}"},
            on_error=self._explain,
            error_cls=EngineError,
        )

        try:
            choice = data["choices"][0]
            message = choice["message"]
            text = message["content"] or ""
            finish = choice.get("finish_reason")
        except (KeyError, IndexError) as exc:
            raise EngineError(f"unexpected DeepSeek response shape: {data}") from exc

        # These models answer in two fields: `reasoning_content` while they think,
        # and `content` once they commit. Run out of budget mid-thought and the
        # reply is well-formed, reports success, and carries an empty `content`.
        #
        # Returning that empty string is the worst of the options. The
        # orchestrator counts the delegation as done, the manager receives
        # nothing, and -- having no way to tell "produced nothing" from "produced
        # something I cannot see" -- it writes an explanation for the emptiness
        # instead of reporting a failure. A large request is exactly where this
        # bites, because that is where the budget runs out.
        if not text.strip():
            reasoning = (message.get("reasoning_content") or "").strip()
            if finish == "length":
                # Say which it was. The first version of this asserted reasoning
                # every time, and then a model that had done none of it hit the
                # cap and was reported as having thought its way through the
                # budget -- a message that names a cause it never checked sends
                # the reader to the wrong fix.
                if reasoning:
                    cause = (
                        f"spent its whole {max_tokens}-token budget reasoning "
                        f"({len(reasoning)} characters of it) and produced no answer"
                    )
                else:
                    cause = (
                        f"hit the {max_tokens}-token cap before writing any answer, "
                        "and did no reasoning on the way"
                    )
                raise EngineError(
                    f"{model_id} {cause}. Raise max_tokens for this model, or ask "
                    "for less in one request."
                )
            if reasoning:
                raise EngineError(
                    f"{model_id} returned reasoning but no answer "
                    f"({len(reasoning)} characters of it). Nothing was produced to use."
                )
            raise EngineError(f"{model_id} returned an empty answer (finish_reason={finish!r}).")

        if json_schema is not None:
            text = extract_json(text)

        note = f"thinking={thinking} ran on {model_id}"
        if tools:
            note += "; search unavailable on this engine, tools dropped"

        return EngineResult(
            text=text.strip(),
            model=f"deepseek/{model_id}",
            stop_reason="end_turn" if finish == "stop" else finish,
            usage=usage_from(data.get("usage") or {}),
            searches=0,
            notes=note,
        )

    # -- internals ---------------------------------------------------------

    def _explain(self, status: int, detail: str) -> str | None:
        """DeepSeek-specific statuses, phrased so a person knows what to do."""
        if status == 401:
            return "DeepSeek rejected the API key (401). Is it current?"
        if status == 402:
            return "DeepSeek reports insufficient balance (402). Top up the account."
        if status == 404:
            retired = RETIRED_MODELS & {self.chat_model, self.reasoner_model}
            if retired:
                return (
                    f"DeepSeek retired {sorted(retired)} on 2026-07-24. "
                    f"Use {CHAT_MODEL} / {REASONER_MODEL}, or set DEEPSEEK_CHAT_MODEL."
                )
            return f"DeepSeek does not serve that model (404): {detail[:200]}"
        return None
