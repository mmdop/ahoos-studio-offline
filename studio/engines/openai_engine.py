"""One engine for every OpenAI-compatible provider.

Most providers speak the same `POST /chat/completions` shape, so rather than a file
per provider this is configured entirely by environment. It covers the free routes
and the paid ones with the same code:

| Provider | Base URL | Key |
|---|---|---|
| Ollama (local, free) | `http://localhost:11434/v1` | not needed |
| Groq | `https://api.groq.com/openai/v1` | free tier |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | free tier |
| OpenRouter | `https://openrouter.ai/api/v1` | free models available |
| Together, Cerebras, Mistral, … | provider's URL | varies |

Free tiers change often — check the provider's current terms rather than trusting
this table.

Two capability gaps, both surfaced rather than faked:

**No thinking ladder.** Most providers expose one model with no effort parameter, so
all five levels run the same request. Set `OPENAI_REASONER_MODEL` to split the ladder
across two models; otherwise every result records that the ladder was flat.

**No server-side search.** `supports_search` is False, so tools are dropped rather
than sent and `deep_search` reports False for every model on this engine.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from ..spec import EngineConfig
from ._http import extract_json, post_json, usage_from
from .base import EngineError, EngineResult

# Ollama runs locally with no account and no key, but still wants the header.
# 127.0.0.1 rather than localhost on purpose: Python resolves `localhost` to ::1
# first on Windows, and Ollama listens on IPv4 only, so the name form fails with a
# misleading "unreachable" after a full retry cycle.
OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"
OLLAMA_PLACEHOLDER_KEY = "ollama"
OLLAMA_DEFAULT_MODEL = "qwen2.5-coder:7b"

# llama.cpp's server speaks the same endpoint, which is the whole reason the
# offline build needs no engine of its own: the desktop app starts llama-server
# on this port with our weights, and `--engine local` points the family at it.
# The model name is ignored by a server holding exactly one model, but it is
# sent anyway so a log of the request says which weights were meant.
LOCAL_BASE_URL = "http://127.0.0.1:8177/v1"
LOCAL_PLACEHOLDER_KEY = "local"
LOCAL_DEFAULT_MODEL = "nimbus-1.1-prime-ee"

JSON_INSTRUCTION = """\

Respond with a single JSON object and nothing else — no prose before or after it, no
markdown code fence. It must conform exactly to this JSON Schema:

{schema}
"""


class OpenAICompatibleEngine:
    """Talks to any provider that implements OpenAI's chat-completions endpoint."""

    name = "openai"
    supports_search = False

    def __init__(self, config: EngineConfig, *, preset: str | None = None) -> None:
        self.preset = preset
        self.config = config

        self.api_keys: list[str] = []

        if preset == "local":
            # No key, no account, no network. The URL is configurable because the
            # desktop app moves the port when 8177 is taken.
            self.base_url = os.environ.get("NIMBUS_LOCAL_BASE_URL", LOCAL_BASE_URL).rstrip("/")
            self.api_key = LOCAL_PLACEHOLDER_KEY
            self.api_keys = [LOCAL_PLACEHOLDER_KEY]
            self.chat_model = os.environ.get("NIMBUS_LOCAL_MODEL", LOCAL_DEFAULT_MODEL)
            self.reasoner_model = self.chat_model
            self.name = "local"
        elif preset == "ollama":
            self.base_url = os.environ.get("OLLAMA_BASE_URL", OLLAMA_BASE_URL).rstrip("/")
            self.api_key = OLLAMA_PLACEHOLDER_KEY
            self.api_keys = [OLLAMA_PLACEHOLDER_KEY]
            self.chat_model = os.environ.get("OLLAMA_MODEL", OLLAMA_DEFAULT_MODEL)
            self.reasoner_model = os.environ.get("OLLAMA_REASONER_MODEL") or self.chat_model
            self.name = "ollama"
        else:
            base_url = os.environ.get("OPENAI_BASE_URL")
            if not base_url:
                raise EngineError(
                    "OPENAI_BASE_URL is not set. Point it at your provider, e.g.\n"
                    "  https://api.groq.com/openai/v1\n"
                    "  https://openrouter.ai/api/v1\n"
                    "  https://generativelanguage.googleapis.com/v1beta/openai"
                )
            self.base_url = base_url.rstrip("/")

            # A comma-separated list rotates: when one key's daily quota is spent,
            # the next is tried. Duplicate OPENAI_API_KEY lines in .env do NOT
            # stack — the first wins and later copies are ignored — so several
            # keys go on one line separated by commas.
            self.api_keys = [
                k.strip() for k in os.environ.get("OPENAI_API_KEY", "").split(",") if k.strip()
            ]
            if not self.api_keys:
                raise EngineError("OPENAI_API_KEY is not set.")
            self.api_key = self.api_keys[0]

            self.chat_model = os.environ.get("OPENAI_MODEL")
            if not self.chat_model:
                raise EngineError(
                    "OPENAI_MODEL is not set. Name the model your provider serves, "
                    "e.g. llama-3.3-70b-versatile or gemini-2.0-flash."
                )
            self.reasoner_model = os.environ.get("OPENAI_REASONER_MODEL") or self.chat_model
            if "openrouter" in self.base_url:
                self.name = "openrouter"

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        # OpenRouter uses these for attribution on its public leaderboard. Both are
        # optional; sent only when configured.
        referer = os.environ.get("OPENROUTER_SITE_URL")
        title = os.environ.get("OPENROUTER_APP_NAME")
        if referer:
            headers["HTTP-Referer"] = referer
        if title:
            headers["X-Title"] = title
        return headers

    def _rotate_key(self) -> bool:
        """Move to the next key. False when there is no untried one left."""
        if len(self.api_keys) < 2:
            return False
        index = self.api_keys.index(self.api_key)
        if index + 1 >= len(self.api_keys):
            return False
        self.api_key = self.api_keys[index + 1]
        return True

    @staticmethod
    def _note_rate_limit(seconds: int, attempt: int, total: int) -> None:
        print(
            f"  rate limited, waiting {seconds}s (attempt {attempt}/{total})",
            file=sys.stderr,
        )

    # -- public ------------------------------------------------------------

    @property
    def has_ladder(self) -> bool:
        """True only when two distinct models back the thinking levels."""
        return self.chat_model != self.reasoner_model

    def model_for(self, thinking: str) -> str:
        if not self.has_ladder:
            return self.chat_model
        return self.chat_model if thinking in ("low", "medium") else self.reasoner_model

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
            payload_messages[-1]["content"] += JSON_INSTRUCTION.format(
                schema=json.dumps(json_schema, indent=2)
            )

        try:
            data = post_json(
                f"{self.base_url}/chat/completions",
                {
                    "model": model_id,
                    "messages": payload_messages,
                    "max_tokens": max_tokens,
                    "stream": False,
                },
                headers=self.headers,
                on_error=self._explain,
                on_rate_limit=self._note_rate_limit,
                error_cls=EngineError,
            )
        except EngineError as exc:
            # "Connection refused" from localhost almost always means the daemon
            # is not running, which is worth saying rather than leaving to guesswork.
            if self.preset == "ollama" and "unreachable" in str(exc):
                raise EngineError(
                    f"Could not reach Ollama at {self.base_url}. Is it running?\n"
                    "  Start it with:  ollama serve"
                ) from exc

            # A spent daily quota is per key, so another key may still have room.
            spent = self.api_keys.index(self.api_key) + 1
            if "quota" in str(exc).lower() and self._rotate_key():
                print(
                    f"  key {spent}/{len(self.api_keys)} quota spent, "
                    f"switching to key {spent + 1}",
                    file=sys.stderr,
                )
                return self.generate(
                    system=system,
                    messages=messages,
                    max_tokens=max_tokens,
                    thinking=thinking,
                    tools=tools,
                    json_schema=json_schema,
                )
            raise

        try:
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
            finish = choice.get("finish_reason")
        except (KeyError, IndexError) as exc:
            raise EngineError(f"unexpected response shape from {self.name}: {data}") from exc

        if json_schema is not None:
            text = extract_json(text)

        notes = [f"thinking={thinking} ran on {model_id}"]
        if not self.has_ladder:
            notes.append("thinking ladder is flat on this engine: every level runs the same model")
        if tools:
            notes.append("search unavailable on this engine, tools dropped")

        return EngineResult(
            text=text.strip(),
            model=f"{self.name}/{model_id}",
            stop_reason="end_turn" if finish == "stop" else finish,
            usage=usage_from(data.get("usage") or {}),
            searches=0,
            notes="; ".join(notes),
        )

    # -- internals ---------------------------------------------------------

    def _explain(self, status: int, detail: str) -> str | None:
        """Turn provider errors into something actionable."""
        if self.preset == "ollama":
            if status == 404:
                return (
                    f"Ollama does not have the model {self.chat_model!r}. Pull it first:\n"
                    f"  ollama pull {self.chat_model}"
                )
            return None

        if status == 401:
            return f"{self.name} rejected the API key (401)"
        if status == 402:
            return (
                f"{self.name} says this request needs credit (402). "
                "On OpenRouter, check the model id actually ends in ':free'."
            )
        if status == 404:
            return (
                f"{self.name} does not serve the model {self.chat_model!r} (404). "
                "Check the exact id, including any ':free' suffix."
            )
        # 429 and 5xx fall through to the retry logic in _http.post_json.
        return None
