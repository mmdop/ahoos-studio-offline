"""Capability wiring — turning a model's declared capabilities into engine input.

Right now there is one capability, deep search. It maps to Anthropic's server-side
web search and web fetch tools, which run on Anthropic's infrastructure: the model
issues the query, the platform executes it, and results come back as content blocks
in the same response. Nothing executes locally.
"""

from __future__ import annotations

from typing import Any

from .spec import Capabilities

# Tool versions with built-in dynamic filtering: results are filtered before they
# reach the context window. Do NOT also declare a code execution tool alongside
# these — they already run code internally, and a second execution environment
# confuses the model.
WEB_SEARCH_TOOL = "web_search_20260209"
WEB_FETCH_TOOL = "web_fetch_20260209"


def deep_search_tools(capabilities: Capabilities) -> list[dict[str, Any]]:
    """Server-side tools for a model that declares deep search.

    `web_fetch` only retrieves URLs already present in the conversation — usually
    ones `web_search` just surfaced. Pairing them is what makes "go deep, not wide"
    possible: search finds the document, fetch reads it.
    """
    if not capabilities.deep_search:
        return []

    max_uses = max(1, int(capabilities.max_searches))
    return [
        {"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": max_uses},
        {"type": WEB_FETCH_TOOL, "name": "web_fetch", "max_uses": max_uses},
    ]


def tools_for(capabilities: Capabilities, *, enabled: bool = True) -> list[dict[str, Any]]:
    """Every tool a model should be offered for one call."""
    if not enabled:
        return []
    return deep_search_tools(capabilities)
