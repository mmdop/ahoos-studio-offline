"""The engine interface.

Every Nimbus model sits behind this one interface. In phase 1 the implementation
is a cloud API; in phase 2 it becomes our own fine-tuned weights. Nothing else in
the system needs to change when that happens — which is the entire point of the
indirection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..spec import EngineConfig


class EngineError(RuntimeError):
    """The engine could not produce a result."""


@dataclass
class EngineResult:
    text: str
    model: str
    stop_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    # How many server-side searches and fetches actually ran, for reporting.
    searches: int = 0
    # Anything the caller should know about how this result was produced — an
    # approximated thinking level, a dropped capability. Reported, never hidden.
    notes: str = ""

    @property
    def refused(self) -> bool:
        return self.stop_reason == "refusal"


@runtime_checkable
class Engine(Protocol):
    """What a provider must implement."""

    name: str
    # Whether this provider offers server-side web search and fetch. Providers that
    # do not set this False, and callers drop tools rather than sending them into
    # the void — so a run without search is visibly a run without search.
    supports_search: bool = True

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
        """Run one turn.

        `thinking` is one of the base's five levels and controls reasoning depth.
        `tools` are server-side tool definitions (deep search).
        `json_schema`, when given, constrains the reply to valid JSON matching it.
        """
        ...


# Providers that draw rather than write. A text engine cannot stand in for one,
# so `--engine` and NIMBUS_ENGINE do not reach a model that names one of these.
IMAGE_PROVIDERS: set[str] = set()


def build_engine(config: EngineConfig, *, override: str | None = None) -> Engine:
    """Instantiate the engine named by the config, or by `override` if given.

    The override is how one run switches every model to another provider, which
    is what makes `--compare` valid: both arms must run on the same model.

    It stops at an image provider. Swapping one text engine for another is a
    swap; replacing an image engine with a text one removes the only thing that
    model does, and would do it silently -- the run would look complete and
    return prose where a picture was asked for. A model pinned to an image
    provider keeps it.
    """
    own = (config.provider or "").lower()
    if own in IMAGE_PROVIDERS and (override or "").lower() not in IMAGE_PROVIDERS:
        override = None

    provider = (override or config.provider or "echo").lower()

    if provider in IMAGE_PROVIDERS:
        from .image_engine import ImageEngine

        return ImageEngine(config)

    if provider == "echo":
        from .echo import EchoEngine

        return EchoEngine(config)

    if provider == "anthropic":
        from .anthropic_engine import AnthropicEngine

        return AnthropicEngine(config)

    if provider == "deepseek":
        from .deepseek_engine import DeepSeekEngine

        return DeepSeekEngine(config)

    if provider in ("openai", "ollama", "local"):
        # One implementation, three entry points. `ollama` and `local` are the
        # same engine pre-pointed at a daemon on this machine with no account and
        # no key -- Ollama on its own port, `local` at the llama.cpp server the
        # offline build starts with our own weights.
        from .openai_engine import OpenAICompatibleEngine

        preset = provider if provider in ("ollama", "local") else None
        return OpenAICompatibleEngine(config, preset=preset)

    raise EngineError(
        f"unknown engine provider: {provider!r}. "
        "available: echo, local, ollama, openai, deepseek, anthropic. "
        "Add new providers under studio/engines/."
    )
