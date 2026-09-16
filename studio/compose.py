"""Assemble a model's final system prompt from adopted base blocks + its own system.md.

This is where the "we take the best parts" contract is enforced: no model gets the
whole base, only the blocks it names in `adopt`.
"""

from __future__ import annotations

from typing import Sequence

from .chips import CHIP_HEADER, Chip
from .spec import BaseSpec, ModelSpec

SEPARATOR = "\n\n---\n\n"


def compose_system(
    base: BaseSpec,
    model: ModelSpec,
    chips: Sequence[Chip] = (),
) -> str:
    """Return the model's complete system prompt.

    Render order comes from the base's `block_order`, not from the order `adopt`
    happens to be written in — so the prompt's leading bytes stay stable across
    models and remain cacheable.

    Installed skill chips come last, after the model's own prompt. They are
    additions to a model that already knows its job, so they refine rather than
    define, and putting them last keeps the cacheable prefix identical whether or
    not a chip is installed.
    """
    adopted = set(model.adopt)
    values = base.language.template_values()

    parts = []
    for block_id in base.block_order:
        if block_id not in adopted:
            continue
        block = base.blocks[block_id]
        text = block.text.strip()
        # Only blocks that opt in are formatted, so a stray brace in ordinary
        # prompt prose can never blow up composition.
        parts.append(text.format(**values) if block.templated else text)

    parts.append(model.system_text.strip())

    for chip in chips:
        if chip.content:
            parts.append(f"{CHIP_HEADER.format(name=chip.name)}\n\n{chip.content}")

    return SEPARATOR.join(part for part in parts if part)


def describe_roster(models: dict[str, ModelSpec], roster: tuple[str, ...]) -> str:
    """Describe the available specialists, for injection into the manager's planning prompt."""
    lines = []
    for model_id in roster:
        spec = models.get(model_id)
        if spec is None:
            continue
        lines.append(f"- `{spec.id}` — {spec.domain}\n  {spec.summary}")
    return "\n".join(lines) if lines else "(no specialists in roster)"
