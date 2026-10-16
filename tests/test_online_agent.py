"""An online model worked by the studio's own agent (desktop/online.py, OnlineEngine): a service that
answers like OpenAI's is asked for a decision as JSON, plans a file, writes it as the continuation of
an opened code fence, and the file is on the disk -- the same Turn a local model runs."""

import json
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desktop import online  # noqa: E402
from desktop.agent import Profile, Turn  # noqa: E402


class Service(BaseHTTPRequestHandler):
    """Answers as a model would: a plan, then 'done', to decisions; the file to a continuation."""
    seen: list = []
    planned = False

    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Service.seen.append(request)
        last = request["messages"][-1]["content"]
        if "Decide your next step" in last:
            if '"const": "plan"' in last and not Service.planned:
                Service.planned = True
                # A sentence and a fence around it, as models do: the engine finds the object.
                reply = ("Here is my plan:\n```json\n" + json.dumps({"name": "plan", "arguments": {
                    "goal": "Write notes.txt", "folder": "", "steps": [
                        {"do": "create", "path": "notes.txt", "detail": "Two lines of notes."}]}}) + "\n```")
            elif '"const": "step_done"' in last:
                reply = json.dumps({"name": "step_done", "arguments": {"note": "written"}})
            else:
                reply = json.dumps({"name": "reply"})
        elif "Continue your last message" in last:
            reply = "first line from the service\nsecond line\n```\nThat is the file."
        else:
            reply = "notes.txt is written."
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        end = {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"completion_tokens": len(reply) // 4}}
        try:
            for i in range(0, len(reply), 7):
                self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'content': reply[i:i + 7]}}]})}\n\n".encode())
            self.wfile.write(f"data: {json.dumps(end)}\n\ndata: [DONE]\n\n".encode())
        except OSError:
            pass                                         # the reader hung up after a stop word, as it should


class OnlineAgent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Service)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "demo"
        self.root.mkdir()
        self.data = Path(tempfile.mkdtemp())
        self.patch = mock.patch("desktop.checkpoints.data_dir", lambda: self.data)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.root.parent, ignore_errors=True)
        shutil.rmtree(self.data, ignore_errors=True)

    def test_the_first_json_object_is_found_in_prose_and_fences(self):
        self.assertEqual(online.first_json_object('Sure:\n```json\n{"name": "reply", "x": "}"}\n```'),
                         '{"name": "reply", "x": "}"}')
        self.assertEqual(online.first_json_object("no object {here"), "")

    def test_an_online_model_plans_and_writes_a_file(self):
        settings = {"memory": 0, "level": 5, "temperature": 5, "agent": {"max_steps": 12},
                    "online": {"keys": {"custom": "k"}, "base": self.base, "active": {"provider": "custom", "model": "m"}}}
        events: list = []
        engine = online.OnlineEngine(settings, "custom", "m", emit=lambda e, d: events.append((e, d)))
        turn = Turn(llama=engine, profile=Profile(engine.label, "chat", online.GENERIC_SYSTEM, False),
                    settings=settings, folder=self.root, inside=None, web=None, history=[],
                    request="Make notes.txt with two lines.", plan=False,
                    emit=lambda e, d: events.append((e, d)), cancel=threading.Event(),
                    ask=lambda key, payload: "allow", needs_asking=lambda key: False, first=True)
        message = turn.run(turn.sent_text([]))
        written = (self.root / "notes.txt").read_text(encoding="utf-8")
        self.assertIn("first line from the service", written)
        self.assertNotIn("```", written)                         # the stop word ends the file
        self.assertNotIn("That is the file", written)
        self.assertTrue(any(e == "plan" or e == "planning" for e, _ in events), [e for e, _ in events][:20])
        decisions = [r for r in Service.seen if "Decide your next step" in r["messages"][-1]["content"]]
        self.assertTrue(decisions)
        self.assertEqual(decisions[0]["model"], "m")
        self.assertTrue(message.get("content") is not None)


if __name__ == "__main__":
    unittest.main()
