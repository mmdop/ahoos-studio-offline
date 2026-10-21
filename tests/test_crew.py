"""The offline studio's agents (desktop/crew.py): a message split into a crew, the agents at work
at the same time, their progress as the online studio's events, and their reports as notes."""

import json
import threading
import time
import unittest

from desktop import crew as C


class FakeModel:
    """The four calls crew.py makes: a split, each agent's steps, its report."""

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.reports = 0

    def apply_template(self, messages):
        return json.dumps(messages)

    def stream(self, prompt, *, n_predict, sampling, json_schema=None, cancel=None, **_):
        messages = json.loads(prompt)
        with self.lock:
            self.calls.append(messages)
        if json_schema is C.SPLIT_SCHEMA:
            yield json.dumps({"agents": [{"name": "AgentA", "task": "gold price"}, {"name": "Nova", "task": "silver price"}]})
            return
        if json_schema is C.FIRST_SCHEMA:
            yield json.dumps({"name": "web_search", "arguments": {"query": "price"}})
            return
        if json_schema is C.DECIDE_SCHEMA:
            searched = any(m["content"].startswith("Result:") for m in messages)
            yield json.dumps({"name": "reply"} if searched else {"name": "web_search", "arguments": {"query": "price"}})
            return
        time.sleep(0.2)                                  # a report takes a while: two at once take one while
        yield "<think>hm</think>Found it: 42 (https://example.com)"


class FakeWeb:
    def search(self, query, count):
        return [{"title": "Prices", "url": "https://example.com", "snippet": "42"}]


class Crew(unittest.TestCase):
    def test_names_and_colours(self):
        crew = C.crew_of({"agents": [{"name": "AgentA", "task": "a"}, {"name": "PythonAgent", "task": "b"},
                                     {"name": "x", "task": ""}, {"name": "Lyra", "task": "c"}]})
        self.assertEqual([a["name"] for a in crew], ["Atlas", "Python", "Lyra"])
        self.assertEqual([a["color"] for a in crew], ["red", "orange", "yellow"])

    def test_a_crew_works_at_once_and_reports(self):
        events = []
        model = FakeModel()
        started = time.monotonic()
        record, notes = C.Crew(llama=model, web=FakeWeb(), emit=lambda k, p: events.append((k, p)),
                               cancel=threading.Event()).run("gold and silver prices")
        seconds = time.monotonic() - started
        self.assertLess(seconds, 0.35)                   # two reports of 0.2 s each, side by side
        kinds = [k for k, _ in events]
        self.assertEqual(kinds[0], "tool_start")
        self.assertEqual(kinds[-1], "tool_result")
        agents = [p for k, p in events if k == "agent"]
        self.assertEqual({(a["name"], a["status"]) for a in agents if a["status"] != "working"},
                         {("Atlas", "running"), ("Nova", "running"), ("Atlas", "done"), ("Nova", "done")})
        self.assertTrue(any(a.get("step") == {"tool": "web_search", "query": "price"} for a in agents))
        self.assertEqual([a["result"] for a in record["meta"]["agents"]], ["Found it: 42 (https://example.com)"] * 2)
        self.assertIn("## Atlas -- gold price\nFound it: 42", notes)
        self.assertIn("do not fill it in from memory", notes)

    def test_without_the_internet_an_agent_only_reports(self):
        model = FakeModel()
        record, _ = C.Crew(llama=model, web=None, emit=lambda k, p: None, cancel=threading.Event()).run("x")
        self.assertTrue(all(not a["steps"] for a in record["meta"]["agents"]))


if __name__ == "__main__":
    unittest.main()
