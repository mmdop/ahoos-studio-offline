"""Skill Chips — importable skill packages.

A chip is a `.zip` that teaches models something specific: a set of Markdown files
that emphasise how work in some domain should be done, plus a licence naming who owns
it. Importing a chip unpacks it into `skills/`, and from then on its content is
appended to the system prompt of every model it targets.

Layout inside the archive:

    chip.toml         manifest — identity, targets, licence, client UI metadata
    SKILL.md          the emphasis content (any number of .md files, any names)
    LICENSE.md        the licence, which must name its holder

Chips differ from base blocks in who owns them. Blocks are ours and define the
family; chips are installed afterwards, can come from someone else, and carry a
licence saying so.
"""

from __future__ import annotations

import shutil
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

MANIFEST_NAME = "chip.toml"
ALL_MODELS = "*"

# Header written above chip content when it is composed into a system prompt, so a
# model can tell installed guidance apart from the base it was built with.
CHIP_HEADER = "# Skill chip: {name}"


class ChipError(RuntimeError):
    """A chip is malformed, unsafe, or could not be installed."""


@dataclass(frozen=True)
class ChipUI:
    """Presentation metadata for graphical clients. Unused by this runtime."""

    label: str = ""
    icon: str = "chip"
    accent: str = ""
    hover: str = ""


@dataclass(frozen=True)
class ChipLicense:
    file: str
    holder: str
    text: str


@dataclass(frozen=True)
class Chip:
    id: str
    name: str
    version: str
    summary: str
    author: str
    models: tuple[str, ...]
    enabled: bool
    files: tuple[str, ...]
    content: str
    license: ChipLicense
    ui: ChipUI
    path: Path

    @classmethod
    def ad_hoc(cls, name: str, content: str, models: Sequence[str] = (ALL_MODELS,)) -> "Chip":
        """A chip supplied with one request rather than installed on disk.

        Installed chips are a trust boundary and are validated as one: the
        licence must exist and must name its declared holder, every listed file
        must be present, and no archive entry may escape the install directory.
        None of that applies here, because nothing is being installed -- the
        content lives for one request, is attributed to nobody, and reaches only
        the person who sent it.

        What it shares with an installed chip is placement: appended after the
        model's own prompt, so it refines a model that already knows its job
        rather than redefining it. That ordering is what keeps a chip from
        rewriting identity or safety, and it matters more here, where the text
        came from a user rather than from a package someone vouched for.
        """
        return cls(
            id=f"adhoc:{name}",
            name=name,
            version="",
            summary="",
            author="",
            models=tuple(models),
            enabled=True,
            files=(),
            content=content,
            license=ChipLicense(file="", holder="", text=""),
            ui=ChipUI(label=name),
            path=Path("."),
        )

    @property
    def targets_all(self) -> bool:
        return ALL_MODELS in self.models

    def applies_to(self, model_id: str) -> bool:
        return self.enabled and (self.targets_all or model_id in self.models)

    def to_ui_dict(self) -> dict[str, Any]:
        """Everything a graphical client needs to render this chip."""
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "summary": self.summary,
            "author": self.author,
            "models": list(self.models),
            "enabled": self.enabled,
            "files": list(self.files),
            "license": {"holder": self.license.holder, "file": self.license.file},
            "label": self.ui.label or self.name,
            "icon": self.ui.icon or "chip",
            "accent": self.ui.accent,
            "hover": self.ui.hover or self.summary,
        }


class ChipSet:
    """Every installed chip."""

    def __init__(self, chips: dict[str, Chip]) -> None:
        self.chips = chips

    def __len__(self) -> int:
        return len(self.chips)

    def __iter__(self) -> Iterable[Chip]:
        return iter(self.chips.values())

    def get(self, chip_id: str) -> Chip | None:
        return self.chips.get(chip_id)

    def for_model(self, model_id: str) -> tuple[Chip, ...]:
        """Chips that apply to a model, in stable id order."""
        return tuple(
            chip for chip in sorted(self.chips.values(), key=lambda c: c.id)
            if chip.applies_to(model_id)
        )


# -- reading ----------------------------------------------------------------


def _manifest_problems(data: dict[str, Any]) -> list[str]:
    """Validate a manifest's shape. Shared by loading and importing."""
    problems: list[str] = []
    chip = data.get("chip", {})
    apply = data.get("apply", {})
    licence = data.get("license", {})

    if not chip.get("id"):
        problems.append("chip.id is missing")
    if not chip.get("name"):
        problems.append("chip.name is missing")

    files = apply.get("files") or []
    if not files:
        problems.append("apply.files is empty — a chip with no content teaches nothing")

    models = apply.get("models") or []
    if not models:
        problems.append("apply.models is empty — say which models this chip targets, or use \"*\"")

    # The licence is not optional and must name someone. A chip is installable
    # third-party content; an unattributed one does not get to run.
    if not licence.get("file"):
        problems.append("license.file is missing")
    if not licence.get("holder"):
        problems.append("license.holder is missing — the licence must name its holder")

    return problems


def load_chip(chip_dir: Path) -> Chip:
    """Read an unpacked chip from disk."""
    manifest_path = chip_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ChipError(f"no {MANIFEST_NAME} in {chip_dir}")

    with manifest_path.open("rb") as handle:
        try:
            data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ChipError(f"invalid TOML in {manifest_path}: {exc}") from exc

    problems = _manifest_problems(data)
    if problems:
        raise ChipError(f"invalid chip at {chip_dir}:\n  - " + "\n  - ".join(problems))

    chip = data["chip"]
    apply = data["apply"]
    licence = data["license"]
    ui = data.get("ui", {})

    files = tuple(apply["files"])
    sections: list[str] = []
    for name in files:
        target = chip_dir / name
        if not target.is_file():
            raise ChipError(f"chip {chip['id']} lists {name!r} but the file is missing")
        sections.append(target.read_text(encoding="utf-8").strip())

    license_path = chip_dir / licence["file"]
    if not license_path.is_file():
        raise ChipError(f"chip {chip['id']} has no licence file at {licence['file']!r}")
    license_text = license_path.read_text(encoding="utf-8")

    holder = str(licence["holder"]).strip()
    if holder.lower() not in license_text.lower():
        raise ChipError(
            f"chip {chip['id']} declares holder {holder!r} but that name does not "
            f"appear in {licence['file']}"
        )

    return Chip(
        id=chip["id"],
        name=chip["name"],
        version=str(chip.get("version", "0")),
        summary=chip.get("summary", ""),
        author=chip.get("author", holder),
        models=tuple(apply["models"]),
        enabled=bool(apply.get("enabled", True)),
        files=files,
        content="\n\n".join(section for section in sections if section),
        license=ChipLicense(file=licence["file"], holder=holder, text=license_text),
        ui=ChipUI(
            label=ui.get("label", ""),
            icon=ui.get("icon", "chip"),
            accent=ui.get("accent", ""),
            hover=ui.get("hover", ""),
        ),
        path=chip_dir,
    )


def load_chips(skills_dir: Path) -> ChipSet:
    """Load every installed chip. An absent directory simply means none installed."""
    chips: dict[str, Chip] = {}
    if not skills_dir.is_dir():
        return ChipSet(chips)

    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir() or not (entry / MANIFEST_NAME).is_file():
            continue
        chip = load_chip(entry)
        if chip.id in chips:
            raise ChipError(f"duplicate chip id: {chip.id}")
        chips[chip.id] = chip

    return ChipSet(chips)


# -- importing --------------------------------------------------------------


def _safe_members(archive: zipfile.ZipFile, prefix: str) -> list[tuple[str, str]]:
    """Return (member_name, relative_path) pairs, rejecting anything unsafe.

    A zip can name entries like `../../etc/passwd` or an absolute path. Extracting
    those would write outside the destination, so they are refused outright rather
    than sanitised — a chip that contains one is not a chip we want installed.
    """
    pairs: list[tuple[str, str]] = []

    for info in archive.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        relative = name[len(prefix):] if prefix and name.startswith(prefix) else name

        if not relative:
            continue
        if relative.startswith("/") or ":" in relative.split("/")[0][1:2]:
            raise ChipError(f"refusing archive: absolute path in entry {info.filename!r}")
        parts = relative.split("/")
        if any(part in ("..", "") for part in parts):
            raise ChipError(f"refusing archive: path traversal in entry {info.filename!r}")

        pairs.append((info.filename, relative))

    return pairs


def _common_prefix(archive: zipfile.ZipFile) -> str:
    """Detect a single wrapping folder, since zipping a directory usually nests one."""
    names = [
        info.filename.replace("\\", "/")
        for info in archive.infolist()
        if not info.is_dir()
    ]
    if any(name == MANIFEST_NAME for name in names):
        return ""

    roots = {name.split("/")[0] for name in names if "/" in name}
    if len(roots) == 1:
        root = roots.pop()
        if f"{root}/{MANIFEST_NAME}" in names:
            return f"{root}/"

    raise ChipError(
        f"no {MANIFEST_NAME} found in the archive, at the root or in a single top folder"
    )


def import_chip(archive_path: Path, skills_dir: Path, *, overwrite: bool = False) -> Chip:
    """Validate a `.zip` and install it into `skills_dir`.

    Validation happens against a temporary extraction, so a bad chip never lands in
    the install directory even partially.
    """
    archive_path = Path(archive_path)
    if not archive_path.is_file():
        raise ChipError(f"archive not found: {archive_path}")

    skills_dir = Path(skills_dir)
    skills_dir.mkdir(parents=True, exist_ok=True)
    staging = skills_dir / f".importing-{archive_path.stem}"
    if staging.exists():
        shutil.rmtree(staging)

    try:
        with zipfile.ZipFile(archive_path) as archive:
            prefix = _common_prefix(archive)
            members = _safe_members(archive, prefix)

            for member, relative in members:
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)

        chip = load_chip(staging)   # raises ChipError if anything is wrong

        final = skills_dir / chip.id
        if final.exists():
            if not overwrite:
                raise ChipError(
                    f"chip {chip.id} is already installed. Pass --overwrite to replace it."
                )
            shutil.rmtree(final)

        staging.rename(final)
        return load_chip(final)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def remove_chip(chip_id: str, skills_dir: Path) -> None:
    target = Path(skills_dir) / chip_id
    if not target.is_dir():
        raise ChipError(f"chip not installed: {chip_id}")
    shutil.rmtree(target)
