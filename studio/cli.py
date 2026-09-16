"""Command-line tool for testing the Nimbus family.

This is a test harness, not a product. No client ships from this repository.

    python -m studio doctor
    python -m studio list
    python -m studio show nimbus-frontend-rza-1.0
    python -m studio run nimbus-backend-rza-1.0 "..." --thinking xhigh
    python -m studio ask "..." --engine anthropic --thinking max
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .chips import ChipError, import_chip, remove_chip
from .contract import Delegation, DelegationResult, Plan
from .engines import EngineError
from .gears import ParsedRequest
from .orchestrator import Orchestrator
from .registry import Registry, RegistryError
from .spec import THINKING_LEVELS

DIVIDER = "─" * 72


# -- terminal output --------------------------------------------------------
#
# Progress goes to stderr and the answer to stdout, and that existing split is
# what makes colour safe to add: it is decided per stream. Piped into a file or
# read by CI, the output is byte-for-byte what it was before -- escape codes
# appear only when a person is watching.


def _enable_windows_ansi() -> None:
    """Turn on VT processing, which older Windows consoles start without.

    Without this the codes are printed literally, which is worse than having no
    colour at all. Failure is not an error: `_use_colour` still asks isatty, and
    a console that refuses the mode simply prints plain text.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # -12 is STD_ERROR_HANDLE; 0x4 is ENABLE_VIRTUAL_TERMINAL_PROCESSING.
        handle = kernel32.GetStdHandle(-12)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x4)
    except Exception:
        pass


def _use_colour(stream) -> bool:
    # NO_COLOR and FORCE_COLOR are honoured because every other tool a person
    # has in the same terminal honours them, and a rule that holds everywhere
    # except here is one they have to remember.
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return bool(getattr(stream, "isatty", None) and stream.isatty())


class Paint:
    """Colour, or nothing at all, decided once for one stream."""

    CODES = {"dim": "2", "bold": "1", "red": "31", "green": "32",
             "yellow": "33", "blue": "34", "cyan": "36"}

    def __init__(self, stream) -> None:
        self.on = _use_colour(stream)
        if self.on:
            _enable_windows_ansi()

    def __call__(self, text: str, *styles: str) -> str:
        codes = ";".join(self.CODES[s] for s in styles if s in self.CODES)
        return f"\033[{codes}m{text}\033[0m" if self.on and codes else text

# Engine providers the CLI accepts. `echo` is offline, `ollama` is local and free;
# the rest need credentials.
ENGINES = ["echo", "ollama", "openai", "deepseek", "anthropic"]


def load_env(path: Path | str = ".env") -> int:
    """Read a .env file into the environment.

    Kept in-house rather than taking python-dotenv: it is twenty lines, and the
    project's core has no dependencies. Real environment variables always win, so
    an exported key is never silently overridden by a stale file.
    """
    path = Path(path)
    if not path.is_file():
        return 0

    loaded = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def _default_engine() -> str:
    return os.environ.get("NIMBUS_ENGINE", "echo")


def _default_thinking() -> str | None:
    return os.environ.get("NIMBUS_THINKING") or None


def _add_call_flags(parser: argparse.ArgumentParser) -> None:
    """Flags shared by every command that actually calls a model."""
    parser.add_argument("--engine", default=_default_engine(), choices=ENGINES)
    parser.add_argument(
        "--thinking",
        default=_default_thinking(),
        choices=list(THINKING_LEVELS),
        help="override the thinking level (default: each model's own)",
    )
    parser.add_argument(
        "--no-search",
        action="store_true",
        help="disable deep search even for models that have the capability",
    )
    parser.add_argument(
        "--lang",
        default=os.environ.get("NIMBUS_LANG") or None,
        help="answer in this language code (default: match the person's language)",
    )
    parser.add_argument("--usage", action="store_true", help="print token usage to stderr")


# -- commands ---------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    registry = Registry.load(args.root)
    base = registry.base
    ladder = base.thinking

    print(f"base      {base.name} v{base.version}  ({len(base.blocks)} blocks)")
    print(f"manager   {registry.manager.id}")
    print(f"models    {len(registry.models)}")
    print(f"gears     {len(registry.gears)}")
    print(f"chips     {len(registry.chips)}")
    print(f"thinking  {' < '.join(ladder.levels)}   (base default: {ladder.default})")
    print()

    for spec in registry.models.values():
        adopted = [b for b in base.block_order if b in set(spec.adopt)]
        skipped = [b for b in base.block_order if b not in set(spec.adopt)]
        caps = spec.capabilities
        search = f"yes (max {caps.max_searches})" if caps.deep_search else "no"
        print(f"{spec.id}")
        print(f"  adopts     {', '.join(adopted) or '(none)'}")
        print(f"  skips      {', '.join(skipped) or '(none)'}")
        print(f"  thinking   {spec.thinking_default}")
        print(f"  deepsearch {search}")
        print(f"  engine     {spec.engine.provider}:{spec.engine.model_id or '-'} "
              f"max_tokens={spec.engine.max_tokens}")
        attached = registry.chips.for_model(spec.id)
        if attached:
            print(f"  chips      {', '.join(chip.id for chip in attached)}")

    if len(registry.gears):
        print()
        for gear in registry.gears:
            target = gear.handler if gear.is_router else "(shapes output, no fixed handler)"
            print(f"{gear.token:<14} {gear.kind:<9} -> {target}")

    print()
    print("configuration is valid.")
    return 0


def cmd_engines(args: argparse.Namespace) -> int:
    """List the engine providers and what each one supports.

    Engines coexist. Adding one never removes another — switching is a flag, and
    every model's `model.toml` keeps its own default provider.
    """
    from .engines.base import build_engine
    from .spec import EngineConfig

    rows = [
        ("echo", "—", "offline stub, no network, no cost", "no", "n/a", "free"),
        ("ollama", "—", "local models, no account needed", "no", "flat unless configured", "free"),
        ("openai", "OPENAI_API_KEY", "any OpenAI-compatible provider", "no", "flat unless configured", "varies"),
        ("deepseek", "DEEPSEEK_API_KEY", "DeepSeek chat-completions", "no", "2 models", "paid"),
        ("anthropic", "ANTHROPIC_API_KEY", "Claude Messages API", "yes", "native, 5 levels", "paid"),
    ]

    print(f"{'engine':<12}{'credential':<20}{'cost':<9}{'search':<9}{'thinking'}")
    print(DIVIDER)
    for name, credential, description, search, thinking, cost in rows:
        needs_key = credential != "—"
        ready = "ready" if not needs_key or os.environ.get(credential) else "no key"
        print(f"{name:<12}{credential:<20}{cost:<9}{search:<9}{thinking}")
        print(f"{'':<12}{description}  [{ready}]")
    print()

    if args.check:
        print("checking credentials by constructing each engine:")
        for name, *_ in rows:
            try:
                build_engine(EngineConfig(provider=name, model_id="", max_tokens=100))
                print(f"  {name:<12} ok")
            except EngineError as exc:
                print(f"  {name:<12} {exc}")
        print()

    print("Every model keeps its own provider in model.toml; --engine overrides it")
    print("for one run. Engines are additive — none was removed to add another.")
    return 0


def cmd_gears(args: argparse.Namespace) -> int:
    registry = Registry.load(args.root)

    if args.json:
        # Exactly what a graphical client needs to render gear chips and hover
        # cards without knowing anything else about Nimbus.
        payload = [gear.to_ui_dict() for gear in registry.gears]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    if not len(registry.gears):
        print("no gears installed.")
        return 0

    for gear in registry.gears:
        tokens = "  ".join(gear.tokens)
        print(f"{gear.name}  ({gear.id} v{gear.version})")
        print(f"  invoke   {tokens}")
        print(f"  kind     {gear.kind}")
        if gear.is_router:
            print(f"  handler  {gear.handler}")
        if gear.thinking:
            print(f"  thinking {gear.thinking}")
        print(f"  {gear.summary}")
        if gear.ui.example:
            print(f"  e.g.     {gear.ui.example}")
        print()
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from .evaluate import EvalError, Evaluator, compare, load_suites

    registry = Registry.load(args.root)
    eval_dir = Path(args.root).resolve() / "eval" if args.root else Path(__file__).resolve().parent.parent / "eval"
    suites = load_suites(eval_dir, args.suite or (), args.task or ())

    def run_arm(baseline: bool):
        evaluator = Evaluator(
            registry,
            engine=args.engine,
            thinking=args.thinking,
            baseline=baseline,
            delay=args.delay,
            repeat=args.repeat,
        )
        results = []
        for suite in suites:
            if not args.json:
                arm = "baseline" if baseline else "nimbus"
                print(f"\n{suite.name} [{arm}]  {len(suite.tasks)} tasks", file=sys.stderr)

            def on_task(outcome):
                if args.json:
                    return
                if outcome.skipped:
                    print(f"  skip  {outcome.task.id}  ({outcome.skip_reason})", file=sys.stderr)
                    return
                mark = "pass" if outcome.passed else "FAIL"
                tally = ""
                if outcome.runs_total > 1:
                    tally = f"  [{outcome.runs_passed}/{outcome.runs_total} runs]"
                    if outcome.flaky:
                        tally += "  FLAKY"
                print(f"  {mark}  {outcome.task.id}{tally}", file=sys.stderr)
                if not outcome.passed and args.verbose:
                    if outcome.error:
                        print(f"        error: {outcome.error}", file=sys.stderr)
                    for check in outcome.failures:
                        detail = f" ({check.detail})" if check.detail else ""
                        print(f"        - {check.name}{detail}", file=sys.stderr)

            results.append(evaluator.run_suite(suite, on_task=on_task))
        return results

    nimbus = run_arm(baseline=False)
    baseline = run_arm(baseline=True) if args.compare else []

    if args.json:
        payload = {
            "engine": args.engine,
            "suites": [
                {
                    "suite": r.suite.name,
                    "arm": r.arm,
                    "passed": r.passed,
                    "total": r.total,
                    "skipped": r.skipped,
                    "rate": r.rate,
                    "usage": r.usage,
                    "failures": [
                        {
                            "task": t.task.id,
                            "error": t.error,
                            "checks": [
                                {"name": c.name, "detail": c.detail} for c in t.failures
                            ],
                        }
                        for t in r.results
                        if not t.passed
                    ],
                }
                for r in nimbus + baseline
            ],
        }
        if args.compare:
            payload["comparison"] = compare(nimbus, baseline)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    def pct(value) -> str:
        return f"{value:.0%}" if value is not None else "n/a"

    print()
    print(DIVIDER)
    if args.compare:
        summary = compare(nimbus, baseline)
        print(f"{'suite':<16}{'nimbus':>10}{'baseline':>10}{'delta':>10}")
        for row in summary["suites"]:
            delta = f"{row['delta']:+.0%}" if row["delta"] is not None else "n/a"
            print(f"{row['suite']:<16}{pct(row['nimbus']):>10}{pct(row['baseline']):>10}{delta:>10}")

        overall = summary["overall"]
        print(DIVIDER)
        if overall["nimbus"] is None:
            print("overall         no task was comparable across both arms")
        else:
            delta = overall["nimbus"] - overall["baseline"]
            print(f"{'overall':<16}{pct(overall['nimbus']):>10}{pct(overall['baseline']):>10}{delta:>+10.0%}")
            print(f"\nCompared over {overall['comparable_tasks']} tasks both arms ran.")
            if overall["not_comparable"]:
                print(f"{overall['not_comparable']} task(s) excluded: the baseline cannot attempt them.")
            print("A delta at or below zero means the Nimbus layer is not earning its cost.")
    else:
        total = sum(r.total for r in nimbus)
        passed = sum(r.passed for r in nimbus)
        skipped = sum(r.skipped for r in nimbus)
        for r in nimbus:
            print(f"{r.suite.name:<16}{r.passed:>3}/{r.total:<3}  {pct(r.rate):>5}")
        print(DIVIDER)
        print(f"{'overall':<16}{passed:>3}/{total:<3}  {pct(passed / total if total else None):>5}")
        if skipped:
            print(f"{skipped} task(s) skipped.")

    print()
    print("Checks are mechanical: routing, structure, string and regex assertions.")
    print("Prose quality is NOT measured here and still needs a human read.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the HTTP API."""
    try:
        import uvicorn
    except ImportError:
        print(
            'the API extra is not installed. Run: pip install "fastapi" "uvicorn"',
            file=sys.stderr,
        )
        return 1

    from .api import create_app

    # Load once here so a broken configuration fails before the port is bound.
    Registry.load(args.root)

    app = create_app(args.root, engine=args.engine)
    print(f"serving on http://{args.host}:{args.port}  (engine: {args.engine})")
    print(f"  docs   http://{args.host}:{args.port}/docs")
    print(f"  legal  http://{args.host}:{args.port}/legal/")
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def cmd_languages(args: argparse.Namespace) -> int:
    registry = Registry.load(args.root)
    policy = registry.base.language

    if args.json:
        print(json.dumps(policy.to_ui_dict(), indent=2, ensure_ascii=False))
        return 0

    print(f"pivot     {policy.pivot.name} ({policy.pivot_code})")
    print(f"native    {len(policy.native_codes)} languages, written directly")
    print(f"bridged   anything else, routed through the pivot — no training needed")
    print()

    for language in policy:
        route = "native " if language.native else "bridged"
        rtl = "  rtl" if language.is_rtl else ""
        print(f"  {language.code:<4} {route}  {language.name:<12} {language.endonym}{rtl}")

    print()
    print("A language absent from this list is still bridged; it just has no display metadata.")
    return 0


def cmd_chips(args: argparse.Namespace) -> int:
    registry = Registry.load(args.root)

    if args.json:
        payload = [chip.to_ui_dict() for chip in registry.chips]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    if not len(registry.chips):
        print("no chips installed.")
        print(f"install one with: python -m studio chips import <file.zip>")
        return 0

    for chip in registry.chips:
        state = "enabled" if chip.enabled else "disabled"
        targets = "all models" if chip.targets_all else ", ".join(chip.models)
        print(f"{chip.name}  ({chip.id} v{chip.version})  [{state}]")
        print(f"  applies to  {targets}")
        print(f"  files       {', '.join(chip.files)}")
        print(f"  licence     {chip.license.holder}  ({chip.license.file})")
        print(f"  content     {len(chip.content)} characters")
        print(f"  {chip.summary}")
        print()
    return 0


def cmd_chips_import(args: argparse.Namespace) -> int:
    registry = Registry.load(args.root)
    chip = import_chip(args.archive, registry.skills_dir, overwrite=args.overwrite)
    targets = "all models" if chip.targets_all else ", ".join(chip.models)
    print(f"installed {chip.name} ({chip.id} v{chip.version})")
    print(f"  applies to  {targets}")
    print(f"  licensed to {chip.license.holder}")
    print(f"  content     {len(chip.content)} characters from {len(chip.files)} file(s)")
    return 0


def cmd_chips_remove(args: argparse.Namespace) -> int:
    registry = Registry.load(args.root)
    remove_chip(args.chip, registry.skills_dir)
    print(f"removed {args.chip}")
    return 0


def cmd_parse(args: argparse.Namespace) -> int:
    """Show what gears a prompt resolves to, without calling any model."""
    registry = Registry.load(args.root)
    parsed = registry.gears.parse(args.prompt)

    if args.json:
        print(json.dumps(_parsed_to_dict(parsed), indent=2, ensure_ascii=False))
        return 0

    if not parsed.invocations and not parsed.unknown:
        print("no gears found.")
        return 0

    for invocation in parsed.invocations:
        gear = invocation.gear
        target = gear.handler if gear.is_router else "applies to every brief"
        print(f"{invocation.token}  [{invocation.start}:{invocation.end}]  "
              f"{gear.kind} -> {target}")
    for token in parsed.unknown:
        print(f"{token}  unknown gear — left in the prompt as written")

    print(f"\ncleaned prompt: {parsed.text!r}")
    return 0


def _parsed_to_dict(parsed: ParsedRequest) -> dict:
    return {
        "raw": parsed.raw,
        "text": parsed.text,
        "unknown": list(parsed.unknown),
        "invocations": [
            {
                "token": item.token,
                "start": item.start,
                "end": item.end,
                "gear": item.gear.to_ui_dict(),
            }
            for item in parsed.invocations
        ],
    }


def cmd_list(args: argparse.Namespace) -> int:
    registry = Registry.load(args.root)
    for spec in registry.models.values():
        marker = "manager  " if spec.is_manager else "specialist"
        search = "deep-search" if spec.capabilities.deep_search else "no-search"
        print(f"{marker}  {spec.id:<28} {spec.domain}")
        print(f"{'':<12}  thinking={spec.thinking_default}  {search}")
        print(f"{'':<12}  {spec.summary}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    registry = Registry.load(args.root)
    orchestrator = Orchestrator(registry, engine_override="echo")
    model = orchestrator.model(args.model)
    caps = model.spec.capabilities

    print(DIVIDER)
    print(f"{model.spec.name}  ({model.id})")
    print(f"adopted blocks: {', '.join(model.spec.adopt)}")
    print(f"thinking:       {model.spec.thinking_default}")
    print(f"deep search:    {'yes' if caps.deep_search else 'no'}")
    print(f"chips:          {', '.join(c.id for c in model.chips) or 'none'}")
    print(f"system prompt:  {len(model.system)} characters")
    print(DIVIDER)
    print(model.system)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    registry = Registry.load(args.root)
    orchestrator = Orchestrator(registry, engine_override=args.engine)
    model = orchestrator.model(args.model)

    level = model.resolve_thinking(args.thinking)
    result = model.ask(args.prompt, thinking=level, search=not args.no_search)
    print(result.text)
    if args.usage:
        print(
            f"\n[{result.model}] thinking={level} searches={result.searches} {result.usage}",
            file=sys.stderr,
        )
    return 0


def cmd_client(args: argparse.Namespace) -> int:
    from .client import main as client_main

    return client_main()


def cmd_ask(args: argparse.Namespace) -> int:
    registry = Registry.load(args.root)
    orchestrator = Orchestrator(
        registry,
        engine_override=args.engine,
        thinking=args.thinking,
        search=not args.no_search,
        language=args.lang,
    )

    paint = Paint(sys.stderr)
    # A delegation is tens of seconds of nothing. Without a number afterwards
    # the only thing the screen distinguishes is "still going" from "finished",
    # and which model is the slow one -- the question actually being asked of a
    # run like this -- has to be timed with a stopwatch.
    started: dict[str, float] = {}

    def took(key: str) -> str:
        begun = started.pop(key, None)
        return "" if begun is None else paint(f"  {time.monotonic() - begun:.1f}s", "dim")

    def line(tag: str, style: str, text: str, suffix: str = "") -> None:
        print(f"{paint(tag.ljust(6), style)}{text}{suffix}", file=sys.stderr)

    def emit(kind: str, detail: object) -> None:
        if args.quiet:
            return
        if kind == "gears:parsed" and isinstance(detail, ParsedRequest):
            for gear in detail.gears:
                target = gear.handler if gear.is_router else "output shape"
                line("gear", "cyan", f"{gear.token} → {target}")
            for token in detail.unknown:
                line("gear", "yellow", f"{token} is not installed — ignored")
        elif kind == "plan:start":
            started["plan"] = time.monotonic()
            line("plan", "blue", f"{detail} …")
        elif kind == "plan:done" and isinstance(detail, Plan):
            if detail.handle_directly:
                line("", "", paint(f"answering directly: {detail.rationale}", "dim"),
                     took("plan"))
            else:
                targets = ", ".join(
                    f"{d.model}({d.thinking or 'default'})" for d in detail.delegations
                )
                line("", "", paint(f"delegating to {targets}", "dim"), took("plan"))
        elif kind == "delegate:start" and isinstance(detail, Delegation):
            started[detail.model] = time.monotonic()
            via = f" via {detail.gear}" if detail.gear else ""
            line("work", "blue", f"{detail.model}{via} …")
        elif kind == "delegate:done" and isinstance(detail, DelegationResult):
            if detail.ok:
                status = f"{len(detail.output):,} chars, thinking={detail.thinking}"
                if detail.searches:
                    status += f", {detail.searches} searches"
                body = paint(status, "dim")
            else:
                body = paint(f"failed: {detail.error}", "red")
            line("", "", body, took(detail.model))
        elif kind == "synthesis:start":
            started["write"] = time.monotonic()
            line("write", "blue", f"{detail} …")

    whole = time.monotonic()
    run = orchestrator.run(args.prompt, on_event=emit)

    if not args.quiet:
        # The assembling step gets its number the same way every other step
        # does, rather than being folded silently into the total.
        elapsed = took("write")
        if elapsed:
            print(paint("      assembled", "dim") + elapsed, file=sys.stderr)
        print(paint(DIVIDER, "dim"), file=sys.stderr)
    print(run.answer)
    if not args.quiet:
        print(paint(f"{time.monotonic() - whole:.1f}s", "dim"), file=sys.stderr)
    if args.usage:
        gears = " ".join(run.gears) or "none"
        print(
            paint(f"\ntotal  gears={gears} searches={run.searches} {run.usage}", "dim"),
            file=sys.stderr,
        )
    return 0


# -- entry point ------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="studio",
        description="Test harness for the Nimbus model family.",
    )
    parser.add_argument("--root", default=None, help="project root (defaults to the repo root)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="validate configuration and show capabilities")
    doctor.set_defaults(func=cmd_doctor)

    listing = subparsers.add_parser("list", help="list the models in the family")
    listing.set_defaults(func=cmd_list)

    engines = subparsers.add_parser("engines", help="list engine providers and their capabilities")
    engines.add_argument("--check", action="store_true", help="test whether each one has credentials")
    engines.set_defaults(func=cmd_engines)

    gears = subparsers.add_parser("gears", help="list the installed gears (plugins)")
    gears.add_argument("--json", action="store_true", help="emit the client-facing UI payload")
    gears.set_defaults(func=cmd_gears)

    evaluate = subparsers.add_parser("eval", help="run the evaluation suites")
    evaluate.add_argument("--suite", action="append", help="run only this suite (repeatable)")
    evaluate.add_argument(
        "--task",
        action="append",
        help="run only this task id (repeatable); useful when quota is tight",
    )
    evaluate.add_argument("--engine", default=_default_engine(), choices=ENGINES)
    evaluate.add_argument("--thinking", default=None, choices=list(THINKING_LEVELS))
    evaluate.add_argument("--env", default=".env", help="env file to load credentials from")
    evaluate.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="seconds to pause between tasks; use on rate-limited free tiers",
    )
    evaluate.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="run each task N times; a task passes only if every run passes",
    )
    evaluate.add_argument(
        "--compare",
        action="store_true",
        help="also run the bare-model baseline and report the delta",
    )
    evaluate.add_argument("-v", "--verbose", action="store_true", help="show which checks failed")
    evaluate.add_argument("--json", action="store_true")
    evaluate.set_defaults(func=cmd_eval)

    serve = subparsers.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", default=os.environ.get("NIMBUS_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("NIMBUS_PORT", "8000")))
    serve.add_argument("--engine", default=_default_engine(), choices=ENGINES)
    serve.add_argument("--log-level", default="info", choices=["critical", "error", "warning", "info", "debug"])
    serve.set_defaults(func=cmd_serve)

    languages = subparsers.add_parser("languages", help="show the writing language policy")
    languages.add_argument("--json", action="store_true", help="emit the client-facing payload")
    languages.set_defaults(func=cmd_languages)

    chips = subparsers.add_parser("chips", help="manage skill chips")
    chips.add_argument("--json", action="store_true", help="emit the client-facing UI payload")
    chips.set_defaults(func=cmd_chips)
    chip_actions = chips.add_subparsers(dest="chip_command")

    chip_import = chip_actions.add_parser("import", help="install a chip from a .zip")
    chip_import.add_argument("archive")
    chip_import.add_argument("--overwrite", action="store_true", help="replace an installed chip")
    chip_import.set_defaults(func=cmd_chips_import)

    chip_remove = chip_actions.add_parser("remove", help="uninstall a chip by id")
    chip_remove.add_argument("chip")
    chip_remove.set_defaults(func=cmd_chips_remove)

    parse = subparsers.add_parser("parse", help="show which gears a prompt resolves to")
    parse.add_argument("prompt")
    parse.add_argument("--json", action="store_true")
    parse.set_defaults(func=cmd_parse)

    show = subparsers.add_parser("show", help="print a model's composed system prompt")
    show.add_argument("model")
    show.set_defaults(func=cmd_show)

    run = subparsers.add_parser("run", help="call one model directly, bypassing the manager")
    run.add_argument("model")
    run.add_argument("prompt")
    _add_call_flags(run)
    run.set_defaults(func=cmd_run)

    # The hosted client, which is a different thing from everything above it:
    # the rest of this file runs the family in this process and needs the
    # provider keys here. `client` needs one API key and a URL, spends against
    # a limit the server keeps, and is what somebody who is not developing the
    # models should be using.
    client = subparsers.add_parser(
        "client", help="interactive terminal client against the hosted API")
    client.set_defaults(func=cmd_client)

    ask = subparsers.add_parser("ask", help="send a request through the manager")
    ask.add_argument("prompt")
    _add_call_flags(ask)
    ask.add_argument("--quiet", action="store_true", help="suppress progress output")
    ask.set_defaults(func=cmd_ask)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows can start Python with a legacy console encoding (for example
    # cp1252), which cannot print the Persian prompts and Unicode dividers this
    # CLI intentionally supports. Keep command output UTF-8 regardless of the
    # active console code page.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")

    # Load credentials before the parser reads env-backed defaults.
    load_env(os.environ.get("NIMBUS_ENV_FILE", ".env"))
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (RegistryError, EngineError, ChipError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
