"""Conversations, one JSON file each, in the data folder.

Since 2.5.0 a message is more than text: it carries the reasoning Apex wrote,
every tool the model used and what came back, and how fast it went. A file per
conversation holds that as it is, is rewritten whole under a lock with a rename
over the old copy, and stays readable by the person it belongs to.

Conversations from 2.0 (store.py's tables) are brought over once, the first
time this runs, and left where they were.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

MAX_CHATS = 300
_ID = re.compile(r"^[a-f0-9]{8,40}$")


class Chats:
    def __init__(self, root: Path, legacy: Path | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        if legacy is not None and not (self.root / ".imported").exists():
            self._import(legacy)

    def _path(self, chat_id: str) -> Path:
        if not _ID.match(str(chat_id)):
            raise KeyError(f"no chat {chat_id!r}")
        return self.root / f"{chat_id}.json"

    def _save(self, chat: dict) -> None:
        path = self._path(chat["id"])
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(chat, ensure_ascii=False, indent=1), encoding="utf-8")
        temporary.replace(path)

    def get(self, chat_id: str) -> dict:
        with self._lock:
            try:
                return json.loads(self._path(chat_id).read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise KeyError(f"no chat {chat_id!r}") from error

    def create(self, title: str = "", kind: str = "chat") -> dict:
        chat = {"id": uuid.uuid4().hex, "title": title or "", "kind": kind, "created": time.time(),
                "updated": time.time(), "messages": []}
        with self._lock:
            self._save(chat)
            self._trim()
        return chat

    def listing(self) -> list[dict]:
        rows = []
        with self._lock:
            for path in self.root.glob("*.json"):
                try:
                    chat = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                last = next((m for m in reversed(chat.get("messages") or []) if m.get("role") == "assistant"), None)
                rows.append({"id": chat["id"], "title": chat.get("title") or "", "updated": chat.get("updated", 0),
                             "count": len(chat.get("messages") or []),
                             "model": (last or {}).get("model", ""), "pinned": bool(chat.get("pinned"))})
        rows.sort(key=lambda r: (r["pinned"], r["updated"]), reverse=True)
        return rows

    def append(self, chat_id: str, message: dict) -> dict:
        with self._lock:
            chat = self.get(chat_id)
            message.setdefault("id", uuid.uuid4().hex[:12])
            message.setdefault("created", time.time())
            chat["messages"].append(message)
            if not chat.get("title") and message.get("role") == "user":
                chat["title"] = title_for(message.get("content", ""))
            chat["updated"] = time.time()
            self._save(chat)
        return message

    def replace_last(self, chat_id: str, role: str) -> None:
        """Drop the trailing message of `role` -- what "answer again" replaces."""
        with self._lock:
            chat = self.get(chat_id)
            if chat["messages"] and chat["messages"][-1].get("role") == role:
                chat["messages"].pop()
                self._save(chat)

    def update(self, chat_id: str, **fields) -> dict:
        with self._lock:
            chat = self.get(chat_id)
            for key in ("title", "pinned"):
                if key in fields:
                    chat[key] = fields[key]
            self._save(chat)
            return chat

    def delete(self, chat_id: str) -> None:
        with self._lock:
            self._path(chat_id).unlink(missing_ok=True)

    def _trim(self) -> None:
        rows = self.listing()
        for row in [r for r in rows if not r["pinned"]][MAX_CHATS:]:
            self.delete(row["id"])

    def _import(self, legacy: Path) -> None:
        """2.0's chats.json and messages.json, brought over as they were."""
        try:
            old_chats = json.loads((legacy / "chats.json").read_text(encoding="utf-8"))
            old_messages = json.loads((legacy / "messages.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old_chats, old_messages = [], []
        for old in old_chats:
            new_id = uuid.uuid5(uuid.NAMESPACE_URL, "ahoos-chat:" + str(old.get("id"))).hex
            if (self.root / f"{new_id}.json").exists():
                continue
            messages = sorted((m for m in old_messages if m.get("chat_id") == old.get("id")),
                              key=lambda m: m.get("created_at") or "")
            chat = {"id": new_id, "title": old.get("title") or "", "kind": "chat",
                    "created": _stamp(old.get("created_at")), "updated": _stamp(old.get("updated_at")),
                    "messages": [{"id": uuid.uuid4().hex[:12], "role": m.get("role"),
                                  "content": m.get("content") or "", "created": _stamp(m.get("created_at"))}
                                 for m in messages if m.get("role") in ("user", "assistant")]}
            if chat["messages"]:
                self._save(chat)
        (self.root / ".imported").write_text("2.0 conversations were imported from ../studio\n", encoding="utf-8")


def title_for(text: str) -> str:
    line = re.sub(r"\s+", " ", str(text or "")).strip()
    return (line[:58] + "…") if len(line) > 60 else line or "New chat"


def _stamp(value) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return time.time()
