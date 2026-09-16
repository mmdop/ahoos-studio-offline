"""Load the base and the models from disk, and validate them.

Configuration is TOML so it can be read with the standard library's `tomllib`
(Python 3.11+) without any external dependency.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from .chips import ALL_MODELS, ChipSet, load_chips
from .gears import KINDS, ROUTER, TOKEN_PATTERN, Gear, GearSet, GearUI
from .languages import DIRECTIONS, LTR, Language, LanguagePolicy
from .spec import (
    THINKING_LEVELS,
    BaseSpec,
    Block,
    Capabilities,
    EngineConfig,
    ModelSpec,
    Thinking,
)

DEFAULT_ROOT = Path(__file__).resolve().parent.parent

# A model adopting this block must declare the matching capability, and vice versa.
DEEP_SEARCH_BLOCK = "deep-search"


class RegistryError(RuntimeError):
    """The on-disk configuration is invalid."""


def _read_toml(path: Path) -> dict:
    if not path.is_file():
        raise RegistryError(f"config file not found: {path}")
    with path.open("rb") as handle:
        try:
            return tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise RegistryError(f"invalid TOML in {path}: {exc}") from exc


def _read_text(path: Path) -> str:
    if not path.is_file():
        raise RegistryError(f"text file not found: {path}")
    return path.read_text(encoding="utf-8")


class Registry:
    """The loaded base plus every model in the family."""

    def __init__(
        self,
        base: BaseSpec,
        models: dict[str, ModelSpec],
        gears: GearSet,
        chips: ChipSet,
        root: Path,
    ) -> None:
        self.base = base
        self.models = models
        self.gears = gears
        self.chips = chips
        self.root = root
        self.skills_dir = root / "skills"

    # -- loading ----------------------------------------------------------

    @classmethod
    def load(cls, root: Path | str | None = None) -> "Registry":
        root = Path(root).resolve() if root else DEFAULT_ROOT
        base = cls._load_base(root / "base")
        models: dict[str, ModelSpec] = {}

        models_dir = root / "models"
        if not models_dir.is_dir():
            raise RegistryError(f"models directory not found: {models_dir}")

        for entry in sorted(models_dir.iterdir()):
            if entry.is_dir() and (entry / "model.toml").is_file():
                spec = cls._load_model(entry, base)
                if spec.id in models:
                    raise RegistryError(f"duplicate model id: {spec.id}")
                models[spec.id] = spec

        registry = cls(
            base,
            models,
            cls._load_gears(root / "gears"),
            load_chips(root / "skills"),
            root,
        )
        registry.validate()
        return registry

    @staticmethod
    def _load_gears(gears_dir: Path) -> GearSet:
        """Load every gear. An absent directory simply means no plugins."""
        gears: dict[str, Gear] = {}
        if not gears_dir.is_dir():
            return GearSet(gears)

        for entry in sorted(gears_dir.glob("*.toml")):
            data = _read_toml(entry)
            meta = data.get("gear", {})
            invocation = data.get("invocation", {})
            behaviour = data.get("behaviour", {})
            ui = data.get("ui", {})

            try:
                gear_id = meta["id"]
                token = invocation["token"]
            except KeyError as exc:
                raise RegistryError(f"missing gear.id or invocation.token in {entry}") from exc

            if gear_id in gears:
                raise RegistryError(f"duplicate gear id: {gear_id}")

            gears[gear_id] = Gear(
                id=gear_id,
                name=meta.get("name", gear_id),
                version=str(meta.get("version", "0")),
                kind=meta.get("kind", ROUTER),
                summary=meta.get("summary", ""),
                handler=meta.get("handler"),
                token=token,
                aliases=tuple(invocation.get("aliases", [])),
                instruction=behaviour.get("instruction", "").strip(),
                thinking=behaviour.get("thinking"),
                search=behaviour.get("search"),
                ui=GearUI(
                    label=ui.get("label", ""),
                    icon=ui.get("icon", ""),
                    accent=ui.get("accent", ""),
                    hover=ui.get("hover", ""),
                    example=ui.get("example", ""),
                ),
                path=entry,
            )

        return GearSet(gears)

    @staticmethod
    def _load_base(base_dir: Path) -> BaseSpec:
        data = _read_toml(base_dir / "base.toml")
        meta = data.get("base", {})
        raw_blocks = data.get("blocks", {})
        raw_thinking = data.get("thinking", {})

        # Required, not defaulted. A bare key in TOML attaches to the most recent
        # [table], so a block_order written below one is silently reparented — and
        # a permissive default would hide that by falling back to something that
        # usually looks right.
        if "block_order" not in data:
            raise RegistryError(
                f"{base_dir / 'base.toml'} has no top-level block_order. If it is "
                "present in the file, move it above the first [table] header."
            )

        blocks: dict[str, Block] = {}
        for block_id, entry in raw_blocks.items():
            blocks[block_id] = Block(
                id=block_id,
                summary=entry.get("summary", ""),
                text=_read_text(base_dir / entry["file"]),
                templated=bool(entry.get("templated", False)),
            )

        language = Registry._load_language(data.get("language", {}))

        thinking = Thinking(
            levels=tuple(raw_thinking.get("levels", THINKING_LEVELS)),
            default=raw_thinking.get("default", "high"),
            descriptions=dict(raw_thinking.get("descriptions", {})),
        )

        return BaseSpec(
            id=meta.get("id", "nimbus-base"),
            name=meta.get("name", "Nimbus Base"),
            version=str(meta.get("version", "0")),
            family=meta.get("family", "Nimbus"),
            vendor=meta.get("vendor", ""),
            summary=meta.get("summary", ""),
            block_order=tuple(data["block_order"]),
            blocks=blocks,
            thinking=thinking,
            language=language,
            upstream=meta.get("upstream", {}),
            path=base_dir,
        )

    @staticmethod
    def _load_language(raw: dict) -> LanguagePolicy:
        native = tuple(code.lower() for code in raw.get("native", ["en"]))
        pivot = str(raw.get("pivot", "en")).lower()

        catalog: dict[str, Language] = {}
        for code, entry in raw.get("catalog", {}).items():
            code = code.lower()
            catalog[code] = Language(
                code=code,
                name=entry.get("name", code.upper()),
                endonym=entry.get("endonym", ""),
                direction=entry.get("direction", LTR),
                native=code in native,
            )

        return LanguagePolicy(catalog=catalog, native=native, pivot=pivot)

    @staticmethod
    def _load_model(model_dir: Path, base: BaseSpec) -> ModelSpec:
        data = _read_toml(model_dir / "model.toml")
        meta = data.get("model", {})
        engine = data.get("engine", {})
        thinking = data.get("thinking", {})
        capabilities = data.get("capabilities", {})

        try:
            model_id = meta["id"]
        except KeyError as exc:
            raise RegistryError(f"missing model.id in {model_dir}") from exc

        return ModelSpec(
            id=model_id,
            name=meta.get("name", model_id),
            version=str(meta.get("version", "0")),
            role=meta.get("role", "specialist"),
            domain=meta.get("domain", ""),
            summary=meta.get("summary", ""),
            adopt=tuple(meta.get("adopt", [])),
            system_text=_read_text(model_dir / meta.get("system", "system.md")),
            engine=EngineConfig(
                provider=engine.get("provider", "echo"),
                model_id=engine.get("model_id", ""),
                max_tokens=int(engine.get("max_tokens", 8000)),
            ),
            thinking_default=thinking.get("default", base.thinking.default),
            capabilities=Capabilities(
                deep_search=bool(capabilities.get("deep_search", False)),
                max_searches=int(capabilities.get("max_searches", 5)),
            ),
            roster=tuple(data.get("manager", {}).get("roster", [])),
            card=data.get("card", {}),
            path=model_dir,
        )

    # -- validation -------------------------------------------------------

    def validate(self) -> None:
        """Catch configuration errors at load time, not mid-run."""
        problems: list[str] = []
        levels = self.base.thinking

        unknown_order = set(self.base.block_order) - set(self.base.blocks)
        if unknown_order:
            problems.append(f"block_order references undefined blocks: {sorted(unknown_order)}")

        missing_order = set(self.base.blocks) - set(self.base.block_order)
        if missing_order:
            problems.append(f"blocks missing from block_order will never render: {sorted(missing_order)}")

        if levels.default not in levels.levels:
            problems.append(
                f"base thinking default {levels.default!r} is not one of {list(levels.levels)}"
            )

        for spec in self.models.values():
            unknown_adopt = set(spec.adopt) - set(self.base.blocks)
            if unknown_adopt:
                problems.append(f"{spec.id} adopts undefined blocks: {sorted(unknown_adopt)}")

            if spec.thinking_default not in levels.levels:
                problems.append(
                    f"{spec.id} has thinking default {spec.thinking_default!r}, "
                    f"not one of {list(levels.levels)}"
                )

            # The capability and the prompt block must agree, or a model either
            # gets search instructions it cannot act on, or tools it was never
            # told how to use.
            adopts_search = DEEP_SEARCH_BLOCK in spec.adopt
            if adopts_search and not spec.capabilities.deep_search:
                problems.append(
                    f"{spec.id} adopts '{DEEP_SEARCH_BLOCK}' but capabilities.deep_search is false"
                )
            if spec.capabilities.deep_search and not adopts_search:
                problems.append(
                    f"{spec.id} declares deep_search but does not adopt '{DEEP_SEARCH_BLOCK}'"
                )

            for member in spec.roster:
                if member not in self.models:
                    problems.append(f"roster of {spec.id} references unknown model: {member}")

        managers = [s.id for s in self.models.values() if s.is_manager]
        if len(managers) != 1:
            problems.append(f"expected exactly one manager model, found {len(managers)}: {managers}")

        problems.extend(self._gear_problems())
        problems.extend(self._chip_problems())
        problems.extend(self._language_problems())

        if problems:
            raise RegistryError("invalid configuration:\n  - " + "\n  - ".join(problems))

    def _gear_problems(self) -> list[str]:
        """A broken gear must fail at load time — it is user-facing prompt syntax."""
        problems: list[str] = []
        levels = self.base.thinking.levels
        seen_tokens: dict[str, str] = {}

        for gear in self.gears:
            if gear.kind not in KINDS:
                problems.append(f"gear {gear.id} has unknown kind {gear.kind!r}, expected one of {list(KINDS)}")

            if gear.is_router:
                if not gear.handler:
                    problems.append(f"router gear {gear.id} declares no handler")
                elif gear.handler not in self.models:
                    problems.append(f"gear {gear.id} routes to unknown model: {gear.handler}")
            elif gear.handler:
                problems.append(
                    f"modifier gear {gear.id} declares handler {gear.handler!r}; "
                    "modifiers shape output and must not pin routing"
                )

            if not gear.instruction:
                problems.append(f"gear {gear.id} has an empty behaviour.instruction")

            if gear.thinking is not None and gear.thinking not in levels:
                problems.append(
                    f"gear {gear.id} sets thinking {gear.thinking!r}, not one of {list(levels)}"
                )

            for token in gear.tokens:
                if not TOKEN_PATTERN.fullmatch(token):
                    problems.append(
                        f"gear {gear.id} has malformed token {token!r}; expected @CR-<name>"
                    )
                owner = seen_tokens.get(token.lower())
                if owner and owner != gear.id:
                    problems.append(f"token {token} is claimed by both {owner} and {gear.id}")
                seen_tokens[token.lower()] = gear.id

        return problems

    def _language_problems(self) -> list[str]:
        """A bad language policy silently degrades every answer, so it fails loudly."""
        problems: list[str] = []
        policy = self.base.language

        if not policy.native_codes:
            problems.append("language.native is empty — at least one native language is required")

        # The pivot is what every bridged language routes through and what every
        # specialist brief is written in. It has to be one we write natively.
        if policy.pivot_code not in policy.native_codes:
            problems.append(
                f"language.pivot {policy.pivot_code!r} is not in language.native "
                f"{list(policy.native_codes)}; the pivot must be a native language"
            )

        for code in policy.native_codes:
            if code not in policy.catalog:
                problems.append(f"native language {code!r} has no entry in language.catalog")

        for language in policy.catalog.values():
            if language.direction not in DIRECTIONS:
                problems.append(
                    f"language {language.code} has direction {language.direction!r}, "
                    f"expected one of {list(DIRECTIONS)}"
                )

        # A templated block whose placeholders do not resolve would ship a literal
        # "{native_languages}" into a system prompt.
        values = policy.template_values()
        for block in self.base.blocks.values():
            if not block.templated:
                continue
            try:
                block.text.format(**values)
            except (KeyError, IndexError, ValueError) as exc:
                problems.append(f"templated block {block.id} has an unresolvable placeholder: {exc}")

        return problems

    def _chip_problems(self) -> list[str]:
        """Chips are third-party content; a chip aimed at nothing is a silent no-op."""
        problems: list[str] = []
        for chip in self.chips:
            for model_id in chip.models:
                if model_id != ALL_MODELS and model_id not in self.models:
                    problems.append(f"chip {chip.id} targets unknown model: {model_id}")
        return problems

    # -- access -----------------------------------------------------------

    @property
    def manager(self) -> ModelSpec:
        for spec in self.models.values():
            if spec.is_manager:
                return spec
        raise RegistryError("no manager model found")

    @property
    def specialists(self) -> list[ModelSpec]:
        return [s for s in self.models.values() if not s.is_manager]

    def get(self, model_id: str) -> ModelSpec:
        try:
            return self.models[model_id]
        except KeyError as exc:
            known = ", ".join(sorted(self.models)) or "(none)"
            raise RegistryError(f"unknown model: {model_id}. available: {known}") from exc
