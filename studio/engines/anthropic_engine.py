"""Anthropic API engine — the phase 1 backend for every Nimbus model.

Install with:  pip install "anthropic"

Credentials resolve the usual way: ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an
`ant auth login` profile. Nothing is hardcoded here.
"""

from __future__ import annotations

from typing import Any

from ..spec import EngineConfig
from .base import EngineError, EngineResult

DEFAULT_MODEL = "claude-opus-5"

# Server-side refusal fallback: if safety classifiers decline a request, the API
# re-runs it on the recommended fallback model inside the same call instead of
# returning an empty response. Disable by setting fallbacks=False on the engine.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# A turn using server-side tools can stop with `pause_turn` when the platform's
# internal tool loop hits its iteration limit. Resending resumes it. Cap the
# resumes so a pathological request cannot loop forever.
MAX_CONTINUATIONS = 5


class AnthropicEngine:
    """Calls the Messages API with adaptive thinking, prompt caching and deep search."""

    name = "anthropic"

    def __init__(self, config: EngineConfig, *, fallbacks: bool = True) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise EngineError(
                'the "anthropic" package is not installed. Run: pip install anthropic'
            ) from exc

        self._sdk = anthropic
        self._client = anthropic.Anthropic()
        self.config = config
        self.model_id = config.model_id or DEFAULT_MODEL
        self.fallbacks = fallbacks

    # -- public ------------------------------------------------------------

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
        # The thinking level is the API's `effort`: it governs reasoning depth and
        # total token spend together.
        output_config: dict[str, Any] = {"effort": thinking}

        if json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
            # Structured outputs and search citations cannot coexist, so a
            # schema-constrained call never carries search tools. The planning
            # call is the only one that does this, and it needs no research.
            tools = None

        request: dict[str, Any] = {
            "model": self.model_id,
            "max_tokens": max_tokens,
            # The system prompt is byte-identical across calls for a given model,
            # so caching it turns every later call into a cache read.
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": output_config,
        }
        if tools:
            request["tools"] = tools

        message, searches = self._send(request)

        if getattr(message, "stop_reason", None) == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) or "unspecified"
            raise EngineError(f"request declined by safety classifiers (category: {category})")

        return EngineResult(
            text=self._text_of(message),
            model=getattr(message, "model", self.model_id),
            stop_reason=getattr(message, "stop_reason", None),
            usage=self._usage_of(message),
            searches=searches,
        )

    # -- internals ---------------------------------------------------------

    def _send(self, request: dict[str, Any]) -> tuple[Any, int]:
        """Run the turn, resuming through any `pause_turn` the server tools cause."""
        messages = list(request["messages"])
        searches = 0
        message: Any = None

        for _ in range(MAX_CONTINUATIONS):
            message = self._stream_once({**request, "messages": messages})
            searches += self._count_server_tools(message)

            if getattr(message, "stop_reason", None) != "pause_turn":
                return message, searches

            # Resuming is just resending with the paused assistant turn appended;
            # the server picks up where its tool loop left off.
            messages = messages + [{"role": "assistant", "content": message.content}]

        return message, searches

    def _stream_once(self, request: dict[str, Any]) -> Any:
        """One API call, preferring the fallback-enabled beta path.

        Streaming is used unconditionally: it avoids HTTP timeouts on large
        `max_tokens` and costs nothing when the reply is short.
        """
        if self.fallbacks:
            try:
                with self._client.beta.messages.stream(
                    **request,
                    betas=[FALLBACK_BETA],
                    fallbacks="default",
                ) as stream:
                    return stream.get_final_message()
            except self._sdk.BadRequestError:
                # SDK or account predates the fallback beta — carry on without it.
                self.fallbacks = False
            except TypeError:
                # SDK too old to accept the parameter at all.
                self.fallbacks = False

        with self._client.messages.stream(**request) as stream:
            return stream.get_final_message()

    @staticmethod
    def _count_server_tools(message: Any) -> int:
        return sum(
            1
            for block in getattr(message, "content", [])
            if getattr(block, "type", None) == "server_tool_use"
        )

    @staticmethod
    def _text_of(message: Any) -> str:
        parts = [
            block.text
            for block in getattr(message, "content", [])
            if getattr(block, "type", None) == "text"
        ]
        return "\n".join(parts).strip()

    @staticmethod
    def _usage_of(message: Any) -> dict[str, int]:
        usage = getattr(message, "usage", None)
        if usage is None:
            return {}
        fields = (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
        return {
            field: int(getattr(usage, field, 0) or 0)
            for field in fields
            if getattr(usage, field, None) is not None
        }
