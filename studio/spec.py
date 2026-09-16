"""Data structures for the base and the family's models.

This module defines shape only; reading from disk is the registry's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .languages import LanguagePolicy

# The five thinking levels, weakest to strongest. Mirrors base.toml; the registry
# validates that they agree.
THINKING_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class Block:
    """One adoptable block of the base."""

    id: str
    summary: str
    text: str
    # When true, the text is rendered through str.format with the base's template
    # values, so a block cannot drift from the config it describes.
    templated: bool = False


@dataclass(frozen=True)
class Thinking:
    """The family-wide thinking ladder."""

    levels: tuple[str, ...]
    default: str
    descriptions: dict[str, str] = field(default_factory=dict)

    def validate_level(self, level: str) -> str:
        if level not in self.levels:
            raise ValueError(
                f"unknown thinking level {level!r}. available: {', '.join(self.levels)}"
            )
        return level

    def rank(self, level: str) -> int:
        return self.levels.index(level)


@dataclass(frozen=True)
class BaseSpec:
    """The base model. Not runnable on its own."""

    id: str
    name: str
    version: str
    family: str
    vendor: str
    summary: str
    block_order: tuple[str, ...]
    blocks: dict[str, Block]
    thinking: Thinking
    language: LanguagePolicy
    upstream: dict[str, Any]
    path: Path


@dataclass(frozen=True)
class EngineConfig:
    provider: str
    model_id: str
    max_tokens: int


@dataclass(frozen=True)
class Capabilities:
    """What a model is allowed to do beyond generating text."""

    deep_search: bool = False
    max_searches: int = 5


@dataclass(frozen=True)
class ModelSpec:
    """One member of the Nimbus family."""

    id: str
    name: str
    version: str
    role: str          # "manager" | "specialist"
    domain: str
    summary: str
    adopt: tuple[str, ...]
    system_text: str
    engine: EngineConfig
    thinking_default: str
    capabilities: Capabilities
    roster: tuple[str, ...]
    card: dict[str, Any]
    path: Path

    @property
    def is_manager(self) -> bool:
        return self.role == "manager"
