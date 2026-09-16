"""The Orchestrator–Workers loop: parse gears, plan, delegate, synthesize.

The manager model owns every decision here — which specialist runs, what it is told,
and how hard it thinks. This module only carries messages; it never routes on its own.

The one exception is gears. A router gear is a routing instruction the person gave
explicitly, so it is enforced in code after planning rather than left to the model's
judgement.
"""

from __future__ import annotations

from typing import Sequence

from .chips import Chip
from .compose import describe_roster
from .contract import Delegation, DelegationResult, Plan, Run, plan_schema
from .engines import EngineError
from .engines.base import IMAGE_PROVIDERS
from .gears import ParsedRequest
from .model import NimbusModel
from .registry import Registry, RegistryError

PLANNING_PROMPT = """\
Decide how this request should be handled.

Routing priority: a request to diagnose, debug, explain a failure, investigate an
incident, review code for defects, or interpret an error message, stack trace, or
failing test MUST delegate to `nimbus-debugger-rza-1.0`. This rule takes priority
over the normal preference to answer simple questions directly. Give the debugger
`xhigh` thinking unless the request is truly trivial.

Available specialists:
{roster}

Thinking levels you can assign to a delegation:
{levels}
{gears}
Request:
\"\"\"
{request}
\"\"\"

Set handle_directly to true unless the request needs real implementation work in a
specialist's domain. Answering a question, explaining something, or making a small
change you can already see is faster and better done by you.

If you do delegate, write each brief so it stands alone: the specialist sees only
what you write, with no access to this conversation. Assign each one a thinking
level matched to how hard its task actually is.
"""

GEARS_SECTION = """
Gears the person invoked in this request — honour every one:
{gears}
"""

SYNTHESIS_PROMPT = """\
The specialists you briefed have reported back. Write the answer for the person.

Original request:
\"\"\"
{request}
\"\"\"

Specialist output:
{results}

Present this as one answer from one team. Resolve any contradictions between
specialists rather than handing over both versions, carry through every NOTE: line,
and add the connective detail only you have context for — what fits where, what
depends on what, what to run first.
"""


# An image model draws what it is handed. Hand it Persian and it draws Persian --
# as shapes, or not at all. Neither is the picture that was asked for, and the
# provider has no translation step to lean on, so one is needed before the call.
IMAGE_SCENE_ASK = (
    "Rewrite this as one English sentence describing what to draw: the subject, "
    "the setting, the style. Nothing else -- no preamble, no quotes, no "
    "instructions, and no words that are meant to appear inside the picture."
    "\n\nRequest:\n"
)

# Above this, the brief is already in a script an image provider was trained on
# and a translation call would cost a request to change nothing.
LATIN_ENOUGH = 0.7


def _latin_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 1.0  # digits and punctuation need no translating
    return sum(1 for c in letters if ord(c) < 0x0250) / len(letters)


class Orchestrator:
    """Runs a request through the family."""

    def __init__(
        self,
        registry: Registry,
        *,
        engine_override: str | None = None,
        thinking: str | None = None,
        search: bool = True,
        language: str | None = None,
        extra_chips: Sequence[Chip] = (),
        solo: bool = False,
    ) -> None:
        self.registry = registry
        # Chips supplied with this request rather than installed on disk. Held
        # per-orchestrator, which is safe because one is built per request --
        # sharing an orchestrator would leak one person's chips into another's
        # conversation.
        self.extra_chips = tuple(extra_chips)
        self.engine_override = engine_override
        # An explicit output language. Left unset, the manager matches whatever
        # language the person wrote in.
        self.language = registry.base.language.resolve(language) if language else None
        # A global thinking override: when set, it wins over model defaults, the
        # manager's per-delegation choice, and any gear's preference.
        self.thinking_override = (
            registry.base.thinking.validate_level(thinking) if thinking else None
        )
        self.search = search
        # Answer with one model instead of the family: no planning call, no
        # delegation, no synthesis.
        #
        # The team is the point of this runtime, so this is not a default. It is
        # for the cases where the team is the wrong shape of answer -- a one-line
        # question that does not need a manager deciding who takes it, or a
        # person who already knows which specialist they want and is paying for
        # three model calls to be told so.
        self.solo = solo
        self._cache: dict[str, NimbusModel] = {}
        self._last_usage: dict[str, int] = {}

    # -- public ------------------------------------------------------------

    def model(self, model_id: str) -> NimbusModel:
        if model_id not in self._cache:
            spec = self.registry.get(model_id)
            self._cache[model_id] = NimbusModel.build(
                self.registry.base,
                spec,
                engine_override=self.engine_override,
                # Installed chips first, then per-request ones. Both land after
                # the model's own prompt, so neither can redefine what it is;
                # ordering here only decides which refines the other.
                chips=(
                    *self.registry.chips.for_model(model_id),
                    *(c for c in self.extra_chips if c.applies_to(model_id)),
                ),
            )
        return self._cache[model_id]

    def plan_only(self, request: str) -> tuple[Plan, ParsedRequest]:
        """Produce the routing decision without executing it.

        One model call instead of the whole loop — which is what makes routing
        evaluation cheap enough to run often.
        """
        parsed = self.registry.gears.parse(request)
        manager = self.model(self.registry.manager.id)
        return self._enforce_gears(self._plan(manager, parsed), parsed), parsed

    def run(self, request: str, *, on_event=None) -> Run:
        """Parse gears, plan, delegate, and synthesize.

        `on_event(kind, detail)` reports progress.
        """
        emit = on_event or (lambda kind, detail: None)

        parsed = self.registry.gears.parse(request)
        if parsed.invocations or parsed.unknown:
            emit("gears:parsed", parsed)

        if self.solo:
            return self._run_solo(request, parsed, emit)

        manager = self.model(self.registry.manager.id)

        emit("plan:start", manager.id)
        plan = self._enforce_gears(self._plan(manager, parsed), parsed)
        run = Run(request=request, plan=plan, gears=[gear.id for gear in parsed.gears])
        run.add_usage(self._last_usage)
        emit("plan:done", plan)

        if plan.handle_directly:
            emit("answer:start", manager.id)
            result = manager.ask(
                self._with_language(self._with_modifiers(parsed.text, parsed)),
                thinking=self.thinking_override,
                search=self.search,
            )
            run.answer = result.text
            run.add_usage(result.usage)
            run.searches += result.searches
            emit("answer:done", run.answer)
            return run

        for delegation in plan.delegations:
            emit("delegate:start", delegation)
            outcome = self._delegate(delegation, parsed)
            run.results.append(outcome)
            run.add_usage(self._last_usage)
            run.searches += outcome.searches
            emit("delegate:done", outcome)

        emit("synthesis:start", manager.id)
        result = manager.ask(
            self._with_language(
                SYNTHESIS_PROMPT.format(
                    request=parsed.text, results=self._format_results(run.results)
                )
            ),
            thinking=self.thinking_override,
            search=False,   # synthesis works from what came back, not from the web
        )
        run.answer = result.text
        run.add_usage(result.usage)
        emit("synthesis:done", run.answer)
        return run

    def _run_solo(self, request: str, parsed: ParsedRequest, emit) -> Run:
        """One model, one call.

        The planning call is skipped rather than made and ignored. Skipping it is
        most of the saving: planning is a full model call before any work starts,
        and on a short question it costs more than the answer does.

        A router gear still wins. The person typed `@frontend` because they know
        who they want, and refusing to honour that in the name of "no team" would
        be obeying the letter of the switch against its purpose -- they asked for
        one model, and naming it is the clearest way to ask for one model.

        Two router gears is a genuine contradiction: the tokens ask for two
        models and the switch asks for one. The switch wins, because it is the
        mode they set deliberately rather than something typed mid-sentence, and
        the rationale says which token was dropped so the answer is not quietly
        narrower than the question.
        """
        routers = parsed.routers
        if routers:
            gear = routers[0]
            why = f"solo: {gear.token} answered directly, without a manager."
            dropped = [g.token for g in routers[1:]]
            if dropped:
                why += f" Solo runs one model, so {' '.join(dropped)} did not run."

            # Through _delegate rather than calling the specialist here, because
            # that path already carries three things this one needs and none of
            # them are visible until they are missing: EngineError is caught and
            # returned as a failed result instead of killing the request, an
            # image brief is put into English before an image provider sees it,
            # and the thinking level is resolved against the model rather than
            # passed through raw.
            #
            # The language instruction goes into the brief here, though. In team
            # mode the specialist answers in whatever language and the manager's
            # synthesis sets the output language; solo has no synthesis, so the
            # specialist's own answer is the one the person reads.
            delegation = Delegation(
                model=gear.handler,
                brief=self._with_language(f"{parsed.text}\n\n{gear.instruction}".strip()),
                thinking=gear.thinking,
                gear=gear.id,
            )
            plan = Plan(handle_directly=False, rationale=why, delegations=(delegation,))
            emit("plan:done", plan)
            run = Run(request=request, plan=plan,
                      gears=[g.id for g in parsed.gears])

            emit("delegate:start", delegation)
            outcome = self._delegate(delegation, parsed)
            run.results.append(outcome)
            run.add_usage(self._last_usage)
            run.searches += outcome.searches
            emit("delegate:done", outcome)

            # No synthesis to turn a failure into prose, so the failure has to
            # be the answer. An empty one would render as the model having
            # nothing to say.
            run.answer = outcome.output if outcome.ok else (
                f"{gear.handler} could not run: {outcome.error}")
            emit("answer:done", run.answer)
            return run

        model = self.model(self.registry.manager.id)
        plan = Plan(
            handle_directly=True,
            rationale="solo: answered directly, with no planning and no delegation.",
            delegations=(),
        )
        emit("plan:done", plan)
        run = Run(request=request, plan=plan, gears=[g.id for g in parsed.gears])

        emit("answer:start", model.id)
        result = model.ask(
            self._with_language(self._with_modifiers(parsed.text, parsed)),
            thinking=self.thinking_override,
            search=self.search,
        )
        run.answer = result.text
        run.add_usage(result.usage)
        run.searches += result.searches
        emit("answer:done", run.answer)
        return run

    # -- steps -------------------------------------------------------------

    def _plan(self, manager: NimbusModel, parsed: ParsedRequest) -> Plan:
        self._last_usage = {}
        ladder = self.registry.base.thinking
        roster = describe_roster(self.registry.models, manager.spec.roster)
        levels = "\n".join(
            f"- {level}: {ladder.descriptions.get(level, '')}".rstrip()
            for level in ladder.levels
        )

        described = self.registry.gears.describe(parsed)
        gears = GEARS_SECTION.format(gears=described) if described else "\n"

        prompt = PLANNING_PROMPT.format(
            roster=roster, levels=levels, gears=gears, request=parsed.text
        )

        try:
            parsed_plan, result = manager.ask_json(
                prompt, plan_schema(ladder.levels), thinking=self.thinking_override
            )
        except EngineError as exc:
            # A failed plan should not sink the request — answer it directly. But
            # mark it, so nothing downstream mistakes a dead call for a decision.
            return Plan.unplanned(f"planning call failed: {exc}")

        self._last_usage = result.usage

        plan = Plan.from_dict(parsed_plan, levels=ladder.levels)
        known = set(self.registry.models)
        unknown = [d.model for d in plan.delegations if d.model not in known]
        if unknown:
            return Plan.direct(f"plan named unknown specialists {unknown}; answering directly")
        return plan

    def _enforce_gears(self, plan: Plan, parsed: ParsedRequest) -> Plan:
        """Guarantee every router gear reached its handler.

        The manager is told about the gears and usually routes correctly. This is
        the backstop: the person typed the token, so the delegation happens whether
        or not the model remembered it.
        """
        routers = parsed.routers
        if not routers:
            return plan

        delegations = list(plan.delegations)
        targeted = {d.model for d in delegations}

        for gear in routers:
            if gear.handler in targeted:
                continue
            delegations.append(
                Delegation(
                    model=gear.handler,
                    brief=f"{parsed.text}\n\n{gear.instruction}".strip(),
                    thinking=gear.thinking,
                    gear=gear.id,
                )
            )
            targeted.add(gear.handler)

        if not delegations:
            return plan

        forced = [gear.token for gear in routers]
        rationale = plan.rationale
        if plan.handle_directly:
            rationale = f"gear {' '.join(forced)} requires delegation. {rationale}".strip()

        return Plan(handle_directly=False, rationale=rationale, delegations=tuple(delegations))

    def _scene_for_image(self, model_id: str, brief: str) -> str:
        """Put a brief into English before an image provider sees it.

        Only for image models, and only when the brief is not already mostly
        Latin. A gear backstop builds its brief from the raw request text, so
        `@CR-image یک گربه بکش` reached the provider in Persian and came back as
        a picture of Persian-shaped marks.

        A failed translation returns the brief unchanged rather than raising: a
        worse picture beats no picture, and the delegation error would say
        nothing about why.
        """
        try:
            provider = (self.registry.get(model_id).engine.provider or "").lower()
        except RegistryError:
            return brief
        if provider not in IMAGE_PROVIDERS or _latin_ratio(brief) >= LATIN_ENOUGH:
            return brief

        manager = self.model(self.registry.manager.id)
        try:
            result = manager.ask(
                IMAGE_SCENE_ASK + brief,
                thinking=manager.resolve_thinking(None),
                search=False,
            )
        except EngineError:
            return brief
        return result.text.strip() or brief

    def _delegate(self, delegation: Delegation, parsed: ParsedRequest) -> DelegationResult:
        self._last_usage = {}
        model_id = delegation.model
        brief = self._with_modifiers(delegation.brief, parsed)
        brief = self._scene_for_image(model_id, brief)
        level = self.thinking_override or delegation.thinking

        try:
            specialist = self.model(model_id)
            resolved = specialist.resolve_thinking(level)
            result = specialist.ask(brief, thinking=resolved, search=self.search)
        except EngineError as exc:
            return DelegationResult(
                model=model_id,
                brief=brief,
                output="",
                thinking=level or "",
                gear=delegation.gear,
                error=str(exc),
            )

        self._last_usage = result.usage

        # An empty answer is a failed delegation, whatever the engine thought.
        #
        # Passed through as a success, it reaches synthesis as a specialist that
        # ran and returned nothing, and the manager cannot tell that apart from
        # output it is failing to read -- so it writes an account of the silence
        # rather than reporting the failure. Naming it here means the manager is
        # told a specialist failed, which it does know how to report.
        if not result.text.strip():
            return DelegationResult(
                model=model_id,
                brief=brief,
                output="",
                thinking=resolved,
                searches=result.searches,
                gear=delegation.gear,
                error=f"{model_id} returned an empty answer.",
            )

        return DelegationResult(
            model=model_id,
            brief=brief,
            output=result.text,
            thinking=resolved,
            searches=result.searches,
            gear=delegation.gear,
        )

    def _with_language(self, text: str) -> str:
        """Pin the answer's language when one was requested explicitly.

        Only the manager's user-facing turns get this. Specialist briefs stay in the
        pivot language, because that is where the specialists work.
        """
        if self.language is None:
            return text

        label = self.language.name
        if self.language.endonym and self.language.endonym != label:
            label = f"{label} ({self.language.endonym})"

        route = "" if self.language.native else " Work the problem in the pivot language and deliver in the target."
        return f"{text}\n\nWrite your answer to the person in {label}.{route}"

    @staticmethod
    def _with_modifiers(text: str, parsed: ParsedRequest) -> str:
        """Append every modifier gear's requirement to a prompt."""
        modifiers = parsed.modifiers
        if not modifiers:
            return text
        extra = "\n\n".join(gear.instruction for gear in modifiers)
        return f"{text}\n\n{extra}".strip()

    @staticmethod
    def _format_results(results: list[DelegationResult]) -> str:
        if not results:
            return "(nothing came back)"
        chunks = []
        for item in results:
            body = item.output if item.ok else f"FAILED: {item.error}"
            label = f"{item.model} (via {item.gear})" if item.gear else item.model
            chunks.append(f"### {label}\n{body}")
        return "\n\n".join(chunks)
