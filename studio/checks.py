"""Loading a task suite and scoring an answer against it.

Split out of evaluate.py, which imports the engines, which import the HTTP
layer, which imports the providers. None of that is needed to decide whether a
piece of text contains "CSS Grid" or is written in Hangul -- and needing it is
what stops the scoring from being reused anywhere the engines cannot be
installed. A Kaggle notebook holding a fine-tuned adapter is exactly that
place: it has torch and no Nimbus server, and it still has to be scored by the
same checks as everything else, or the comparison is against a different ruler.

So: no engines, no registry, no network. Tasks in, a verdict out.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


BASELINE_SYSTEM = "You are a helpful assistant."

# Rough script ranges, enough to tell whether an answer came back in the script the
# task asked for. Not a language detector — a smoke check.
SCRIPTS: dict[str, tuple[tuple[int, int], ...]] = {
    "latin": ((0x0041, 0x024F),),
    "arabic": ((0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF)),
    "cyrillic": ((0x0400, 0x04FF),),
    "devanagari": ((0x0900, 0x097F),),
    # Hangul is here because a task asks for Korean and declares
    # expect_script = "cjk". Without these ranges a perfect Korean answer
    # scores 0% and the task cannot pass at any threshold -- the check was
    # measuring the alphabet the answer was not written in.
    "cjk": ((0x4E00, 0x9FFF), (0x3040, 0x30FF), (0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F)),
    "hebrew": ((0x0590, 0x05FF),),
}


class EvalError(RuntimeError):
    """An evaluation suite is malformed."""


# -- tasks ------------------------------------------------------------------


@dataclass(frozen=True)
class Task:
    id: str
    prompt: str
    kind: str                      # "routing" | "specialist"
    model: str | None = None       # required for kind="specialist"
    note: str = ""

    # routing expectations
    expect_direct: bool | None = None
    expect_models: tuple[str, ...] = ()
    forbid_models: tuple[str, ...] = ()
    expect_gears: tuple[str, ...] = ()

    # output expectations
    must_include: tuple[str, ...] = ()
    must_exclude: tuple[str, ...] = ()
    must_match: tuple[str, ...] = ()
    expect_script: str | None = None
    expect_code_block: bool | None = None
    thinking: str | None = None


@dataclass(frozen=True)
class Suite:
    name: str
    description: str
    tasks: tuple[Task, ...]
    path: Path


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class TaskResult:
    task: Task
    checks: list[CheckResult] = field(default_factory=list)
    output: str = ""
    seconds: float = 0.0
    usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    # With --repeat, how many of N runs passed. A task that passes sometimes is
    # not a passing task — generation is non-deterministic, so a single green run
    # proves less than it looks like it does.
    runs_passed: int = 1
    runs_total: int = 1
    # Set when this arm cannot attempt the task at all — a bare model has no
    # roster and no gears, so routing is not a comparable measurement. A skip is
    # never a pass; counting it as one would inflate the baseline against a suite
    # it never ran.
    skipped: bool = False
    skip_reason: str = ""

    @property
    def passed(self) -> bool:
        if self.skipped or self.error is not None:
            return False
        if self.runs_total > 1:
            # Every run must pass. Anything less is flaky, and flaky is not passing.
            return self.runs_passed == self.runs_total
        return all(c.passed for c in self.checks)

    @property
    def flaky(self) -> bool:
        return 0 < self.runs_passed < self.runs_total

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]


@dataclass
class SuiteResult:
    suite: Suite
    arm: str                       # "nimbus" | "baseline"
    results: list[TaskResult] = field(default_factory=list)

    @property
    def attempted(self) -> list[TaskResult]:
        """Tasks this arm could actually run. Skips are excluded, not counted."""
        return [r for r in self.results if not r.skipped]

    @property
    def total(self) -> int:
        return len(self.attempted)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.skipped)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.attempted if r.passed)

    @property
    def rate(self) -> float | None:
        """None when nothing was attempted — distinct from a genuine 0%."""
        return self.passed / self.total if self.total else None

    @property
    def usage(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for result in self.results:
            for key, value in result.usage.items():
                totals[key] = totals.get(key, 0) + value
        return totals


# -- loading ----------------------------------------------------------------


def load_suite(path: Path) -> Suite:
    with path.open("rb") as handle:
        try:
            data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise EvalError(f"invalid TOML in {path}: {exc}") from exc

    tasks: list[Task] = []
    for index, raw in enumerate(data.get("task", [])):
        try:
            kind = raw.get("kind", "routing")
            if kind == "specialist" and not raw.get("model"):
                raise EvalError(f"{path}: task {raw.get('id', index)} is kind=specialist but names no model")
            tasks.append(
                Task(
                    id=raw["id"],
                    prompt=raw["prompt"],
                    kind=kind,
                    model=raw.get("model"),
                    note=raw.get("note", ""),
                    expect_direct=raw.get("expect_direct"),
                    expect_models=tuple(raw.get("expect_models", [])),
                    forbid_models=tuple(raw.get("forbid_models", [])),
                    expect_gears=tuple(raw.get("expect_gears", [])),
                    must_include=tuple(raw.get("must_include", [])),
                    must_exclude=tuple(raw.get("must_exclude", [])),
                    must_match=tuple(raw.get("must_match", [])),
                    expect_script=raw.get("expect_script"),
                    expect_code_block=raw.get("expect_code_block"),
                    thinking=raw.get("thinking"),
                )
            )
        except KeyError as exc:
            raise EvalError(f"{path}: task at index {index} is missing {exc}") from exc

    return Suite(
        name=data.get("suite", path.stem),
        description=data.get("description", ""),
        tasks=tuple(tasks),
        path=path,
    )


def load_suites(
    eval_dir: Path,
    only: Iterable[str] = (),
    tasks: Iterable[str] = (),
) -> list[Suite]:
    """Load suites, optionally narrowed to named suites and named tasks.

    Task filtering exists because free-tier quotas are small: a focused six-task
    comparison that actually completes beats a forty-two-task one that dies
    halfway and measures nothing.
    """
    if not eval_dir.is_dir():
        raise EvalError(f"no eval directory at {eval_dir}")

    suites = [load_suite(path) for path in sorted(eval_dir.glob("*.toml"))]

    # Task ids appear in command-line filters and comparison reports, so they
    # must name one scenario globally. A duplicate would silently make `--task`
    # select more work than requested and make cross-suite reporting ambiguous.
    seen_ids: set[str] = set()
    duplicate_ids: set[str] = set()
    for suite in suites:
        for task in suite.tasks:
            normalized = task.id.lower()
            if normalized in seen_ids:
                duplicate_ids.add(task.id)
            seen_ids.add(normalized)
    if duplicate_ids:
        raise EvalError(f"duplicate task ids across eval suites: {sorted(duplicate_ids)}")

    wanted = {name.lower() for name in only}
    if wanted:
        suites = [s for s in suites if s.name.lower() in wanted]
        if not suites:
            raise EvalError(f"no suite matched {sorted(wanted)}")

    wanted_tasks = {name.lower() for name in tasks}
    if wanted_tasks:
        narrowed: list[Suite] = []
        seen: set[str] = set()
        for suite in suites:
            keep = tuple(t for t in suite.tasks if t.id.lower() in wanted_tasks)
            seen.update(t.id.lower() for t in keep)
            if keep:
                narrowed.append(
                    Suite(
                        name=suite.name,
                        description=suite.description,
                        tasks=keep,
                        path=suite.path,
                    )
                )
        missing = wanted_tasks - seen
        if missing:
            raise EvalError(f"no task matched {sorted(missing)}")
        suites = narrowed

    return suites


# -- checks -----------------------------------------------------------------


# Code is exempt from the language rule, and the rule itself says so: prose,
# headings and explanation go in the person's language, while "code, identifiers,
# commands and file paths stay exactly as they are written in code". Counting
# every letter in the answer counted the code as evidence against it.
#
# It stayed invisible while answers were short. At 347 characters -- the median
# of the Persian samples this project had -- a snippet is a small share of the
# letters. Ask the same model for two thousand characters and the same correct
# answer falls from 88% to 57% and then through the floor, so the check rejects
# Persian for containing JavaScript. Measured over prose alone, those same
# answers score 79% to 92%.
#
# That is also why every Persian sample here is short: the long ones were made
# and thrown away, and the shape of the data came from the shape of the check.
_CODE_SPAN = re.compile(r"\`\`\`.*?\`\`\`|\`[^\`\n]*\`", re.S)


def _prose(text: str) -> str:
    """The part of an answer the language rule governs."""
    return _CODE_SPAN.sub(" ", text)


def _script_ratio(text: str, script: str) -> float:
    ranges = SCRIPTS.get(script.lower())
    if ranges is None:
        raise EvalError(f"unknown script {script!r}. known: {', '.join(sorted(SCRIPTS))}")
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    hits = sum(1 for c in letters if any(lo <= ord(c) <= hi for lo, hi in ranges))
    return hits / len(letters)


def check_output(task: Task, output: str) -> list[CheckResult]:
    checks: list[CheckResult] = []
    lowered = output.lower()

    for needle in task.must_include:
        checks.append(
            CheckResult(f"includes {needle!r}", needle.lower() in lowered)
        )

    for needle in task.must_exclude:
        found = needle.lower() in lowered
        checks.append(
            CheckResult(f"excludes {needle!r}", not found, "found" if found else "")
        )

    for pattern in task.must_match:
        checks.append(
            CheckResult(f"matches /{pattern}/", re.search(pattern, output, re.I | re.M) is not None)
        )

    if task.expect_code_block is not None:
        has_block = "```" in output
        checks.append(
            CheckResult(
                "has fenced code block" if task.expect_code_block else "no fenced code block",
                has_block == task.expect_code_block,
            )
        )

    if task.expect_script:
        ratio = _script_ratio(_prose(output), task.expect_script)
        checks.append(
            CheckResult(
                f"written in {task.expect_script}",
                ratio >= 0.5,
                f"{ratio:.0%} of letters in prose",
            )
        )

    return checks


def check_routing(task: Task, plan, gear_ids: list[str]) -> list[CheckResult]:
    checks: list[CheckResult] = []
    targeted = [d.model for d in plan.delegations]

    if task.expect_direct is not None:
        checks.append(
            CheckResult(
                "answers directly" if task.expect_direct else "delegates",
                plan.handle_directly == task.expect_direct,
                f"routed to {targeted}" if targeted else "handled directly",
            )
        )

    for model_id in task.expect_models:
        checks.append(
            CheckResult(f"delegates to {model_id}", model_id in targeted, f"got {targeted}")
        )

    for model_id in task.forbid_models:
        checks.append(
            CheckResult(f"does not use {model_id}", model_id not in targeted, f"got {targeted}")
        )

    for gear_id in task.expect_gears:
        checks.append(CheckResult(f"fires gear {gear_id}", gear_id in gear_ids, f"got {gear_ids}"))

    return checks


# -- running ----------------------------------------------------------------
