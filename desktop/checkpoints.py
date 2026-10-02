"""Every file a turn changes can be put back the way it was.

A model that writes a whole project in one turn will sometimes write the wrong
one, or overwrite a file the person cared about. Asking before every write is
one answer to that, and it is the wrong one for a forty-step task: the person
stops reading and starts clicking. The other answer is to make every change
undoable, and then asking can be less frequent without being less safe.

Before a turn first touches a file, the file as it was -- or the fact that it
did not exist -- is copied aside, once. "Undo" puts every one of them back:
files the turn created are deleted, files it changed or deleted return. The
copies live in the studio's own data folder, never in the project.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from .paths import data_dir

KEEP = 60              # turns whose changes can still be undone


def root() -> Path:
    return data_dir() / "checkpoints"


class Checkpoint:
    """The files one turn changed, as they were before it."""

    def __init__(self, turn_id: str, folder: Path, made_folder: bool = False) -> None:
        self.id = turn_id
        self.folder = folder.resolve()
        self.made_folder = made_folder      # the turn created the project folder itself
        self.dir = root() / turn_id
        self.entries: dict[str, dict] = {}

    def before(self, path: Path) -> None:
        """Call before writing, editing or deleting `path`. Only the first call counts."""
        path = path.resolve()
        try:
            rel = path.relative_to(self.folder).as_posix()
        except ValueError:
            return
        if rel in self.entries:
            return
        entry: dict = {"path": rel, "existed": path.is_file()}
        if entry["existed"]:
            copy = self.dir / "files" / f"{len(self.entries):04d}"
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, copy)
            entry["copy"] = copy.name
        self.entries[rel] = entry
        self._save()

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "checkpoint.json").write_text(json.dumps(
            {"id": self.id, "folder": str(self.folder), "time": time.time(), "made_folder": self.made_folder,
             "entries": list(self.entries.values())}, ensure_ascii=False, indent=1), encoding="utf-8")

    @property
    def paths(self) -> list[str]:
        return list(self.entries)


def undo(turn_id: str) -> list[str]:
    """Put back everything one turn changed. Returns what was restored."""
    folder_dir = root() / turn_id
    record = folder_dir / "checkpoint.json"
    if not record.is_file():
        raise KeyError("there is nothing to undo for that turn")
    data = json.loads(record.read_text(encoding="utf-8"))
    project = Path(data["folder"])
    restored = []
    for entry in reversed(data["entries"]):
        target = (project / entry["path"]).resolve()
        try:
            target.relative_to(project.resolve())
        except ValueError:
            continue
        if entry.get("existed") and entry.get("copy"):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(folder_dir / "files" / entry["copy"], target)
        elif target.is_file():
            target.unlink()
            # Folders the turn created and left empty go too.
            parent = target.parent
            while parent != project and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        restored.append(entry["path"])
    # A project folder the turn made, now empty again, goes with it.
    if data.get("made_folder") and project.is_dir() and not any(project.iterdir()):
        project.rmdir()
    shutil.rmtree(folder_dir, ignore_errors=True)
    return restored


def prune(keep: int = KEEP) -> None:
    """Forget the oldest checkpoints beyond `keep`."""
    base = root()
    if not base.is_dir():
        return
    found = sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in found[keep:]:
        shutil.rmtree(old, ignore_errors=True)
