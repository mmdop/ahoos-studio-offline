"""Request and response shapes for the HTTP API.

These are the public contract. Internal dataclasses (`Plan`, `DelegationResult`,
`Gear`, `Chip`) are deliberately *not* exposed directly — they change as the runtime
evolves, and clients should not have to change with them.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# `local` is the llama.cpp server the offline studio starts. It was added to
# build_engine and not here, so /v1/health answered 500 in the offline build:
# the engine existed and the schema describing it refused to name it.
EngineName = Literal["echo", "local", "ollama", "openai", "anthropic", "deepseek"]
ThinkingLevel = Literal["low", "medium", "high", "xhigh", "max"]


# -- shared call options ----------------------------------------------------


class CallOptions(BaseModel):
    engine: EngineName | None = Field(
        default=None,
        description="Override the engine. Defaults to the server's configured engine.",
    )
    thinking: ThinkingLevel | None = Field(
        default=None,
        description="Override the thinking level. Defaults to each model's own.",
    )
    search: bool = Field(
        default=True,
        description="Allow deep search for models that have the capability.",
    )
    solo: bool = Field(
        default=False,
        description="Answer with one model instead of the family: no planning "
                    "call, no delegation, no synthesis. A router gear still "
                    "chooses which model that is.",
    )


class ChipInput(BaseModel):
    """A chip supplied with the request instead of installed on disk.

    Content is appended after the model's own prompt, which is what stops it
    redefining what the model is: it refines a model that already knows its job.
    Capped because a system prompt has to leave room for the actual work.
    """

    name: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=8000)


class AskRequest(CallOptions):
    prompt: str = Field(min_length=1, description="The person's message, gear tokens included.")
    chips: list[ChipInput] = Field(
        default_factory=list,
        max_length=5,
        description="Ad-hoc skill chips for this request only. Nothing is installed.",
    )
    language: str | None = Field(
        default=None,
        description="Answer in this language code. Defaults to matching the prompt's language.",
    )


class RunRequest(CallOptions):
    prompt: str = Field(min_length=1, description="The brief for this model.")


class ParseRequest(BaseModel):
    prompt: str = Field(min_length=1)


# -- OpenAI-compatible surface ----------------------------------------------
#
# Nimbus answers in its own shape, which carries the plan and every specialist
# result -- more than a chat client wants and a shape none of them understand.
# These mirror OpenAI's chat-completions request and response so any existing
# bot, SDK or tool can point at Nimbus by changing a base URL, without knowing
# anything about how the family works.


class ChatMessage(BaseModel):
    role: str = Field(default="user")
    content: str = Field(default="")


class ChatCompletionRequest(CallOptions):
    model: str = Field(
        default="nimbus",
        description="`nimbus` runs the whole family. A specialist's id calls that model alone.",
    )
    messages: list[ChatMessage] = Field(min_length=1)
    # Accepted and ignored: this endpoint exists for compatibility, and silently
    # rejecting a caller for sending a temperature it always sends would defeat
    # the point. Which of them are honoured is documented rather than guessed.
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stream: bool = Field(default=False)
    user: str | None = None


# -- responses --------------------------------------------------------------


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    base: str
    base_version: str
    models: int
    gears: int
    chips: int
    engine: EngineName


class CapabilitiesOut(BaseModel):
    deep_search: bool
    max_searches: int


class ModelOut(BaseModel):
    id: str
    name: str
    version: str
    role: Literal["manager", "specialist"]
    domain: str
    summary: str
    thinking_default: ThinkingLevel
    capabilities: CapabilitiesOut
    adopted_blocks: list[str]
    chips: list[str]
    roster: list[str]


class ModelDetailOut(ModelOut):
    card: dict[str, Any]
    system_prompt_length: int


class DelegationOut(BaseModel):
    model: str
    brief: str
    thinking: str | None = None
    gear: str | None = None


class PlanOut(BaseModel):
    handle_directly: bool
    rationale: str
    delegations: list[DelegationOut]


class DelegationResultOut(BaseModel):
    model: str
    output: str
    thinking: str
    searches: int
    gear: str | None = None
    notes: list[str] = Field(default_factory=list)
    error: str | None = None


class AskResponse(BaseModel):
    answer: str
    plan: PlanOut
    results: list[DelegationResultOut]
    gears: list[str]
    searches: int
    usage: dict[str, int]


class RunResponse(BaseModel):
    model: str
    engine_model: str
    output: str
    thinking: str
    searches: int
    stop_reason: str | None = None
    usage: dict[str, int]


class GearInvocationOut(BaseModel):
    token: str
    start: int
    end: int
    gear: dict[str, Any]


class ParseResponse(BaseModel):
    raw: str
    text: str
    invocations: list[GearInvocationOut]
    unknown: list[str]


class ErrorResponse(BaseModel):
    error: str
    detail: str
