"""Files as the model writes them best: as text in a code block, not inside JSON.

WHY 3.0 STOPPED PUTTING FILES INSIDE THE DECISION

In 2.5 a file was the `content` argument of a JSON tool call, written under a
grammar. Every newline became `\\n`, every quote `\\"`, and a seven-billion-
parameter model writing code that way writes very little of it: asked for a
snake game, it wrote a 49-character page that said hello. The same model asked
for the same file *as a code block* -- which is the habit it was trained on, the
one 2.5 had to fight to get tool calls at all -- writes the whole game.

So the decision says only which file (`write_file` with a path and a line about
it), and the file itself is a second, free generation that starts inside an
opened code fence and ends at the closing one. Nothing in it is escaped, and the
content streams as the person watches.

EDITS

A change to an existing file is a set of SEARCH/REPLACE blocks -- the format
code models are trained on -- applied exactly where the searched text is unique,
and with whitespace forgiven where it is not quite exact. A model may instead
rewrite a short file whole; both are understood.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Code-fence names, by extension. A fence with the right name is half of what
# makes the model write the right language.
FENCE = {
    "py": "python", "pyw": "python", "js": "javascript", "mjs": "javascript", "cjs": "javascript",
    "jsx": "jsx", "ts": "typescript", "tsx": "tsx", "html": "html", "htm": "html", "css": "css",
    "scss": "scss", "json": "json", "md": "markdown", "txt": "text", "sh": "bash", "bat": "bat",
    "ps1": "powershell", "c": "c", "h": "c", "cpp": "cpp", "hpp": "cpp", "cc": "cpp", "cs": "csharp",
    "java": "java", "kt": "kotlin", "go": "go", "rs": "rust", "rb": "ruby", "php": "php", "sql": "sql",
    "yml": "yaml", "yaml": "yaml", "toml": "toml", "xml": "xml", "svg": "xml", "vue": "vue",
    "swift": "swift", "dart": "dart", "lua": "lua", "r": "r", "ini": "ini", "cfg": "ini", "env": "bash",
}


def fence_for(path: str) -> str:
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name in ("dockerfile", "makefile"):
        return name
    return FENCE.get(name.rsplit(".", 1)[-1], "") if "." in name else ""


def opening(path: str) -> str:
    """The fence a file's generation starts inside.

    Markdown gets four backticks: a README holds code blocks of its own, and a
    three-backtick fence around it would end at the first one.
    """
    lang = fence_for(path)
    return ("````" if lang == "markdown" else "```") + lang + "\n"


def closing(path: str) -> str:
    return "\n````" if fence_for(path) == "markdown" else "\n```"


_FENCE_LINE = re.compile(r"^\s*(`{3,})[\w+#.-]*\s*$")


def clean_file(text: str, path: str = "") -> str:
    """The file inside what was generated: fences, a stray closing tag, chatter after.

    Generation starts inside an opened fence and stops at the closing one, so
    usually there is nothing to remove. But a model sometimes opens a second
    fence of its own, or ends with the fence and a sentence after it. In a
    Markdown file only a four-backtick line closes it: its own examples use three.
    """
    ticks = 4 if fence_for(path) == "markdown" else 3
    lines = text.replace("\r\n", "\n").split("\n")
    head = _FENCE_LINE.match(lines[0]) if lines else None
    if head and len(head.group(1)) >= ticks:
        lines = lines[1:]
    # A closing fence, and anything after it, is not part of the file.
    for index, line in enumerate(lines):
        bare = line.strip()
        fence = bare.startswith("`" * ticks) and set(bare) == {"`"}
        if fence or line.startswith("</tool_call>") or line.startswith("<|im_end|>"):
            lines = lines[:index]
            break
    body = "\n".join(lines).rstrip()
    return body + "\n" if body else ""


# A part of a file the model left for later. Said in a comment, these are the
# ways a small model stops short of the whole thing.
_PLACEHOLDER = re.compile(
    r"(?im)^\s*(?:#|//|/\*|<!--|\*)\s*(?:\.\.\.|…|"
    r"(?:the\s+)?rest\s+of\s+(?:the\s+)?(?:code|file|implementation)|"
    r"(?:add|implement|insert|put)\s+(?:your|the|more|other|remaining)\b[^\n]{0,40}\s+here|"
    r"existing\s+code|same\s+as\s+(?:before|above)|"
    r"todo\b|fixme\b)"
)


def placeholders(content: str) -> list[str]:
    """Lines that stand for code that was not written."""
    found = []
    for match in _PLACEHOLDER.finditer(content):
        line = content[match.start():content.find("\n", match.start()) if "\n" in content[match.start():] else None]
        found.append(line.strip()[:120])
    return found[:6]


# -- edits ------------------------------------------------------------------------

_BLOCK = re.compile(
    r"<{5,9}\s*SEARCH[^\n]*\n(.*?)\n?={5,9}[^\n]*\n(.*?)\n?>{5,9}\s*REPLACE",
    re.S,
)


@dataclass
class Edit:
    search: str
    replace: str


def parse_edits(text: str) -> list[Edit]:
    """SEARCH/REPLACE blocks, in the order written."""
    return [Edit(m.group(1), m.group(2)) for m in _BLOCK.finditer(text.replace("\r\n", "\n"))]


def _norm(line: str) -> str:
    return " ".join(line.split())


def _find(lines: list[str], wanted: list[str]) -> list[int]:
    """Where `wanted` appears in `lines`, comparing lines with spacing ignored."""
    if not wanted:
        return []
    target = [_norm(x) for x in wanted]
    first = target[0]
    hits = []
    for start in range(0, len(lines) - len(wanted) + 1):
        if _norm(lines[start]) != first:
            continue
        if all(_norm(lines[start + k]) == target[k] for k in range(1, len(wanted))):
            hits.append(start)
    return hits


def _reindent(replacement: list[str], found: list[str], wanted: list[str]) -> list[str]:
    """Shift the replacement by however much the model's indentation was off."""
    def indent(line: str) -> int:
        return len(line) - len(line.lstrip(" \t"))

    pairs = [(f, w) for f, w in zip(found, wanted) if f.strip()]
    if not pairs:
        return replacement
    shift = indent(pairs[0][0]) - indent(pairs[0][1])
    if shift == 0:
        return replacement
    out = []
    for line in replacement:
        if not line.strip():
            out.append(line)
        elif shift > 0:
            out.append(" " * shift + line)
        else:
            out.append(line[min(-shift, indent(line)):])
    return out


def apply_edits(original: str, edits: list[Edit]) -> tuple[str, list[str]]:
    """The file with every edit that could be placed; and why the others could not."""
    text = original.replace("\r\n", "\n")
    problems: list[str] = []
    for number, edit in enumerate(edits, 1):
        search = edit.search.replace("\r\n", "\n")
        if not search.strip():
            # An empty search is "add this": at the end of the file.
            text = text.rstrip("\n") + "\n" + edit.replace.rstrip("\n") + "\n"
            continue
        count = text.count(search)
        if count == 1:
            text = text.replace(search, edit.replace, 1)
            continue
        if count > 1:
            problems.append(f"block {number}: the searched text appears {count} times; include more lines around it")
            continue
        lines = text.split("\n")
        wanted = search.strip("\n").split("\n")
        hits = _find(lines, wanted)
        if len(hits) == 1:
            start = hits[0]
            found = lines[start:start + len(wanted)]
            replacement = _reindent(edit.replace.strip("\n").split("\n"), found, wanted) if edit.replace.strip() else []
            lines[start:start + len(wanted)] = replacement
            text = "\n".join(lines)
            continue
        problems.append(f"block {number}: the searched text is not in the file"
                        if not hits else f"block {number}: the searched text matches {len(hits)} places")
    if original.endswith("\n") and not text.endswith("\n"):
        text += "\n"
    return text, problems


def numbered(text: str, start: int = 1) -> str:
    return "\n".join(f"{n}: {line}" for n, line in enumerate(text.split("\n"), start))
