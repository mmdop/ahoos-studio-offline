"""Gears — the plugin layer.

A gear is a named capability the person invokes from inside their prompt text:

    @CR-image a dark pricing hero, terracotta accents

Two kinds:

* **router** — pins the work to a specific handler model. `@CR-image` always reaches
  the image specialist, whatever the manager would otherwise have decided.
* **modifier** — shapes the output without choosing who does the work. `@CR-file`
  says "come back with real files", and applies to whoever ends up doing it.

Every gear also carries a `[ui]` table that this runtime never reads. It exists so a
graphical client can render the gear chip, colour it, and show a hover card without
hardcoding anything about Nimbus. `Gear.to_ui_dict()` is what such a client consumes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# Gear tokens are `@CR-` followed by a name.
#
# Case-insensitive, because the lookup already is: GearSet indexes by
# token.lower(), so `@cr-image` resolved fine and never got that far -- the
# pattern did not match it, so it was not even reported as an unknown token.
# It simply vanished, and the request went wherever the manager felt like.
#
# The lookbehind is what keeps an email address from being a gear.
# `design@CR-image.example` used to invoke the image router, because nothing
# said the `@` had to start a word. It excludes the characters an email local
# part is made of rather than every word character, so a gear typed straight
# after Persian or Arabic text still counts -- those are not email addresses,
# and this project is written in both.
TOKEN_PATTERN = re.compile(r"(?<![A-Za-z0-9_.+-])@CR-[A-Za-z0-9][A-Za-z0-9_-]*", re.IGNORECASE)

ROUTER = "router"
MODIFIER = "modifier"
KINDS = (ROUTER, MODIFIER)


@dataclass(frozen=True)
class GearUI:
    """Presentation metadata for graphical clients. Unused by this runtime."""

    label: str = ""
    icon: str = ""
    accent: str = ""
    hover: str = ""
    example: str = ""


@dataclass(frozen=True)
class Gear:
    id: str
    name: str
    version: str
    kind: str
    summary: str
    handler: str | None
    token: str
    aliases: tuple[str, ...]
    instruction: str
    thinking: str | None
    search: bool | None
    ui: GearUI
    path: Path

    @property
    def tokens(self) -> tuple[str, ...]:
        return (self.token, *self.aliases)

    @property
    def is_router(self) -> bool:
        return self.kind == ROUTER

    def to_ui_dict(self) -> dict[str, Any]:
        """Everything a graphical client needs to render this gear."""
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "kind": self.kind,
            "summary": self.summary,
            "handler": self.handler,
            "token": self.token,
            "aliases": list(self.aliases),
            "label": self.ui.label or self.name,
            "icon": self.ui.icon,
            "accent": self.ui.accent,
            "hover": self.ui.hover or self.summary,
            "example": self.ui.example,
        }


@dataclass(frozen=True)
class GearInvocation:
    """One gear token found in a request, with where it was found.

    `start` and `end` index into the original request text, so a client can turn
    the token into a hover target without re-parsing.
    """

    gear: Gear
    token: str
    start: int
    end: int


@dataclass(frozen=True)
class ParsedRequest:
    """The result of scanning a request for gear tokens."""

    raw: str
    text: str                              # tokens stripped, whitespace tidied
    invocations: tuple[GearInvocation, ...]
    unknown: tuple[str, ...]               # `@CR-*` tokens matching no gear

    @property
    def gears(self) -> tuple[Gear, ...]:
        """Invoked gears, de-duplicated, in the order they first appear."""
        seen: dict[str, Gear] = {}
        for invocation in self.invocations:
            seen.setdefault(invocation.gear.id, invocation.gear)
        return tuple(seen.values())

    @property
    def routers(self) -> tuple[Gear, ...]:
        return tuple(gear for gear in self.gears if gear.is_router)

    @property
    def modifiers(self) -> tuple[Gear, ...]:
        return tuple(gear for gear in self.gears if not gear.is_router)


class GearSet:
    """Every gear on disk, indexed by token."""

    def __init__(self, gears: dict[str, Gear]) -> None:
        self.gears = gears
        self._by_token: dict[str, Gear] = {}
        for gear in gears.values():
            for token in gear.tokens:
                self._by_token[token.lower()] = gear

    def __len__(self) -> int:
        return len(self.gears)

    def __iter__(self) -> Iterable[Gear]:
        return iter(self.gears.values())

    def get(self, gear_id: str) -> Gear | None:
        return self.gears.get(gear_id)

    def by_token(self, token: str) -> Gear | None:
        return self._by_token.get(token.lower())

    def parse(self, request: str) -> ParsedRequest:
        """Find every gear token in a request."""
        invocations: list[GearInvocation] = []
        unknown: list[str] = []
        cuts: list[tuple[int, int]] = []

        for match in TOKEN_PATTERN.finditer(request):
            token = match.group(0)
            gear = self.by_token(token)
            if gear is None:
                unknown.append(token)
                continue
            invocations.append(
                GearInvocation(gear=gear, token=token, start=match.start(), end=match.end())
            )
            cuts.append((match.start(), match.end()))

        # Strip only the tokens we recognised; leave unknown ones in place so the
        # model can see the person typed something that did not resolve.
        text = request
        for start, end in reversed(cuts):
            text = text[:start] + text[end:]
        text = re.sub(r"[ \t]{2,}", " ", text).strip()

        return ParsedRequest(
            raw=request,
            text=text,
            invocations=tuple(invocations),
            unknown=tuple(dict.fromkeys(unknown)),
        )

    def describe(self, parsed: ParsedRequest) -> str:
        """Describe invoked gears for the manager's planning prompt."""
        if not parsed.gears:
            return ""
        lines = []
        for gear in parsed.gears:
            if gear.is_router:
                head = f"- {gear.token} ({gear.name}) — ROUTER, handler: `{gear.handler}`"
            else:
                head = f"- {gear.token} ({gear.name}) — MODIFIER, applies to every brief"
            lines.append(f"{head}\n  {gear.instruction.strip()}")
        return "\n".join(lines)
