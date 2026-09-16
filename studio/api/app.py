"""The Nimbus HTTP API.

Read-only endpoints describe the family — models, gears, chips, languages — so a
client can render its UI without knowing anything about Nimbus internals. Two
endpoints do work: `/v1/ask` runs the manager loop, `/v1/models/{id}/run` calls one
model directly.

Run it with:  python -m studio serve
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Iterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..chips import Chip, ChipError
from ..contract import Delegation, DelegationResult, Plan, Run
from ..engines import EngineError
from ..gears import ParsedRequest
from ..orchestrator import Orchestrator
from ..registry import Registry, RegistryError
from . import keys
from .schemas import (
    AskRequest,
    ChatCompletionRequest,
    AskResponse,
    CapabilitiesOut,
    DelegationOut,
    DelegationResultOut,
    ErrorResponse,
    GearInvocationOut,
    HealthResponse,
    ModelDetailOut,
    ModelOut,
    ParseRequest,
    ParseResponse,
    PlanOut,
    RunRequest,
    RunResponse,
)

DEFAULT_ENGINE = os.environ.get("NIMBUS_ENGINE", "echo")
# When set, every /v1 request must carry `Authorization: Bearer <key>`. Unset means
# open, which is fine for localhost and wrong for anything else.
API_KEY = os.environ.get("NIMBUS_API_KEY") or None
CORS_ORIGINS = [o.strip() for o in os.environ.get("NIMBUS_CORS_ORIGINS", "").split(",") if o.strip()]


class State:
    """Process-wide registry, loaded once."""

    registry: Registry | None = None
    root: Path | None = None

    @classmethod
    def load(cls, root: Path | str | None = None) -> Registry:
        cls.root = Path(root).resolve() if root else None
        cls.registry = Registry.load(cls.root)
        return cls.registry

    @classmethod
    def get(cls) -> Registry:
        if cls.registry is None:
            return cls.load(cls.root)
        return cls.registry


def presented_key(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def require_key(request: Request) -> None:
    """Bearer-token gate, for two kinds of token.

    The shared NIMBUS_API_KEY still works: it is what the studio's own PHP host
    presents, and it has no per-caller identity because it is the deployment
    talking to itself.

    Anything else is looked up as a per-caller key, issued from the platform
    page. That path carries a token limit, and the request is refused here only
    when the caller is already over -- never for the request that crosses the
    line, which has already been paid for by the time anybody knows.

    Order matters. The shared key is checked first and locally, so the studio
    never waits on a database round trip to talk to its own backend.
    """
    token = presented_key(request)
    if API_KEY is not None and token == API_KEY:
        return
    if not keys.enabled():
        # No per-caller keys configured, so the shared key is the only gate
        # there is -- and it either matched above or there is nothing to match.
        if API_KEY is None:
            return
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
    if not token:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    answer = keys.check(token) or {}
    if answer.get("_error"):
        # Configured but unreachable. Refusing is the honest answer: letting the
        # request through would be a limit that stops applying exactly when the
        # database is having a bad minute.
        raise HTTPException(status_code=503, detail="cannot verify the key right now")
    if not answer.get("known"):
        raise HTTPException(status_code=401, detail="unknown or revoked key")
    if answer.get("over"):
        raise HTTPException(
            status_code=429,
            detail=f"token limit reached: {answer.get('used')} of {answer.get('limit')} "
                   f"this {answer.get('period')}",
        )


def record_usage(request: Request, usage: dict) -> None:
    """Charge a finished request against the key that made it.

    After the work, not before: what it cost is not known until it is done, and
    a reservation would have to be guessed and then corrected.

    Never raises. The tokens are spent either way, and a failure to write the
    meter should not turn a completed answer into an error the caller sees
    instead of it.
    """
    token = presented_key(request)
    if not token or not keys.enabled() or (API_KEY is not None and token == API_KEY):
        return
    try:
        tokens_in, tokens_out = keys.usage_tokens(usage or {})
        keys.consume(token, tokens_in, tokens_out)
    except Exception:
        pass


# -- conversion -------------------------------------------------------------


def _model_out(registry: Registry, spec) -> ModelOut:
    return ModelOut(
        id=spec.id,
        name=spec.name,
        version=spec.version,
        role=spec.role,
        domain=spec.domain,
        summary=spec.summary,
        thinking_default=spec.thinking_default,
        capabilities=CapabilitiesOut(
            deep_search=spec.capabilities.deep_search,
            max_searches=spec.capabilities.max_searches,
        ),
        adopted_blocks=list(spec.adopt),
        chips=[chip.id for chip in registry.chips.for_model(spec.id)],
        roster=list(spec.roster),
    )


def _plan_out(plan: Plan) -> PlanOut:
    return PlanOut(
        handle_directly=plan.handle_directly,
        rationale=plan.rationale,
        delegations=[
            DelegationOut(model=d.model, brief=d.brief, thinking=d.thinking, gear=d.gear)
            for d in plan.delegations
        ],
    )


def _result_out(result: DelegationResult) -> DelegationResultOut:
    return DelegationResultOut(
        model=result.model,
        output=result.output,
        thinking=result.thinking,
        searches=result.searches,
        gear=result.gear,
        notes=result.notes,
        error=result.error,
    )


def _run_out(run: Run) -> AskResponse:
    return AskResponse(
        answer=run.answer,
        plan=_plan_out(run.plan),
        results=[_result_out(r) for r in run.results],
        gears=list(run.gears),
        searches=run.searches,
        usage=run.usage,
    )


def _parse_out(parsed: ParsedRequest) -> ParseResponse:
    return ParseResponse(
        raw=parsed.raw,
        text=parsed.text,
        invocations=[
            GearInvocationOut(
                token=item.token,
                start=item.start,
                end=item.end,
                gear=item.gear.to_ui_dict(),
            )
            for item in parsed.invocations
        ],
        unknown=list(parsed.unknown),
    )


def _event_payload(kind: str, detail: Any) -> dict[str, Any]:
    """Turn an orchestrator progress event into something JSON-serialisable."""
    if isinstance(detail, ParsedRequest):
        return {"gears": [g.id for g in detail.gears], "unknown": list(detail.unknown)}
    if isinstance(detail, Plan):
        return _plan_out(detail).model_dump()
    if isinstance(detail, Delegation):
        return DelegationOut(
            model=detail.model, brief=detail.brief, thinking=detail.thinking, gear=detail.gear
        ).model_dump()
    if isinstance(detail, DelegationResult):
        return _result_out(detail).model_dump()
    return {"value": detail if isinstance(detail, (str, int, float, bool, type(None))) else str(detail)}


# -- app --------------------------------------------------------------------


def create_app(root: Path | str | None = None, *, engine: str | None = None) -> FastAPI:
    default_engine = engine or DEFAULT_ENGINE

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Load and validate at startup so a broken configuration fails on boot
        # rather than on the first request.
        State.load(root)
        yield

    app = FastAPI(
        title="Nimbus API",
        version=__version__,
        summary="One manager model and four specialists.",
        lifespan=lifespan,
    )

    if CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=CORS_ORIGINS,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    def orchestrator_for(options, language: str | None = None, chips=()) -> Orchestrator:
        """Build a fresh orchestrator per request.

        Orchestrators carry per-run state, so sharing one across concurrent
        requests would interleave usage counters and delegation results. Building
        one is cheap; the registry it reads is shared and already loaded.

        Per-request chips make that isolation load-bearing rather than merely
        tidy: they belong to one caller, and a shared orchestrator would carry
        them into somebody else's conversation.
        """
        return Orchestrator(
            State.get(),
            engine_override=options.engine or default_engine,
            thinking=options.thinking,
            solo=options.solo,
            search=options.search,
            language=language,
            extra_chips=[Chip.ad_hoc(c.name, c.content) for c in chips],
        )

    # -- description ---------------------------------------------------------

    @app.get("/v1/health", response_model=HealthResponse, tags=["describe"])
    def health() -> HealthResponse:
        registry = State.get()
        return HealthResponse(
            status="ok",
            version=__version__,
            base=registry.base.name,
            base_version=registry.base.version,
            models=len(registry.models),
            gears=len(registry.gears),
            chips=len(registry.chips),
            engine=default_engine,
        )

    @app.get("/v1/models", response_model=list[ModelOut], tags=["describe"])
    def list_models() -> list[ModelOut]:
        registry = State.get()
        return [_model_out(registry, spec) for spec in registry.models.values()]

    @app.get("/v1/models/{model_id}", response_model=ModelDetailOut, tags=["describe"])
    def get_model(model_id: str) -> ModelDetailOut:
        registry = State.get()
        spec = registry.get(model_id)
        orchestrator = Orchestrator(registry, engine_override="echo")
        base = _model_out(registry, spec)
        return ModelDetailOut(
            **base.model_dump(),
            card=spec.card,
            # The prompt itself is not exposed — only its size, which is enough for
            # a client to show cost context without publishing our prompts.
            system_prompt_length=len(orchestrator.model(model_id).system),
        )

    @app.get("/v1/gears", tags=["describe"])
    def list_gears() -> list[dict[str, Any]]:
        return [gear.to_ui_dict() for gear in State.get().gears]

    @app.get("/v1/chips", tags=["describe"])
    def list_chips() -> list[dict[str, Any]]:
        return [chip.to_ui_dict() for chip in State.get().chips]

    @app.get("/v1/languages", tags=["describe"])
    def get_languages() -> dict[str, Any]:
        return State.get().base.language.to_ui_dict()

    @app.post("/v1/parse", response_model=ParseResponse, tags=["describe"])
    def parse(body: ParseRequest) -> ParseResponse:
        return _parse_out(State.get().gears.parse(body.prompt))

    # -- work ----------------------------------------------------------------

    @app.post(
        "/v1/ask",
        response_model=AskResponse,
        tags=["work"],
        dependencies=[Depends(require_key)],
        responses={401: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
    )
    def ask(request: Request, body: AskRequest) -> AskResponse:
        run = orchestrator_for(body, body.language, body.chips).run(body.prompt)
        record_usage(request, run.usage)
        return _run_out(run)

    @app.post(
        "/v1/ask/stream",
        tags=["work"],
        dependencies=[Depends(require_key)],
        response_class=StreamingResponse,
    )
    def ask_stream(request: Request, body: AskRequest) -> StreamingResponse:
        """Server-sent events: progress while the family works, then the answer.

        The orchestrator is synchronous and reports progress by callback, so it runs
        on its own thread and pushes events into a queue that this generator drains.
        """
        events: queue.Queue = queue.Queue()
        SENTINEL = object()

        def work() -> None:
            try:
                run = orchestrator_for(body, body.language, body.chips).run(
                    body.prompt, on_event=lambda kind, detail: events.put((kind, detail))
                )
                # Charged from the worker thread, where the numbers are, and
                # before the "done" frame so a client that disconnects the
                # moment it arrives has still been billed for the work.
                record_usage(request, run.usage)
                events.put(("done", _run_out(run).model_dump()))
            except (EngineError, RegistryError) as exc:
                events.put(("error", {"error": type(exc).__name__, "detail": str(exc)}))
            finally:
                events.put(SENTINEL)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()

        def stream() -> Iterator[str]:
            while True:
                item = events.get()
                if item is SENTINEL:
                    return
                kind, detail = item
                payload = detail if kind in ("done", "error") else _event_payload(kind, detail)
                yield f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post(
        "/v1/chat/completions",
        tags=["work"],
        dependencies=[Depends(require_key)],
        responses={401: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
    )
    def chat_completions(body: ChatCompletionRequest) -> dict[str, Any]:
        """OpenAI-compatible chat completions.

        Exists so anything that already speaks to OpenAI -- a bot, an SDK, a
        desktop client -- can reach Nimbus by changing a base URL and a model
        name, with no knowledge of plans, gears or delegation.

        `model` selects what answers:

            nimbus                     the whole family: plan, delegate,
                                       synthesise
            nimbus-frontend-rza-1.0    one specialist, called directly
            (any other model id)       the same, for that model

        Only the last user message is used. Nimbus has no memory between runs,
        and quietly concatenating a history it cannot act on would spend tokens
        to create the appearance of continuity rather than the thing itself.

        Sampling parameters are accepted and ignored: the family sets its own
        per model, and the thinking ladder is not a temperature. Saying so here
        beats pretending to honour a number.
        """
        prompt = next(
            (m.content for m in reversed(body.messages)
             if m.role == "user" and m.content.strip()),
            "",
        )
        if not prompt.strip():
            raise HTTPException(status_code=400, detail="no user message to answer")

        registry = State.get()
        wants_family = body.model.strip().lower() in {"", "nimbus", "default", registry.manager.id}

        if wants_family:
            run = orchestrator_for(body).run(prompt)
            answer, usage = run.answer, run.usage
            served = registry.manager.id
        else:
            if body.model not in registry.models:
                raise HTTPException(
                    status_code=404,
                    detail=f"unknown model {body.model!r}. Use 'nimbus' for the family, "
                           f"or one of: {', '.join(sorted(registry.models))}",
                )
            orchestrator = orchestrator_for(body)
            model = orchestrator.model(body.model)
            result = model.ask(prompt, thinking=body.thinking, search=body.search)
            answer, usage = result.text, result.usage
            served = body.model

        prompt_tokens = int(usage.get("input_tokens", 0) or 0)
        completion_tokens = int(usage.get("output_tokens", 0) or 0)

        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": served,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    @app.post(
        "/v1/models/{model_id}/run",
        response_model=RunResponse,
        tags=["work"],
        dependencies=[Depends(require_key)],
    )
    def run_model(model_id: str, body: RunRequest) -> RunResponse:
        """Call one model directly, bypassing the manager and any gear routing."""
        orchestrator = orchestrator_for(body)
        model = orchestrator.model(model_id)
        level = model.resolve_thinking(body.thinking)
        result = model.ask(body.prompt, thinking=level, search=body.search)
        return RunResponse(
            model=model_id,
            engine_model=result.model,
            output=result.text,
            thinking=level,
            searches=result.searches,
            stop_reason=result.stop_reason,
            usage=result.usage,
        )

    # -- admin ---------------------------------------------------------------

    @app.post("/v1/reload", tags=["admin"], dependencies=[Depends(require_key)])
    def reload() -> dict[str, Any]:
        """Re-read everything from disk. Use after installing or removing a chip."""
        registry = State.load(State.root)
        return {
            "reloaded": True,
            "models": len(registry.models),
            "gears": len(registry.gears),
            "chips": len(registry.chips),
        }

    # -- errors --------------------------------------------------------------

    @app.exception_handler(RegistryError)
    async def registry_error(request: Request, exc: RegistryError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"error": "RegistryError", "detail": str(exc)})

    @app.exception_handler(EngineError)
    async def engine_error(request: Request, exc: EngineError):
        from fastapi.responses import JSONResponse

        # The engine is upstream of us; its failure is a bad gateway, not a bad request.
        return JSONResponse(status_code=502, content={"error": "EngineError", "detail": str(exc)})

    @app.exception_handler(ChipError)
    async def chip_error(request: Request, exc: ChipError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=400, content={"error": "ChipError", "detail": str(exc)})

    # -- legal pages ---------------------------------------------------------

    # Opt-in, and deliberately not "on whenever the directory exists".
    #
    # A host that deploys the whole repository — Render's Python runtime does —
    # puts legal/ on disk whether or not anyone meant to publish it, so presence
    # is not consent. STATUS.txt section 7 records that privacy.html section 4
    # states commitments that are not yet true of any running system (that API
    # traffic never trains models, and that a chat training toggle defaults to
    # off, when there is no pipeline and no toggle), that all four pages are
    # drafts, and that two placeholders still need a lawyer. Publishing that
    # text before it is true would be a false statement, so serving it has to be
    # a decision someone takes on purpose.
    #
    # Set NIMBUS_SERVE_LEGAL=1 once the text is true and reviewed.
    serve_legal = os.environ.get("NIMBUS_SERVE_LEGAL", "").strip().lower() in {"1", "true", "yes", "on"}
    legal_dir = (Path(root).resolve() if root else Path(__file__).resolve().parents[2]) / "legal"
    if serve_legal and legal_dir.is_dir():
        # Served from the API so a chat client can embed them from one origin.
        app.mount("/legal", StaticFiles(directory=legal_dir, html=True), name="legal")

    return app
