"""Models and adapters the person brought in themselves, from the Hub.

The catalogue (catalogue.py) is ours: two models, their builds, their adapters,
checked into the code. This is everything else -- a GGUF picked from any
repository, an adapter pasted in as a link -- kept in `library.json` in the data
folder, with each file's path, size and what its own header says it is.

WHY THE HEADER IS READ

A file's name says what the uploader called it. Its GGUF header says what it
is: `general.architecture` decides which adapters can be applied to which base
(a Qwen2 adapter on a Qwen3 base does not load), and `general.type` tells an
adapter from a full model, which a filename often does not.
"""

from __future__ import annotations

import json
import re
import struct
import threading
import time
import uuid
from pathlib import Path

from .paths import data_dir, models_dir

_GGUF_TYPES = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}
FILE_TYPES = {0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1", 10: "Q2_K",
              11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L", 14: "Q4_K_S", 15: "Q4_K_M", 16: "Q5_K_S",
              17: "Q5_K_M", 18: "Q6_K", 19: "IQ2_XXS", 20: "IQ2_XS", 21: "Q2_K_S", 22: "IQ3_XS",
              23: "IQ3_XXS", 24: "IQ1_S", 25: "IQ4_NL", 26: "IQ3_S", 27: "IQ3_M", 28: "IQ2_S",
              29: "IQ2_M", 30: "IQ4_XS", 31: "IQ1_M", 32: "BF16", 36: "TQ1_0", 37: "TQ2_0", 38: "MXFP4"}
WANTED = ("general.architecture", "general.type", "general.name", "general.file_type",
          "adapter.type", "adapter.lora.alpha")


def gguf_header(path: Path) -> dict:
    """The few metadata keys that matter here, without reading the tensors.

    Stops at the first tokenizer key: the vocabulary is an array of a hundred
    and fifty thousand strings and nothing wanted comes after it.
    """
    found: dict = {}
    with Path(path).open("rb") as handle:
        if handle.read(4) != b"GGUF":
            raise ValueError(f"{Path(path).name} is not a GGUF file")
        (version,) = struct.unpack("<I", handle.read(4))
        if version < 2:
            raise ValueError("GGUF version 1 is too old to read")
        _tensors, count = struct.unpack("<QQ", handle.read(16))

        def string() -> str:
            (length,) = struct.unpack("<Q", handle.read(8))
            return handle.read(length).decode("utf-8", "replace")

        def value(kind: int):
            if kind == 8:
                return string()
            if kind == 9:
                inner, length = struct.unpack("<IQ", handle.read(12))
                if inner in _GGUF_TYPES:
                    size = struct.calcsize(_GGUF_TYPES[inner])
                    handle.seek(size * length, 1)
                else:
                    for _ in range(length):
                        value(inner)
                return None
            fmt = _GGUF_TYPES[kind]
            return struct.unpack("<" + fmt, handle.read(struct.calcsize(fmt)))[0]

        for _ in range(count):
            key = string()
            (kind,) = struct.unpack("<I", handle.read(4))
            if key.startswith("tokenizer."):
                break
            item = value(kind)
            if key in WANTED or key.endswith(".context_length"):
                found[key] = item
    arch = found.get("general.architecture", "")
    return {
        "architecture": arch,
        "type": found.get("general.type") or ("adapter" if found.get("adapter.type") else "model"),
        "name": found.get("general.name", ""),
        "quant": FILE_TYPES.get(found.get("general.file_type"), ""),
        "context": found.get(f"{arch}.context_length"),
        "alpha": found.get("adapter.lora.alpha"),
    }


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._")[:80] or "item"


class Library:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_dir() / "library.json"
        self._lock = threading.RLock()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return {"models": data.get("models") or [], "adapters": data.get("adapters") or []}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        temporary.replace(self.path)

    @staticmethod
    def model_folder(repo: str) -> Path:
        return models_dir() / "imported" / safe_name(repo.replace("/", "__"))

    @staticmethod
    def adapter_folder(repo: str) -> Path:
        return data_dir() / "adapters" / safe_name(repo.replace("/", "__"))

    def items(self, kind: str) -> list[dict]:
        """Entries whose file is still on disk; anything deleted by hand drops out."""
        with self._lock:
            return [i for i in self._read()[kind] if Path(i["file"]).is_file()]

    def get(self, kind: str, item_id: str) -> dict | None:
        return next((i for i in self.items(kind) if i["id"] == item_id), None)

    def add(self, kind: str, *, name: str, repo: str, file: Path, source: str = "", extra: dict | None = None) -> dict:
        header = {}
        try:
            header = gguf_header(file)
        except (OSError, ValueError, struct.error):
            pass
        entry = {"id": uuid.uuid4().hex[:12], "name": name, "repo": repo, "file": str(file),
                 "bytes": file.stat().st_size, "source": source, "added": time.time(),
                 "architecture": header.get("architecture", ""), "quant": header.get("quant", ""),
                 "context": header.get("context"), **(extra or {})}
        with self._lock:
            data = self._read()
            data[kind] = [i for i in data[kind] if i["file"] != str(file)] + [entry]
            self._write(data)
        return entry

    def remove(self, kind: str, item_id: str) -> dict | None:
        with self._lock:
            data = self._read()
            gone = next((i for i in data[kind] if i["id"] == item_id), None)
            data[kind] = [i for i in data[kind] if i["id"] != item_id]
            self._write(data)
        if gone:
            path = Path(gone["file"])
            for sibling in [path] + sorted(path.parent.glob(re.sub(r"-00001-of-", "-*-of-", path.name))):
                sibling.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass
        return gone
