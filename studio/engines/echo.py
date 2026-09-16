"""Offline engine for testing wiring without spending tokens.

It calls nothing over the network. Use it to check that the registry loads, prompts
compose correctly, thinking levels and tools are wired through, and the
orchestrator's plan/delegate/synthesize loop runs end to end.
"""

from __future__ import annotations

import json
from typing import Any

from ..spec import EngineConfig
from .base import EngineResult


def _stub_for_schema(schema: dict[str, Any]) -> Any:
    """Build the smallest value that satisfies a schema's required fields."""
    kind = schema.get("type")

    if kind == "object":
        properties = schema.get("properties", {})
        return {
            key: _stub_for_schema(properties.get(key, {}))
            for key in schema.get("required", properties.keys())
        }
    if kind == "array":
        return []
    if kind == "boolean":
        return True
    if kind == "integer":
        return 0
    if kind == "number":
        return 0.0
    if "enum" in schema:
        return schema["enum"][0]
    return ""


class EchoEngine:
    """Deterministic, network-free stand-in."""

    name = "echo"

    def __init__(self, config: EngineConfig) -> None:
        self.config = config

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
        last_user = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                last_user = str(message.get("content", ""))
                break

        if json_schema is not None:
            payload = _stub_for_schema(json_schema)
            if isinstance(payload, dict) and "rationale" in payload:
                payload["rationale"] = "[echo] no routing performed"
            text = json.dumps(payload, ensure_ascii=False)
        else:
            tool_names = ", ".join(tool.get("name", "?") for tool in tools or []) or "none"
            text = (
                f"[echo:{self.config.model_id or 'stub'}] "
                f"system={len(system)} chars, thinking={thinking}, "
                f"max_tokens={max_tokens}, tools={tool_names}\n"
                f"received: {last_user[:400]}"
            )

        return EngineResult(
            text=text,
            model=f"echo/{self.config.model_id or 'stub'}",
            stop_reason="end_turn",
            usage={"input_tokens": 0, "output_tokens": 0},
            searches=0,
        )
