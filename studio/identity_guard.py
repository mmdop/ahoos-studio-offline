"""Keeps the backend provider out of what Nimbus says.

Nimbus runs on somebody else's weights, and which somebody is a deployment
detail. A person talking to Nimbus should meet Nimbus, so an answer that
introduces itself as DeepSeek -- or carries an invisible marker -- has leaked
something the reader was never meant to see.

WHAT THIS CATCHES

Self-identification, not mention. "Use the OpenAI SDK" is a correct answer to a
question about the OpenAI SDK and must survive untouched; "I am a model built by
OpenAI" must not. The difference is whether the provider is named as the
speaker, so the patterns below require a first-person or authorship frame near
the name rather than the name alone. Matching the name by itself would break
exactly the technical answers this family exists to give.

Invisible characters too: zero-width spaces, joiners, word joiners, the BOM,
soft hyphens. No legitimate answer needs them, and they are the obvious place to
hide a marker, so they are stripped whenever they appear.

WHAT THIS CANNOT CATCH, AND WILL NOT PRETEND TO

Statistical watermarking -- a provider biasing which tokens it picks so its
output can be recognised later -- lives in the choice of ordinary words, not in
any character or phrase. It cannot be seen from here, so it cannot be removed
from here. Scrubbing invisible characters is a different thing from removing a
watermark, and calling it one would be false. If provenance matters, the honest
answer is that only the provider can say.
"""

from __future__ import annotations

import re

# Providers whose name must never appear as the speaker. Add rather than
# replace: an engine added later brings its own name with it.
PROVIDERS = (
    "deepseek", "anthropic", "claude", "openai", "chatgpt", "gpt-4", "gpt-5",
    "gemini", "google ai", "mistral", "llama", "qwen", "groq", "cerebras",
    "hugging ?face",
)

# The frame that turns a mention into a claim of identity. Kept close to the
# name -- within a short window -- so an unrelated sentence two clauses away
# does not trip it.
_FRAME = (
    r"(?:i am|i'm|i was|my name is|this is|you are (?:talking|speaking) to|"
    r"developed by|created by|built by|trained by|made by|powered by|"
    r"a model (?:from|by)|assistant (?:from|by))"
)
_NAME = "(?:" + "|".join(PROVIDERS) + ")"

# Either order: "I am DeepSeek", "DeepSeek is the model behind this".
IDENTITY_CLAIM = re.compile(
    rf"{_FRAME}[^.\n]{{0,40}}{_NAME}|{_NAME}[^.\n]{{0,25}}(?:is the model|powers|is behind)",
    re.IGNORECASE,
)

# Zero-width and formatting characters with no place in an answer.
INVISIBLE = re.compile("[​-‏⁠-⁤﻿­᠎]")


def scrub(text: str) -> tuple[str, int]:
    """Remove invisible characters. Returns the text and how many went."""
    cleaned = INVISIBLE.sub("", text)
    return cleaned, len(text) - len(cleaned)


def leak(text: str) -> str | None:
    """The first provider self-identification found, or None."""
    match = IDENTITY_CLAIM.search(text)
    return match.group(0).strip() if match else None


def inspect(text: str) -> tuple[str, int, str | None]:
    """Scrub, then check. Returns (clean text, invisibles removed, leak or None).

    Scrubbing runs first on purpose: a marker split by zero-width characters --
    `D​eepSeek` -- reads as ordinary text to the eye and defeats a naive
    pattern, so the text is normalised before it is judged.
    """
    cleaned, removed = scrub(text)
    return cleaned, removed, leak(cleaned)
