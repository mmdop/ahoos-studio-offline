"""What can be checked about a project without running it, after it is written.

A small model's code fails in a few ways again and again, and most of them are
visible without executing anything: a Python file that does not parse; a page
that loads `style.css` when the file is `styles.css`; a script that looks up
`#score` in a page that calls it `#scoreBoard`; a JSON file with a trailing
comma. Each check here is one of those, and each one says where and what, in a
sentence the model can act on -- the turn hands them back to it to fix.

Nothing here runs the project. JavaScript is parsed by `node --check` when Node
is installed and skipped when it is not; Python is compiled, not run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path


@dataclass
class Problem:
    path: str
    message: str
    line: int | None = None

    def text(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where}: {self.message}"


def _read(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:4096]:
        return None
    return data.decode("utf-8", "replace")


# -- Python ---------------------------------------------------------------------

def check_python(path: Path, name: str) -> list[Problem]:
    source = _read(path)
    if source is None:
        return []
    try:
        compile(source, name, "exec", dont_inherit=True)
    except SyntaxError as error:
        return [Problem(name, f"syntax error: {error.msg}", error.lineno)]
    except ValueError as error:          # a null byte, an encoding the compiler refuses
        return [Problem(name, str(error))]
    return []


# -- JSON -------------------------------------------------------------------------

def check_json(path: Path, name: str) -> list[Problem]:
    source = _read(path)
    if source is None or not source.strip():
        return []
    try:
        json.loads(source)
    except ValueError as error:
        line = getattr(error, "lineno", None)
        return [Problem(name, f"not valid JSON: {getattr(error, 'msg', error)}", line)]
    return []


# -- CSS ----------------------------------------------------------------------------

_CSS_NOISE = re.compile(r"/\*.*?\*/|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'", re.S)


def check_css(path: Path, name: str) -> list[Problem]:
    source = _read(path)
    if source is None:
        return []
    bare = _CSS_NOISE.sub("", source)
    opened, closed = bare.count("{"), bare.count("}")
    if opened != closed:
        return [Problem(name, f"{opened} '{{' but {closed} '}}' -- a rule is not closed, or the file was cut off")]
    return []


# -- JavaScript -------------------------------------------------------------------------

def _node() -> str | None:
    return shutil.which("node")


def check_js(path: Path, name: str, node: str | None) -> list[Problem]:
    if not node:
        return []
    source = _read(path)
    if source is None:
        return []
    target = path
    temporary = None
    # A file with import/export is a module; node --check would read a plain
    # .js as CommonJS and call the first `import` an error that it is not.
    if path.suffix == ".js" and re.search(r"(?m)^\s*(?:import\s|export\s)", source):
        temporary = Path(tempfile.mkdtemp()) / (path.stem + ".mjs")
        temporary.write_text(source, encoding="utf-8")
        target = temporary
    try:
        done = subprocess.run([node, "--check", str(target)], capture_output=True, text=True, timeout=20,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return []
    finally:
        if temporary is not None:
            shutil.rmtree(temporary.parent, ignore_errors=True)
    if done.returncode == 0:
        return []
    text = (done.stderr or done.stdout).replace(str(target), name)
    line = None
    match = re.search(re.escape(name) + r":(\d+)", text)
    if match:
        line = int(match.group(1))
    reason = next((l.strip() for l in text.splitlines() if "Error" in l), text.strip().splitlines()[-1:] or [""])
    return [Problem(name, f"does not parse: {reason if isinstance(reason, str) else reason[0]}", line)]


_JS_NOISE = re.compile(r"/\*.*?\*/|//[^\n]*|`(?:\\.|[^`\\])*`|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'", re.S)
_CONST = re.compile(r"(?m)\bconst\s+([A-Za-z_$][\w$]*)\s*=")


def reassigned_consts(script: str) -> list[str]:
    """Names declared `const` and then assigned again -- a TypeError the moment that line runs.

    A small model writes `const snake = [...]` at the top of a game and
    `snake = [...]` in its restart function; the game then dies on its first
    game over. `node --check` cannot see it: it is not a syntax error. Only a
    name declared once, and never with let or var, is judged -- two
    declarations may be two scopes, and that is past what a pattern can tell.
    """
    bare = _JS_NOISE.sub('""', script)
    names = _CONST.findall(bare)
    found = []
    for name in sorted(set(names)):
        if names.count(name) != 1 or re.search(r"\b(?:let|var)\s+" + re.escape(name) + r"\b", bare):
            continue
        assigned = re.findall(r"(?<![\w$.])" + re.escape(name) + r"\s*(?:[-+*/%]?=(?!=)|\+\+|--)", bare)
        if len(assigned) > 1:                  # the declaration is one of them
            found.append(name)
    return found


def check_js_logic(path: Path, name: str) -> list[Problem]:
    source = _read(path)
    if source is None:
        return []
    return [Problem(name, f"`{n}` is declared with const and assigned again later; that line throws a TypeError "
                          f"when it runs -- declare it with let") for n in reassigned_consts(source)[:4]]


# -- HTML ------------------------------------------------------------------------------

class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.classes: set[str] = set()
        self.canvases: list[tuple[str, bool]] = []   # (id, has a width and a height of its own)
        self.refs: list[tuple[str, str]] = []        # (what, address)
        self.scripts: list[str] = []
        self._in_script = False
        self._script: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.add(attrs["id"])
        for cls in (attrs.get("class") or "").split():
            self.classes.add(cls)
        if tag == "canvas":
            self.canvases.append((attrs.get("id") or "", "width" in attrs and "height" in attrs))
        if tag == "script":
            if attrs.get("src"):
                self.refs.append(("script", attrs["src"]))
            else:
                self._in_script = True
                self._script = []
        elif tag == "link" and attrs.get("href") and "stylesheet" in (attrs.get("rel") or ""):
            self.refs.append(("stylesheet", attrs["href"]))
        elif tag in ("img", "audio", "video", "source") and attrs.get("src"):
            self.refs.append((tag, attrs["src"]))

    def handle_endtag(self, tag):
        if tag == "script" and self._in_script:
            self.scripts.append("".join(self._script))
            self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self._script.append(data)


def _local(address: str) -> str | None:
    address = address.strip()
    if not address or address.startswith(("http:", "https:", "//", "data:", "#", "mailto:", "javascript:", "blob:")):
        return None
    return address.split("?", 1)[0].split("#", 1)[0]


_BY_ID = re.compile(r"""getElementById\(\s*['"]([\w\-:.]+)['"]\s*\)""")
_BY_SELECTOR = re.compile(r"""querySelector(?:All)?\(\s*['"]#([\w\-]+)['"]\s*\)""")


def _made_by_script(script: str, ident: str) -> bool:
    """The element is created by the script itself, so the page need not have it."""
    return bool(re.search(r"""(?:\.id\s*=\s*['"]%s['"]|id=["']?%s\b|id:\s*['"]%s['"])""" % ((re.escape(ident),) * 3),
                          script))


def check_html(path: Path, name: str, root: Path) -> list[Problem]:
    source = _read(path)
    if source is None:
        return []
    page = _Page()
    try:
        page.feed(source)
        page.close()
    except Exception:  # noqa: BLE001 - html.parser rarely raises; a page it cannot read is not checked
        return []
    problems: list[Problem] = []
    scripts = list(page.scripts)
    for what, address in page.refs:
        local = _local(address)
        if local is None:
            continue
        target = (path.parent / local).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            continue
        if not target.is_file():
            problems.append(Problem(name, f'loads {what} "{address}", which does not exist'))
        elif what == "script":
            text = _read(target)
            if text:
                scripts.append(text)
    lookups: dict[str, None] = {}
    for script in scripts:
        for match in list(_BY_ID.finditer(script)) + list(_BY_SELECTOR.finditer(script)):
            ident = match.group(1)
            if ident in page.ids or any(_made_by_script(s, ident) for s in scripts):
                continue
            lookups.setdefault(ident, None)
    for ident in list(lookups)[:8]:
        problems.append(Problem(name, f'the script looks up the element #{ident}, but the page has no '
                                      f'element with id="{ident}"'))
    # A canvas with no size of its own is 300 by 150 pixels, whatever the game
    # draws on it -- the bottom of a 300 by 300 board simply is not there.
    sized_by_script = any(re.search(r"\.(?:width|height)\s*=(?!=)", s) for s in scripts)
    for ident, sized in page.canvases:
        if not sized and not sized_by_script:
            label = f"#{ident}" if ident else "<canvas>"
            problems.append(Problem(name, f"the canvas {label} has no width and height, and no script sets them, "
                                          "so it is 300×150 pixels; give it the size the game draws on"))
    for script in page.scripts:
        for const in reassigned_consts(script)[:2]:
            problems.append(Problem(name, f"an inline script declares `{const}` with const and assigns it again; "
                                          "declare it with let"))
    return problems


# -- all of it ------------------------------------------------------------------------------

CHECKED = {".py", ".pyw", ".json", ".css", ".js", ".mjs", ".cjs", ".html", ".htm"}


def check(root: Path, paths: list[str]) -> list[Problem]:
    """Every problem found in these files (relative to `root`), most useful first."""
    node = _node()
    problems: list[Problem] = []
    seen: set[str] = set()
    for name in paths:
        name = name.replace("\\", "/")
        if name in seen:
            continue
        seen.add(name)
        path = root / name
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        try:
            if suffix in (".py", ".pyw"):
                problems += check_python(path, name)
            elif suffix == ".json":
                problems += check_json(path, name)
            elif suffix == ".css":
                problems += check_css(path, name)
            elif suffix in (".js", ".mjs", ".cjs"):
                syntax = check_js(path, name, node)
                problems += syntax or check_js_logic(path, name)
            elif suffix in (".html", ".htm"):
                problems += check_html(path, name, root)
        except Exception:  # noqa: BLE001 - a check that fails is a check that did not happen, not an error
            continue
    return problems[:20]


def runnable(root: Path, paths: list[str]) -> dict:
    """How the result can be tried: a page to open, or a script to run."""
    pages = [p for p in paths if p.lower().endswith((".html", ".htm"))]
    page = next((p for p in pages if Path(p).name.lower() == "index.html"), pages[0] if pages else "")
    scripts = [p for p in paths if p.lower().endswith(".py")]
    main = next((p for p in scripts if Path(p).name.lower() in ("main.py", "app.py", "game.py")),
                scripts[0] if len(scripts) == 1 else "")
    return {"page": page, "script": main, "node": bool(_node()), "python": bool(shutil.which("python")
                                                                                 or shutil.which("python3"))}


def env_tools() -> dict:
    """Which interpreters this computer has, for the model to know before it runs anything."""
    found = {}
    for name, probe in (("python", ["python", "--version"]), ("node", ["node", "--version"]),
                        ("npm", ["npm", "--version"]), ("git", ["git", "--version"])):
        path = shutil.which(probe[0]) or (shutil.which("python3") if name == "python" else None)
        if not path:
            continue
        found[name] = path
    if os.name == "nt" and "python" in found and "WindowsApps" in found["python"]:
        # The Microsoft Store's stand-in, which opens the Store instead of running anything.
        found.pop("python")
    return found
