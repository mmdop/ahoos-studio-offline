"""The registry root the offline studio loads.

The online family is six models, two gears and a manager that delegates. The
offline studio is one model that answers directly, so it gets a root of its own
rather than a filtered view of the online one: the registry validates what it
loads, and an image gear routing to a model that is not there fails validation
by design.

What goes in, and from where:

    base/      base.toml with its language policy and thinking ladder, and none
               of the prompt blocks: the offline model adopts none of them, and
               the online family's prompts have no business inside a public
               download
    skills/    the repository's own chips, so a built-in chip behaves identically
    gears/     CR-file only. CR-image routes to an image model this build lacks.
    models/    desktop/family/models: Nimbus 1.1 Prime-EE, alone

A packaged build carries this assembled already (see desktop/packaging/studio.spec).
A checkout assembles it into the data directory on first use, from the files in
the repository, so there is one definition and not two copies of it.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .paths import data_dir

REPO = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
OFFLINE_GEARS = ("CR-file.toml",)


def root() -> Path:
    bundled = Path(getattr(sys, "_MEIPASS", "")) / "family"
    if getattr(sys, "frozen", False) and bundled.is_dir():
        return bundled
    return assemble(data_dir() / "family")


def assemble(target: Path) -> Path:
    """Build the root from the repository. Rebuilt each start: it is small, and a
    stale copy of base/ would be a prompt nobody can find the source of."""
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    write_offline_base(REPO / "base" / "base.toml", target / "base")
    if (REPO / "skills").is_dir():
        shutil.copytree(REPO / "skills", target / "skills")
    (target / "gears").mkdir()
    for name in OFFLINE_GEARS:
        shutil.copy2(REPO / "gears" / name, target / "gears" / name)
    shutil.copytree(HERE / "family" / "models", target / "models")
    retarget_chips(target / "skills")
    return target


OFFLINE_MODEL = "nimbus-1.1-prime-ee"


def retarget_chips(skills: Path) -> None:
    """Point every installed chip at the one model that exists offline.

    rtl-web is attached online to the front-end specialist, which is not in this
    build, so the registry rejects it -- correctly, since a chip targeting an
    absent model would silently never apply. The behaviour it teaches is the one
    Prime-EE was trained on, so offline it belongs to Prime-EE.
    """
    if not skills.is_dir():
        return
    import re

    for manifest in skills.glob("*/chip.toml"):
        text = manifest.read_text(encoding="utf-8")
        new, count = re.subn(r'(?m)^(\s*models\s*=\s*)\[[^\]]*\]', rf'\1["{OFFLINE_MODEL}"]', text)
        if count != 1:
            raise RuntimeError(f"{manifest}: expected one `models = [...]` line, found {count}")
        manifest.write_text(new, encoding="utf-8")


def write_offline_base(source: Path, target: Path) -> None:
    """base.toml without its blocks.

    The registry needs the base for the language policy and the thinking ladder.
    It does not need the block texts -- identity, delegation, deep search -- for a
    model that adopts none of them, and copying base/ wholesale put every one of
    the online family's system prompts inside the packaged app, where anyone who
    unzips a release can read them.
    """
    import re

    text = source.read_text(encoding="utf-8")
    # Either the repository's multi-line list or an already-trimmed `[]`.
    text, orders = re.subn(r"(?ms)^block_order\s*=\s*\[(?:\s*\]|.*?^\])", "block_order = []", text)
    if orders != 1:
        raise RuntimeError(f"{source}: expected one block_order, found {orders}")
    # Every [blocks.x] table runs to the next table header or the end of the file.
    text = re.sub(r'(?ms)^\[blocks\.[^\]]+\]\n.*?(?=^\[(?!blocks\.)|\Z)', "", text)
    if "[blocks." in text:
        raise RuntimeError(f"{source}: a blocks table survived the trim")
    target.mkdir(parents=True, exist_ok=True)
    (target / "base.toml").write_text(text, encoding="utf-8")
