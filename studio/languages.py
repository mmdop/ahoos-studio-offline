"""Writing languages.

Nimbus writes natively in a small set of languages and reaches every other language
through a *bridge*: the model works in the pivot language and delivers in the target.
Bridged languages need no training of their own.

This module is the machine-readable half of that policy. The behavioural half is the
`language` base block, which is templated from the same config so the two cannot
drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

LTR = "ltr"
RTL = "rtl"
DIRECTIONS = (LTR, RTL)


@dataclass(frozen=True)
class Language:
    code: str
    name: str
    endonym: str
    direction: str
    native: bool

    @property
    def is_rtl(self) -> bool:
        return self.direction == RTL

    def to_ui_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "endonym": self.endonym or self.name,
            "direction": self.direction,
            "native": self.native,
        }


class LanguagePolicy:
    """Which languages are native, which are bridged, and through what."""

    def __init__(
        self,
        catalog: dict[str, Language],
        native: tuple[str, ...],
        pivot: str,
    ) -> None:
        self.catalog = catalog
        self.native_codes = native
        self.pivot_code = pivot

    def __iter__(self) -> Iterable[Language]:
        return iter(self.catalog.values())

    def __len__(self) -> int:
        return len(self.catalog)

    # -- lookup ------------------------------------------------------------

    def get(self, code: str) -> Language | None:
        return self.catalog.get(code.lower())

    def resolve(self, code: str) -> Language:
        """Look up a language, synthesising a bridged entry for unknown codes.

        An unknown code is not an error. The catalogue exists for display metadata;
        the bridge works for any language whether or not we listed it.
        """
        code = code.lower()
        known = self.catalog.get(code)
        if known is not None:
            return known
        return Language(code=code, name=code.upper(), endonym="", direction=LTR, native=False)

    def is_native(self, code: str) -> bool:
        return code.lower() in self.native_codes

    # -- views -------------------------------------------------------------

    @property
    def pivot(self) -> Language:
        return self.resolve(self.pivot_code)

    @property
    def natives(self) -> tuple[Language, ...]:
        return tuple(self.resolve(code) for code in self.native_codes)

    @property
    def bridged(self) -> tuple[Language, ...]:
        return tuple(lang for lang in self.catalog.values() if not lang.native)

    def native_names(self) -> str:
        """Human-readable list of native languages, for prompt templating."""
        return ", ".join(lang.name for lang in self.natives)

    def template_values(self) -> dict[str, str]:
        """Values injected into templated base blocks."""
        return {
            "native_languages": self.native_names(),
            "pivot_language": self.pivot.name,
        }

    def to_ui_dict(self) -> dict[str, Any]:
        """Everything a graphical client needs to render a language picker."""
        return {
            "pivot": self.pivot_code,
            "native": list(self.native_codes),
            "languages": [lang.to_ui_dict() for lang in self.catalog.values()],
        }
