"""Evaluation harness.

Answers three questions that nothing else in this project can:

1. **Does the manager route correctly?** Including the harder half — does it
   correctly decide *not* to delegate?
2. **Does each specialist honour its contract?** Output shape, domain boundaries,
   the rules its blocks state.
3. **Does the Nimbus layer beat the bare base model?** Run any suite with
   `baseline=True` and the same checks run against the same engine with a minimal
   system prompt, no blocks, no chips, no delegation. If Nimbus does not win, the
   layer is not earning its cost — and that is worth knowing before spending money
   on fine-tuning.

Checks are deliberately mechanical: string, regex, structural, and routing
assertions, no model-as-judge. A mechanical check is cheap, deterministic, and
cannot flatter us. It cannot judge prose quality — that needs human review, and the
harness reports what it did not check rather than pretending otherwise.
"""

from __future__ import annotations

import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .engines import EngineError, build_engine
from .orchestrator import Orchestrator
from .registry import Registry

# A neutral prompt for the baseline arm: what you would get from the same engine
# with no Nimbus layer at all.
# The suite format and every check live in checks.py, which has no engine
# imports -- so a notebook with torch and no Nimbus server can score against
# exactly these rules rather than a re-typed approximation of them.
from .checks import (  # noqa: F401  (re-exported: callers import these from here)
    BASELINE_SYSTEM,
    SCRIPTS,
    CheckResult,
    EvalError,
    Suite,
    SuiteResult,
    Task,
    TaskResult,
    _script_ratio,
    check_output,
    check_routing,
    load_suite,
    load_suites,
)


class Evaluator:
    """Runs suites against either the Nimbus layer or a bare-model baseline."""

    def __init__(
        self,
        registry: Registry,
        *,
        engine: str = "echo",
        thinking: str | None = None,
        baseline: bool = False,
        delay: float = 0.0,
        repeat: int = 1,
    ) -> None:
        self.registry = registry
        self.engine = engine
        self.thinking = thinking
        self.baseline = baseline
        # How many times to run each task. Above 1, a task counts as passing only
        # if every run passes — which is how a flaky check gets caught instead of
        # being mistaken for a regression, or for a fix.
        self.repeat = max(1, repeat)
        # Seconds to pause between tasks. Free tiers cap requests per minute, and
        # pacing under the cap is cheaper than retrying past it.
        self.delay = delay

    @property
    def arm(self) -> str:
        return "baseline" if self.baseline else "nimbus"

    def run_suite(self, suite: Suite, *, on_task=None) -> SuiteResult:
        result = SuiteResult(suite=suite, arm=self.arm)
        for index, task in enumerate(suite.tasks):
            if self.delay and index:
                time.sleep(self.delay)
            outcome = self.run_task(task)
            result.results.append(outcome)
            if on_task:
                on_task(outcome)
        return result

    def run_task(self, task: Task) -> TaskResult:
        """Run a task, repeating it when configured, and report the aggregate."""
        if self.repeat == 1:
            return self._run_once(task)

        runs = []
        for index in range(self.repeat):
            if self.delay and index:
                time.sleep(self.delay)
            runs.append(self._run_once(task))
            if runs[-1].skipped:
                # Out of quota mid-repeat; report what we have rather than
                # pretending the remaining runs failed.
                break

        attempted = [r for r in runs if not r.skipped]
        if not attempted:
            return runs[-1]

        # Keep the first failing run's detail so -v still shows why it failed.
        representative = next((r for r in attempted if not r.passed), attempted[0])
        representative.runs_total = len(attempted)
        representative.runs_passed = sum(1 for r in attempted if r.passed)
        representative.usage = {}
        for run in attempted:
            for key, value in run.usage.items():
                representative.usage[key] = representative.usage.get(key, 0) + value
        return representative

    def _run_once(self, task: Task) -> TaskResult:
        started = time.monotonic()
        outcome = TaskResult(task=task)
        try:
            if task.kind == "routing":
                self._run_routing(task, outcome)
            elif task.kind == "specialist":
                self._run_specialist(task, outcome)
            else:
                raise EvalError(f"unknown task kind {task.kind!r}")
        except EngineError as exc:
            # The call never produced output — quota, network, provider fault. That
            # is not the model failing a check, so it is skipped rather than scored.
            # Counting it as a failure inflates the other arm's advantage, which is
            # exactly the mistake that made an early specialists run read +69% when
            # the real figure was +50 over the tasks both arms actually completed.
            outcome.skipped = True
            outcome.skip_reason = str(exc).split("\n")[0][:120]
        except EvalError as exc:
            # A malformed task is our bug, not the provider's. Surface it loudly.
            outcome.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - one bad task must not sink the run
            outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.seconds = time.monotonic() - started
        return outcome

    # -- arms -------------------------------------------------------------

    def _run_routing(self, task: Task, outcome: TaskResult) -> None:
        if self.baseline:
            # A bare model has no roster and no concept of delegation, so routing
            # is not a comparable measurement.
            outcome.skipped = True
            outcome.skip_reason = "routing is not applicable to a bare model"
            return

        orchestrator = Orchestrator(
            self.registry, engine_override=self.engine, thinking=self.thinking
        )
        plan, parsed = orchestrator.plan_only(task.prompt)

        # The briefs are part of what a routing decision produces, so expose them
        # for content checks — otherwise there is no way to assert anything about
        # what the manager actually told a specialist.
        outcome.output = "\n\n".join(
            [plan.rationale, *(d.brief for d in plan.delegations)]
        ).strip()

        if plan.failed:
            # No decision was made, so there is nothing to score. Skipped, not
            # failed — same rule as the specialist arm: a dead API call must never
            # be counted against the model, in either direction.
            outcome.skipped = True
            outcome.skip_reason = plan.rationale.split("\n")[0][:120]
            return

        outcome.checks = check_routing(task, plan, [g.id for g in parsed.gears])
        outcome.checks.extend(check_output(task, outcome.output))

    def _run_specialist(self, task: Task, outcome: TaskResult) -> None:
        spec = self.registry.get(task.model)

        if self.baseline:
            # Same engine, same model id, same prompt — minus everything Nimbus adds.
            engine = build_engine(spec.engine, override=self.engine)
            level = task.thinking or self.thinking or spec.thinking_default
            result = engine.generate(
                system=BASELINE_SYSTEM,
                messages=[{"role": "user", "content": task.prompt}],
                max_tokens=spec.engine.max_tokens,
                thinking=level,
                tools=None,
            )
        else:
            orchestrator = Orchestrator(
                self.registry, engine_override=self.engine, thinking=self.thinking
            )
            model = orchestrator.model(task.model)
            result = model.ask(task.prompt, thinking=task.thinking or self.thinking)

        outcome.output = result.text
        outcome.usage = result.usage
        outcome.checks = check_output(task, result.text)


# -- reporting --------------------------------------------------------------


def compare(nimbus: list[SuiteResult], baseline: list[SuiteResult]) -> dict[str, Any]:
    """Side-by-side pass rates. The number this whole project turns on.

    Only tasks both arms attempted are compared. A suite the baseline could not run
    reports `null`, never a number — a delta computed against a skipped arm looks
    like a measurement and is not one.
    """
    by_name = {r.suite.name: r for r in baseline}
    rows = []
    comparable_total = 0
    comparable_nimbus = 0
    comparable_baseline = 0

    for run in nimbus:
        other = by_name.get(run.suite.name)
        run_by_id = {result.task.id: result for result in run.attempted}
        baseline_by_id = (
            {result.task.id: result for result in other.attempted} if other is not None else {}
        )
        common_ids = run_by_id.keys() & baseline_by_id.keys()
        compared = len(common_ids)

        nimbus_passed = sum(1 for task_id in common_ids if run_by_id[task_id].passed)
        baseline_passed = sum(
            1 for task_id in common_ids if baseline_by_id[task_id].passed
        )
        nimbus_rate = nimbus_passed / compared if compared else None
        baseline_rate = baseline_passed / compared if compared else None
        delta = nimbus_rate - baseline_rate if compared else None

        if compared:
            comparable_total += compared
            comparable_nimbus += nimbus_passed
            comparable_baseline += baseline_passed

        rows.append(
            {
                "suite": run.suite.name,
                "nimbus": nimbus_rate,
                "baseline": baseline_rate,
                "delta": delta,
                "total": compared,
                "skipped_by_baseline": other.skipped if other else run.total,
            }
        )

    return {
        "suites": rows,
        "overall": {
            "nimbus": comparable_nimbus / comparable_total if comparable_total else None,
            "baseline": comparable_baseline / comparable_total if comparable_total else None,
            "comparable_tasks": comparable_total,
            "not_comparable": sum(r.total for r in nimbus) - comparable_total,
        },
    }
