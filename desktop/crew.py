"""Agents: the offline studio's crew, the engine's spawn_agents done on this computer.

In agent mode a message is first split into tasks, one an agent -- each with a one-word English
name and a colour, as the online studio draws them -- and the agents work at the same time, each
on its one task: a search and a page or two when the internet is on, then a short report. Their
reports go to the model as notes, and it answers from them.

They run on the model the studio is working (a local llama-server, or an online model through
online.OnlineEngine): the four calls agent.Turn uses -- apply_template, stream with a JSON schema.
A server with one slot takes them in turn; the studio's own engine has more.

Events, the online studio's (projects/nimbus-2-apex-live/modal/engine.py): the crew is a
`spawn_agents` tool step (tool_start, tool_call, tool_result with meta.agents), and each agent's
progress an "agent" event -- id, name, color, hex, task, status (running, working with its step,
done with result and seconds, error).
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .engine import Cancelled, EngineError, Sampling

MAX_AGENTS = 4
ROUNDS = 3                                   # an agent's searches and pages, before its report
REPORT_TOKENS = 700
RESULT_CHARS = 2500
COLORS = [("red", "#E5484D"), ("orange", "#F76B15"), ("yellow", "#FFC53D"), ("green", "#30A46C"),
          ("blue", "#0090FF"), ("purple", "#8E4EC6")]
NAMES = ["Atlas", "Nova", "Echo", "Orion", "Lyra", "Vega"]
COOL = Sampling(temperature=0.2, top_p=0.9, top_k=40, min_p=0.05, repeat_penalty=1.0)
WARM = Sampling(temperature=0.5, top_p=0.9, top_k=40, min_p=0.05, repeat_penalty=1.05)

SPLIT = ("You lead a crew of agents. Split the user's request into the separate tasks it needs -- one task an "
         "agent, 1 to {n} agents -- each complete in itself: what to find or do, and what to report. A request "
         "that is one piece gets one agent. Give each agent its own name, as a person has one: one English word "
         "(Atlas, Nova, Echo, Orion) -- not a label like AgentA. Write each task as a question to find out: never "
         "put a guess of the answer in it (no versions, numbers or dates you expect), and no task that only checks "
         "the others.")
RULES = ("You are one of the studio's agents. You are given one task: do it, and finish. Nothing else.\n"
         "- {tools}\n"
         "- Then reply with your report: the findings themselves, short and concrete -- numbers, names, dates, "
         "code -- with the addresses of the pages they came from. No greeting, no advice.\n"
         "- If you could not find something, say so. Never give a number you did not read on a page.\n"
         "- Write in the language of the task.")
WITH_WEB = ("Search the web when the task needs facts that may be recent or that you are not sure of; read a page "
            "when a snippet is not enough. When the results are about something else, search again with other words.")
WITHOUT_WEB = "There is no internet here: work from what you know, and say what you could not check."
NOTES = ("# Your agents' reports\n\nAgents worked on parts of this request at the same time; their reports follow. "
         "Answer the user from them -- check them against each other, and say who found what where it helps. When a "
         "report does not have what was asked, say that it was not found; do not fill it in from memory, above all "
         "numbers, prices and dates.\n\n{reports}")

DECIDE_SCHEMA = {"anyOf": [
    {"type": "object", "properties": {"name": {"const": "web_search"}, "arguments": {"type": "object", "properties": {
        "query": {"type": "string"}}, "required": ["query"]}}, "required": ["name", "arguments"]},
    {"type": "object", "properties": {"name": {"const": "fetch_url"}, "arguments": {"type": "object", "properties": {
        "url": {"type": "string"}}, "required": ["url"]}}, "required": ["name", "arguments"]},
    {"type": "object", "properties": {"name": {"const": "reply"}}, "required": ["name"]}]}
# An agent's first step with the internet on: a search or a page. Left to choose, an agent answered from memory --
# a Python release two years old.
FIRST_SCHEMA = {"anyOf": DECIDE_SCHEMA["anyOf"][:2]}
# "name": an online model's decisions (online.OnlineEngine) are read as JSON with a name, and asked again without one.
SPLIT_SCHEMA = {"type": "object", "properties": {"name": {"const": "split"}, "agents": {"type": "array", "minItems": 1,
    "maxItems": MAX_AGENTS, "items": {"type": "object", "properties": {"name": {"type": "string"}, "task": {"type": "string"}},
                                      "required": ["name", "task"]}}},
    "required": ["name", "agents"]}


def crew_of(arguments: dict, limit: int = MAX_AGENTS) -> list[dict]:
    """A crew from the split: clean one-word English names (not used twice), colours, tasks."""
    crew, taken = [], set()
    for given in (arguments.get("agents") if isinstance(arguments, dict) else None) or []:
        if not isinstance(given, dict):
            continue
        task = re.sub(r"\s+", " ", str(given.get("task") or "")).strip()[:1200]
        if not task:
            continue
        name = re.sub(r"(?i)agent", "", re.sub(r"[^A-Za-z]", "", str(given.get("name") or "")))[:14]
        name = name[:1].upper() + name[1:]
        if len(name) < 3 or name in taken:
            name = next((n for n in NAMES if n not in taken), f"Agent{len(crew) + 1}")
        taken.add(name)
        color, hexa = COLORS[len(crew) % len(COLORS)]
        crew.append({"id": "agent_" + uuid.uuid4().hex[:12], "name": name, "color": color, "hex": hexa, "task": task})
        if len(crew) == limit:
            break
    return crew


def reports_text(crew: list[dict]) -> str:
    return "\n\n".join(f"## {a['name']} -- {a['task']}\n{a.get('result') or '(no report)'}" for a in crew)


def without_thinking(text: str) -> str:
    """A reply without a reasoning block the model may have opened first."""
    text = re.sub(r"(?s)<think>.*?</think>", "", text)
    return text.split("</think>", 1)[-1].strip() if "</think>" in text else text.strip()


class Crew:
    """One message's agents: split, run at the same time, reported."""

    def __init__(self, *, llama, web, emit: Callable[[str, dict], None], cancel: threading.Event) -> None:
        self.llama, self.web, self.emit, self.cancel = llama, web, emit, cancel
        self.lock = threading.Lock()

    def _say(self, kind: str, payload: dict) -> None:
        with self.lock:                              # the agents' threads share one event stream
            self.emit(kind, payload)

    def _json(self, messages: list[dict], schema: dict, n_predict: int = 400) -> dict:
        prompt = self.llama.apply_template(messages)
        text = "".join(self.llama.stream(prompt, n_predict=n_predict, sampling=COOL, json_schema=schema, cancel=self.cancel))
        start = text.find("{")
        try:
            return json.loads(text[start:]) if start >= 0 else {}
        except ValueError:
            return {}

    def split(self, request: str) -> list[dict]:
        found = self._json([{"role": "system", "content": SPLIT.format(n=MAX_AGENTS)},
                            {"role": "user", "content": request}], SPLIT_SCHEMA, 600)
        return crew_of(found) or crew_of({"agents": [{"name": "Atlas", "task": request}]})

    def _agent(self, agent: dict) -> dict:
        face = {k: agent[k] for k in ("id", "name", "color", "hex", "task")}
        self._say("agent", {**face, "status": "running"})
        started, steps = time.monotonic(), []
        messages = [{"role": "system", "content": RULES.format(tools=WITH_WEB if self.web else WITHOUT_WEB)},
                    {"role": "user", "content": f"Your name: {agent['name']}\nYour task: {agent['task']}"}]
        try:
            for round_no in range(ROUNDS if self.web else 0):
                first = round_no == 0
                choice = self._json(messages + [{"role": "user", "content":
                                    "Your first step, as one JSON object: web_search (or fetch_url for an address you know)."
                                    if first else "Your next step, as one JSON object: web_search, fetch_url, or reply (to report)."}],
                                    FIRST_SCHEMA if first else DECIDE_SCHEMA, 200)
                name, arguments = choice.get("name"), choice.get("arguments") or {}
                if name not in ("web_search", "fetch_url"):
                    break
                step = {"tool": name, **{k: str(v)[:200] for k, v in arguments.items() if k in ("query", "url")}}
                steps.append(step)
                self._say("agent", {**face, "status": "working", "step": step})
                try:
                    if name == "web_search":
                        found = self.web.search(str(arguments.get("query") or ""), 5)
                        result = "\n\n".join(f"[{i + 1}] {r['title']}\n{r['url']}\n{r['snippet']}" for i, r in enumerate(found)) or "No results."
                    else:
                        page = self.web.fetch(str(arguments.get("url") or ""), chars=RESULT_CHARS)
                        result = f"{page['title']}\n{page['url']}\n\n{page['text']}"
                except Exception as error:  # noqa: BLE001 - a failed search is something to read, not a crash
                    result = f"The tool failed: {error}"
                messages += [{"role": "assistant", "content": json.dumps({"name": name, "arguments": arguments})},
                             {"role": "user", "content": "Result:\n" + result[:RESULT_CHARS]}]
            messages.append({"role": "user", "content": "Now write your report as plain text: what you found for your "
                             "task, short and concrete, with the addresses it came from."})
            prompt = self.llama.apply_template(messages)
            report = without_thinking("".join(self.llama.stream(prompt, n_predict=REPORT_TOKENS, sampling=WARM, cancel=self.cancel)))
            done = {**face, "status": "done", "result": report or "(no report)", "steps": steps,
                    "seconds": round(time.monotonic() - started, 1)}
        except Cancelled:
            raise
        except (EngineError, OSError, ValueError) as error:
            done = {**face, "status": "error", "result": f"(the agent failed: {str(error)[:200]})", "steps": steps,
                    "seconds": round(time.monotonic() - started, 1)}
        self._say("agent", done)
        return done

    def run(self, request: str) -> tuple[dict, str]:
        """(the crew's step record, the notes the model answers from)."""
        ident = "crew_" + uuid.uuid4().hex[:8]
        self._say("tool_start", {"id": ident, "name": "spawn_agents"})
        crew = self.split(request)
        self._say("tool_call", {"id": ident, "name": "spawn_agents", "args": {"agents": [{"name": a["name"], "task": a["task"]} for a in crew]}})
        with ThreadPoolExecutor(max_workers=len(crew)) as pool:
            finished = list(pool.map(self._agent, crew))
        record = {"id": ident, "name": "spawn_agents", "args": {"agents": [{"name": a["name"], "task": a["task"]} for a in crew]},
                  "status": "done", "meta": {"agents": finished}, "output": ""}
        self._say("tool_result", record)
        return record, NOTES.format(reports=reports_text(finished))
