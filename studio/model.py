"""A runnable Nimbus model: a spec, its composed system prompt, and an engine."""

from __future__ import annotations

import json
from typing import Any, Sequence

from .capabilities import tools_for
from .chips import Chip
from .compose import compose_system
from . import identity_guard
from .engines import Engine, EngineError, build_engine
from .spec import BaseSpec, ModelSpec

# Re-runs allowed when an answer names the backend. Two, because a leak is
# usually a one-off phrasing and a third attempt mostly buys latency.
IDENTITY_ATTEMPTS = 2


class NimbusModel:
    """One member of the family, ready to call."""

    def __init__(
        self,
        base: BaseSpec,
        spec: ModelSpec,
        engine: Engine,
        chips: Sequence[Chip] = (),
    ) -> None:
        self.base = base
        self.spec = spec
        self.engine = engine
        self.chips = tuple(chips)
        self.system = compose_system(base, spec, self.chips)

    @classmethod
    def build(
        cls,
        base: BaseSpec,
        spec: ModelSpec,
        *,
        engine_override: str | None = None,
        chips: Sequence[Chip] = (),
    ) -> "NimbusModel":
        return cls(
            base,
            spec,
            build_engine(spec.engine, override=engine_override),
            chips,
        )

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def deep_search(self) -> bool:
        """Whether this model both declares search and is on an engine that has it."""
        return self.spec.capabilities.deep_search and getattr(
            self.engine, "supports_search", True
        )

    def resolve_thinking(self, level: str | None) -> str:
        """Pick the thinking level for a call: explicit override, else the model's default."""
        if level is None:
            return self.spec.thinking_default
        return self.base.thinking.validate_level(level)

    def ask(
        self,
        prompt: str,
        *,
        thinking: str | None = None,
        search: bool = True,
        json_schema: dict[str, Any] | None = None,
    ):
        """Run one turn and return the raw `EngineResult`.

        The answer is checked before it is returned. Nimbus runs on somebody
        else's weights, and which somebody is a deployment detail: a person
        talking to Nimbus should meet Nimbus. An answer that introduces itself
        as the backend has leaked something the reader was never meant to see,
        so it is discarded and the call is run again rather than repaired --
        editing the sentence out would leave the rest of an answer written from
        the wrong identity.

        Invisible characters are stripped every time, leak or not. Nothing
        legitimate needs them, and they are the obvious place to hide a marker.
        """
        # An engine without server-side search gets no tools at all, rather than
        # tools it would silently ignore.
        enabled = search and getattr(self.engine, "supports_search", True)

        last_leak = None
        for attempt in range(IDENTITY_ATTEMPTS):
            result = self.engine.generate(
                system=self.system,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=self.spec.engine.max_tokens,
                thinking=self.resolve_thinking(thinking),
                tools=tools_for(self.spec.capabilities, enabled=enabled),
                json_schema=json_schema,
            )

            cleaned, removed, found = identity_guard.inspect(result.text)
            result.text = cleaned
            if removed:
                result.notes = (result.notes or "") + f"; {removed} invisible char(s) stripped"

            if not found:
                return result

            last_leak = found
            result.notes = (result.notes or "") + f"; discarded, named the backend: {found!r}"

        # Refusing beats returning it. A leak that survives every attempt is a
        # prompt problem, and shipping the answer anyway would hide that.
        raise EngineError(
            f"{self.id} named its backend in every one of {IDENTITY_ATTEMPTS} attempts "
            f"(last: {last_leak!r}). The answer was discarded rather than sent on."
        )

    def ask_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        thinking: str | None = None,
    ) -> tuple[dict[str, Any], Any]:
        """Run one turn constrained to `schema` and return the parsed object.

        Returns `(parsed, result)` so the caller can still read usage off the result.
        """
        result = self.ask(prompt, thinking=thinking, search=False, json_schema=schema)
        try:
            parsed = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise EngineError(
                f"{self.id} was asked for JSON but returned something else: {result.text[:200]!r}"
            ) from exc
        if not isinstance(parsed, dict):
            raise EngineError(f"{self.id} returned a non-object JSON value: {type(parsed).__name__}")
        return parsed, result
