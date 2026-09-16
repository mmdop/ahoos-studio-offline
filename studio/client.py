"""The terminal client: the studio, without the browser.

    python -m studio client

WHY THIS IS NOT cli.py

cli.py builds an Orchestrator in this process, which means the provider keys
have to be on this machine. That is the right shape for developing the family
and the wrong shape for using it: it puts a credential on every laptop, and
nothing it spends can be counted against a limit, because the limit lives on the
server and this never speaks to the server.

This is a client. It holds one API key, issued from the platform page, and talks
to the hosted API over HTTP. Everything about who you are and what you may spend
is decided there -- which is the only way the terminal and the studio can share
a limit rather than each having their own.

WHAT IT CARRIES OVER FROM THE STUDIO

The same three controls, because someone who learns them in one should not have
to learn them again in the other: a model picker, the five-level effort ladder,
and the team/solo switch. Gears are typed inline exactly as they are in the
studio, @CR-image and the rest, because they are part of the message rather than
part of the client.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG = Path(os.environ.get("NIMBUS_HOME", Path.home() / ".nimbus")) / "config.json"
LEVELS = ("low", "medium", "high", "xhigh", "max")

# -- painting ---------------------------------------------------------------
#
# Colour only when a terminal is actually attached. Piped into a file or a pager
# these become escape sequences in the middle of the text, which is worse than
# plain output rather than prettier than it.


def _colour_ok() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        # Windows 10 draws ANSI only once it has been asked to.
        try:
            import ctypes

            handle = ctypes.windll.kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            return False
    return True


COLOUR = _colour_ok()


def _unicode_ok() -> bool:
    """Can this terminal actually print the box characters?

    A Windows console on a legacy code page cannot, and printing one raises
    UnicodeEncodeError rather than showing a substitute -- so the client would
    die on its own banner, on the machine most likely to be running it.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "─◆→✓✗›".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


FANCY = _unicode_ok()


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOUR else text


DIM = lambda s: paint(s, "2")
BOLD = lambda s: paint(s, "1")
BLUE = lambda s: paint(s, "38;5;33")
GREEN = lambda s: paint(s, "38;5;35")
RED = lambda s: paint(s, "38;5;167")
MARK = "◆" if FANCY else "*"
BAR = "─" if FANCY else "-"
ARROW = "→" if FANCY else "->"
TICK = "✓" if FANCY else "ok"
CROSS = "✗" if FANCY else "x"
PROMPT = "› " if FANCY else "> "


def width() -> int:
    try:
        return max(40, min(100, os.get_terminal_size().columns))
    except OSError:
        return 80


def rule(label: str = "") -> str:
    w = width()
    if not label:
        return DIM(BAR * w)
    return DIM(BAR * 3 + " " + label + " " + BAR * max(0, w - len(label) - 5))


# -- config -----------------------------------------------------------------


def load_config() -> dict:
    if CONFIG.is_file():
        try:
            return json.loads(CONFIG.read_text(encoding="utf-8"))
        except ValueError:
            print(RED(f"{CONFIG} is not readable as JSON. Delete it to start over."))
    return {}


def save_config(config: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(config, indent=2), encoding="utf-8")
    # The key is in here. 0600 where the platform has it; on Windows the
    # per-user profile directory is the protection and chmod is a no-op.
    try:
        CONFIG.chmod(0o600)
    except OSError:
        pass


# -- transport --------------------------------------------------------------


class Client:
    def __init__(self, url: str, key: str) -> None:
        self.url = url.rstrip("/")
        self.key = key

    def _request(self, path: str, payload: dict | None = None, timeout: int = 60):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            self.url + path,
            data=data,
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST" if data is not None else "GET",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace") or "null")

    def models(self) -> list[dict]:
        return self._request("/v1/models") or []

    def stream(self, payload: dict, timeout: int = 900):
        """Yield (event, data) from the server-sent stream.

        Framed by a blank line, which is the whole of the SSE spec that matters
        here. Written out rather than pulled in as a dependency: a client people
        install to try the product should need nothing but a Python.
        """
        request = urllib.request.Request(
            self.url + "/v1/ask/stream",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            event, lines = "message", []
            for raw in response:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    lines.append(line[5:].strip())
                elif not line:
                    if lines:
                        try:
                            yield event, json.loads("\n".join(lines))
                        except ValueError:
                            yield event, {"raw": "\n".join(lines)}
                    event, lines = "message", []


# -- the session ------------------------------------------------------------


class Session:
    def __init__(self, client: Client, config: dict) -> None:
        self.client = client
        self.config = config
        self.effort: str | None = config.get("effort") or None
        self.solo: bool = bool(config.get("solo"))
        self.model: str | None = config.get("model") or None
        self._models: list[dict] = []

    # -- settings that outlive the session
    def remember(self) -> None:
        self.config["effort"] = self.effort or ""
        self.config["solo"] = self.solo
        self.config["model"] = self.model or ""
        save_config(self.config)

    def roster(self) -> list[dict]:
        if not self._models:
            try:
                self._models = self.client.models()
            except Exception as exc:
                print(RED(f"could not read the model list: {exc}"))
                self._models = []
        return self._models

    # -- commands
    def pick_model(self, argument: str) -> None:
        models = self.roster()
        if not models:
            return
        if argument:
            wanted = argument.strip()
            if wanted in ("-", "auto", "team"):
                self.model = None
                print(DIM("the manager chooses"))
                self.remember()
                return
            match = [m for m in models if m["id"] == wanted or wanted in m["id"]]
            if not match:
                print(RED(f"no model matching {wanted!r}"))
                return
            self.model = match[0]["id"]
            print(GREEN(f"{MARK} {self.model}"))
            self.remember()
            return

        print()
        print(rule("models"))
        print(f"  {DIM('0')}  {BOLD('the manager chooses')}  "
              f"{DIM('— plan and delegate, the studio default')}")
        for n, m in enumerate(models, start=1):
            here = GREEN(MARK) if m["id"] == self.model else " "
            print(f"  {DIM(str(n))}{here} {m['id']}")
            if m.get("summary"):
                print(f"     {DIM(m['summary'][:70])}")
        print(rule())
        choice = input("  number, or blank to keep: ").strip()
        if not choice:
            return
        if choice == "0":
            self.model = None
        else:
            try:
                self.model = models[int(choice) - 1]["id"]
            except (ValueError, IndexError):
                print(RED("not one of those"))
                return
        self.remember()

    def set_effort(self, argument: str) -> None:
        value = argument.strip().lower()
        if value in ("", "?"):
            print("  " + "  ".join(
                (GREEN(MARK + level) if level == self.effort else DIM(level))
                for level in LEVELS))
            print(DIM("  /effort <level>, or /effort auto for each model's own"))
            return
        if value in ("auto", "-", "own"):
            self.effort = None
            print(DIM("each model's own"))
        elif value in LEVELS:
            self.effort = value
            print(GREEN(f"{MARK} {value}"))
        else:
            print(RED(f"levels are {', '.join(LEVELS)}"))
            return
        self.remember()

    def toggle_solo(self) -> None:
        self.solo = not self.solo
        print(GREEN(f"{MARK} solo — one model answers") if self.solo
              else DIM("team — the manager plans and delegates"))
        self.remember()

    def status(self) -> None:
        effort = self.effort or "each model's own"
        print(rule("session"))
        print(f"  {DIM('server '):10}{self.client.url}")
        print(f"  {DIM('model  '):10}{self.model or 'the manager chooses'}")
        print(f"  {DIM('effort '):10}{effort}")
        print(f"  {DIM('mode   '):10}{'solo' if self.solo else 'team'}")
        print(rule())

    # -- the turn
    def ask(self, prompt: str) -> None:
        payload = {"prompt": prompt, "solo": self.solo}
        if self.effort:
            payload["thinking"] = self.effort
        if self.model:
            # A named model is a routing instruction, which is what solo means.
            payload["solo"] = True

        started = time.monotonic()
        stages: dict[str, float] = {}
        answer = ""
        print()
        try:
            for event, data in self.client.stream(payload):
                if event == "plan:start":
                    print(DIM("  planning..."), end="\r", flush=True)
                elif event == "plan:done":
                    direct = data.get("handle_directly")
                    names = [d.get("model", "") for d in data.get("delegations", [])]
                    line = "answering directly" if direct else " + ".join(names)
                    print(f"  {BLUE(MARK)} {line}{' ' * 20}")
                elif event == "delegate:start":
                    name = data.get("model", "")
                    stages[name] = time.monotonic()
                    print(f"    {DIM(ARROW + ' ' + name + ' working...')}",
                          end="\r", flush=True)
                elif event == "delegate:done":
                    name = data.get("model", "")
                    took = time.monotonic() - stages.get(name, started)
                    bad = data.get("error")
                    mark = RED(CROSS) if bad else GREEN(TICK)
                    print(f"    {mark} {name} {DIM(f'{took:.0f}s')}"
                          f"{RED('  ' + str(bad)) if bad else ''}{' ' * 12}")
                elif event == "synthesis:start":
                    print(DIM("  bringing it together..."), end="\r", flush=True)
                elif event in ("answer:done", "synthesis:done", "done"):
                    text = data if isinstance(data, str) else data.get("answer", "")
                    if text:
                        answer = text
                elif event == "error":
                    print(RED(f"  {data.get('detail', data)}"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            if exc.code == 401:
                print(RED("  the server did not accept this key. /login to set another."))
            elif exc.code == 429:
                print(RED(f"  over the token limit. {body}"))
            else:
                print(RED(f"  HTTP {exc.code}: {body}"))
            return
        except Exception as exc:
            print(RED(f"  {type(exc).__name__}: {exc}"))
            return

        print(" " * width(), end="\r")
        if answer:
            print(answer)
        print(DIM(f"\n  {time.monotonic() - started:.0f}s"))


# -- entry ------------------------------------------------------------------

HELP = f"""
  {BOLD('/model')}            pick a model, or 0 to let the manager choose
  {BOLD('/effort')} <level>   {' < '.join(LEVELS)}, or auto
  {BOLD('/solo')}             one model, or the team
  {BOLD('/status')}           what this session is set to
  {BOLD('/login')}            set the server and the API key
  {BOLD('/help')}             this
  {BOLD('/quit')}             leave

  Gears work exactly as in the studio: type {BOLD('@CR-image')} in the message.
"""


def do_login(config: dict) -> dict:
    print(rule("sign in"))
    print(DIM("  Create a key at https://ahoos-ai.site/nimbus/platforme/"))
    url = input(f"  server [{config.get('url', 'https://ahoos-ai.site/nimbus')}]: ").strip()
    key = input("  key: ").strip()
    if url:
        config["url"] = url
    config.setdefault("url", "https://ahoos-ai.site/nimbus")
    if key:
        config["key"] = key
    save_config(config)
    print(GREEN(f"  {MARK} saved to {CONFIG}"))
    return config


def main(argv: list[str] | None = None) -> int:
    config = load_config()
    if os.environ.get("NIMBUS_API_URL"):
        config["url"] = os.environ["NIMBUS_API_URL"]
    if os.environ.get("NIMBUS_API_KEY"):
        config["key"] = os.environ["NIMBUS_API_KEY"]

    if not config.get("key") or not config.get("url"):
        config = do_login(config)
    if not config.get("key"):
        print(RED("no key, nothing to talk to."))
        return 1

    session = Session(Client(config["url"], config["key"]), config)

    print()
    print(f"  {BLUE(BOLD('AhoosAI'))} {DIM('- ahoos-ai.site')}")
    print(DIM("  /help for the commands, /quit to leave"))
    session.status()

    while True:
        try:
            line = input(BLUE(PROMPT)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue

        if line.startswith("/"):
            command, _, argument = line[1:].partition(" ")
            command = command.lower()
            if command in ("quit", "exit", "q"):
                return 0
            if command in ("help", "?"):
                print(HELP)
            elif command == "model":
                session.pick_model(argument)
            elif command == "effort":
                session.set_effort(argument)
            elif command == "solo":
                session.toggle_solo()
            elif command == "status":
                session.status()
            elif command == "login":
                config = do_login(config)
                session = Session(Client(config["url"], config["key"]), config)
            else:
                print(RED(f"  no /{command}. /help lists them."))
            continue

        session.ask(line)


if __name__ == "__main__":
    raise SystemExit(main())
