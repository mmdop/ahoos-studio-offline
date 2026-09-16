"""Conversations, messages, files and chips, kept on this computer.

The web studio keeps these in Supabase and talks to them through supabase-js.
The offline build runs the same app.js, unmodified, against a small stand-in for
that client (ui/local-supabase.js), and this is what the stand-in talks to.

WHY A STAND-IN AND NOT A REWRITE

app.js is nine hundred lines that already work, and every one of its database
calls has a comment explaining a bug it no longer has. Rewriting it for a local
store would be a second client to keep in step with the first, and the two would
drift from the first change onwards. Implementing the dozen query shapes it
actually uses -- select, insert, update, eq, order, limit, single -- lets the
same file run in both places.

WHY JSON FILES AND NOT SQLITE

The volumes are one person's chats. A table is a list in a file, rewritten
whole under a lock, with a temporary file renamed over it so a crash mid-write
leaves the previous version rather than half of one. The files are readable by
a person, which matters on a machine where this data is the person's own and
nowhere else.

WHAT IT DELIBERATELY DOES NOT DO

Anything app.js does not ask for. The query language below is exactly the part
of PostgREST the client uses, and a request for anything else fails loudly with
the name of what is missing -- a silent partial implementation of a query
language is how a filter gets ignored and every row comes back.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TABLES = ("chats", "messages", "files", "chips")

# The web studio keeps twenty chats. Offline there is no server to protect, so
# the limit is here only to keep the history list a list rather than a scroll.
MAX_CHATS = 200

LOCAL_USER = {
    "id": "00000000-0000-0000-0000-000000000001",
    "email": "",
    "user_metadata": {"name": ""},
}

_SAFE_PATH = re.compile(r"^[A-Za-z0-9._/-]+$")


class StoreError(ValueError):
    """A query this store does not implement, or a bad value in one."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.blobs = self.root / "files"
        self.blobs.mkdir(exist_ok=True)
        self._lock = threading.RLock()

    # -- tables -------------------------------------------------------------

    def _path(self, table: str) -> Path:
        if table not in TABLES:
            raise StoreError(f"no table named {table!r}")
        return self.root / f"{table}.json"

    def _read(self, table: str) -> list[dict[str, Any]]:
        path = self._path(table)
        if not path.is_file():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A damaged file is kept for whoever wants to recover it, and the
            # app carries on with an empty table rather than refusing to start.
            path.replace(path.with_suffix(f".damaged-{uuid.uuid4().hex[:6]}.json"))
            return []

    def _write(self, table: str, rows: list[dict[str, Any]]) -> None:
        path = self._path(table)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temporary, path)

    # -- the query ----------------------------------------------------------

    def query(self, table: str, spec: dict[str, Any]) -> dict[str, Any]:
        """Run one supabase-js style request. Returns {data, error}."""
        try:
            with self._lock:
                return {"data": self._run(table, spec), "error": None}
        except StoreError as exc:
            return {"data": None, "error": {"message": str(exc)}}

    def _run(self, table: str, spec: dict[str, Any]) -> Any:
        op = spec.get("op", "select")
        # Checked before any row is read. Checking inside the row loop meant an
        # unsupported filter on an empty table was never looked at -- it
        # "worked" until the day the table had something in it.
        self._check_filters(spec.get("filters") or [])
        rows = self._read(table)

        if op == "insert":
            values = spec.get("values")
            batch = values if isinstance(values, list) else [values]
            created = [self._defaults(table, dict(v or {})) for v in batch]
            rows.extend(created)
            if table == "chats" and len(rows) > MAX_CHATS:
                rows = self._trim_chats(rows)
            self._write(table, rows)
            if table == "chats":
                self._cascade_orphans()
            return self._shape(created, spec)

        matched = [r for r in rows if self._matches(r, spec.get("filters") or [])]

        if op == "select":
            return self._shape(matched, spec)

        if op == "update":
            changes = dict(spec.get("values") or {})
            for row in matched:
                row.update(changes)
            self._write(table, rows)
            return self._shape(matched, spec)

        if op == "delete":
            if not spec.get("filters"):
                # PostgREST refuses an unfiltered delete; so does this.
                raise StoreError("delete needs a filter")
            doomed = {id(r) for r in matched}
            self._write(table, [r for r in rows if id(r) not in doomed])
            if table == "chats":
                self._cascade_orphans()
            return self._shape(matched, spec)

        raise StoreError(f"unsupported operation {op!r}")

    @staticmethod
    def _check_filters(filters: list) -> None:
        for item in filters:
            if not isinstance(item, list) or len(item) != 3:
                raise StoreError(f"a filter is [column, operator, value], not {item!r}")
            if item[1] != "eq":
                raise StoreError(f"filter {item[1]!r} is not implemented")

    @staticmethod
    def _matches(row: dict[str, Any], filters: list) -> bool:
        return all(row.get(column) == value for column, _, value in filters)

    @staticmethod
    def _shape(rows: list[dict[str, Any]], spec: dict[str, Any]) -> Any:
        out = list(rows)
        for column, ascending in reversed(spec.get("order") or []):
            # Stable sorts applied last-key-first give PostgREST's multi-column
            # order. Nulls sort last ascending, as Postgres does by default.
            out.sort(key=lambda r: (r.get(column) is None, r.get(column)),
                     reverse=not ascending)
        if spec.get("limit"):
            out = out[: int(spec["limit"])]

        columns = spec.get("columns")
        if columns and columns != "*":
            keep = [c.strip() for c in columns.split(",") if c.strip()]
            out = [{c: r.get(c) for c in keep} for r in out]

        if spec.get("single"):
            if len(out) != 1:
                raise StoreError(f"expected one row, found {len(out)}")
            return out[0]
        return out

    def _defaults(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        stamp = now()
        row.setdefault("id", str(uuid.uuid4()))
        row.setdefault("created_at", stamp)
        if table == "chats":
            row.setdefault("updated_at", stamp)
            row.setdefault("title", "New chat")
        if table == "files":
            # The web studio deletes files after four hours to protect a shared
            # bucket. Here the files are on the person's own disk, and a clock
            # that deletes them would be a feature working against its owner.
            row.setdefault("expires_at", None)
        if table == "chips":
            row.setdefault("builtin", False)
            row.setdefault("visibility", "private")
        return row

    @staticmethod
    def _trim_chats(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
        return rows[:MAX_CHATS]

    def _cascade_orphans(self) -> None:
        """Messages whose chat is gone go with it, as the foreign key does online."""
        alive = {r["id"] for r in self._read("chats")}
        messages = self._read("messages")
        kept = [m for m in messages if m.get("chat_id") in alive]
        if len(kept) != len(messages):
            self._write("messages", kept)

    # -- blobs --------------------------------------------------------------

    def _blob(self, path: str) -> Path:
        # The path is chosen by app.js as `<user id>/<timestamp>-<name>`, where
        # the name came out of a model's answer. It is checked here rather than
        # trusted, because a name like ../../settings.json would otherwise write
        # wherever it pointed.
        if not path or not _SAFE_PATH.match(path) or ".." in path.split("/"):
            raise StoreError(f"refusing file path {path!r}")
        target = (self.blobs / path).resolve()
        if self.blobs.resolve() not in target.parents:
            raise StoreError(f"refusing file path {path!r}")
        return target

    def put(self, path: str, data: bytes) -> None:
        with self._lock:
            target = self._blob(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def get(self, path: str) -> bytes:
        target = self._blob(path)
        if not target.is_file():
            raise StoreError(f"no file at {path!r}")
        return target.read_bytes()

    def blob_path(self, path: str) -> Path:
        return self._blob(path)

    # -- chips that ship with the models ------------------------------------

    def seed_builtin_chips(self, chips: list[dict[str, Any]]) -> None:
        """Describe the chips installed beside the models, as the web library does.

        Content is deliberately absent: a built-in chip is attached by the
        runtime to the models its manifest names, not switched on from here.
        """
        with self._lock:
            rows = [r for r in self._read("chips") if not r.get("builtin")]
            for chip in chips:
                rows.append(self._defaults("chips", {
                    "id": f"builtin-{chip['id']}",
                    "slug": chip["id"],
                    "name": chip.get("name") or chip["id"],
                    "summary": chip.get("summary") or "",
                    "licence_holder": chip.get("licence_holder") or "",
                    "visibility": "public",
                    "builtin": True,
                    "content": None,
                    "owner_id": None,
                    "created_at": "1970-01-01T00:00:00+00:00",
                }))
            self._write("chips", rows)
