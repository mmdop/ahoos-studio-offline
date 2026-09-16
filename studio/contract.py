"""The inter-model contract.

This is the only sanctioned way work moves between the manager and the specialists.
Specialists never talk to each other; everything routes through the manager.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .spec import THINKING_LEVELS


def plan_schema(levels: Sequence[str] = THINKING_LEVELS) -> dict[str, Any]:
    """JSON Schema the manager's planning call is constrained to.

    Structured outputs require every property listed in `required` and
    `additionalProperties: false`, so the manager cannot omit a thinking level or
    invent a field.
    """
    return {
        "type": "object",
        "properties": {
            "handle_directly": {
                "type": "boolean",
                "description": "True when you should answer this yourself with no delegation.",
            },
            "rationale": {
                "type": "string",
                "description": "One sentence: why this routing, in the user's language.",
            },
            "delegations": {
                "type": "array",
                "description": "Empty when handle_directly is true.",
                "items": {
                    "type": "object",
                    "properties": {
                        "model": {
                            "type": "string",
                            "description": "Exact specialist id from the roster.",
                        },
                        "brief": {
                            "type": "string",
                            "description": (
                                "The complete, self-contained task for that specialist: goal, "
                                "constraints, decisions already made, and what done looks like."
                            ),
                        },
                        "thinking": {
                            "type": "string",
                            "enum": list(levels),
                            "description": (
                                "How deeply this specialist should reason. Match it to the "
                                "task: low for a lookup, xhigh for hard implementation or "
                                "root-cause work."
                            ),
                        },
                    },
                    "required": ["model", "brief", "thinking"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["handle_directly", "rationale", "delegations"],
        "additionalProperties": False,
    }


# Convenience constant for callers that don't need custom levels.
PLAN_SCHEMA: dict[str, Any] = plan_schema()


@dataclass(frozen=True)
class Delegation:
    """One task the manager hands to one specialist."""

    model: str
    brief: str
    thinking: str | None = None   # None means "use the specialist's own default"
    gear: str | None = None       # set when a router gear forced this delegation


@dataclass(frozen=True)
class Plan:
    """The manager's routing decision for a request."""

    handle_directly: bool
    rationale: str
    delegations: tuple[Delegation, ...]
    # True when no routing decision was actually made — the planning call failed
    # and this is a fallback. Production still answers directly; evaluation must
    # never score it, because an infrastructure failure is not a model behaviour.
    failed: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, levels: Sequence[str] = THINKING_LEVELS) -> "Plan":
        raw = data.get("delegations") or []
        delegations = tuple(
            Delegation(
                model=item["model"],
                brief=item["brief"],
                # An out-of-range level falls back to the specialist's default
                # rather than failing the whole run.
                thinking=item.get("thinking") if item.get("thinking") in levels else None,
            )
            for item in raw
            if item.get("model") and item.get("brief")
        )
        handle_directly = bool(data.get("handle_directly", not delegations))
        if not delegations:
            handle_directly = True
        return cls(
            handle_directly=handle_directly,
            rationale=str(data.get("rationale", "")),
            delegations=() if handle_directly else delegations,
        )

    @classmethod
    def direct(cls, rationale: str = "") -> "Plan":
        return cls(handle_directly=True, rationale=rationale, delegations=())

    @classmethod
    def unplanned(cls, reason: str) -> "Plan":
        """A fallback after the planning call failed. Not a routing decision."""
        return cls(handle_directly=True, rationale=reason, delegations=(), failed=True)


@dataclass(frozen=True)
class DelegationResult:
    """What one specialist returned."""

    model: str
    brief: str
    output: str
    thinking: str = ""
    searches: int = 0
    gear: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def notes(self) -> list[str]:
        """Lines the specialist marked with `NOTE:` — these reach the user intact."""
        return [
            line.strip()
            for line in self.output.splitlines()
            if line.strip().startswith("NOTE:")
        ]


@dataclass
class Run:
    """A full pass through the family: plan, delegate, synthesize."""

    request: str
    plan: Plan
    results: list[DelegationResult] = field(default_factory=list)
    answer: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    searches: int = 0
    gears: list[str] = field(default_factory=list)

    def add_usage(self, usage: dict[str, int]) -> None:
        for key, value in usage.items():
            self.usage[key] = self.usage.get(key, 0) + int(value)
