"""The 2.5 studio's new parts: tools, the agent's decision, the Hub, the web, storage.

    python -m unittest tests.test_desktop_agent

Nothing here starts a model or touches the network: the agent is exercised
against the echo engine and pages are parsed from strings.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from desktop import hub, tools as T
from desktop.agent import Profile, Turn, partial_strings
from desktop.app import inside
from desktop.battle import Battles
from desktop.chats import Chats
from desktop.engine import Echo
from desktop.web import WebError, _dig, html_to_text, refuse_local

REPO = Path(__file__).resolve().parents[1]


class Folder(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()) / "demo"
        self.root.mkdir()
        self.ctx = T.Context(folder=self.root, inside=lambda rel: inside(self.root, rel))

    def tearDown(self) -> None:
        shutil.rmtree(self.root.parent, ignore_errors=True)

    def test_write_then_read_then_edit(self):
        written = T.write_file(self.ctx, {"path": "src/hello.py", "content": "print('hi')\n"})
        self.assertTrue(written.ok)
        self.assertTrue(written.meta["created"])
        self.assertEqual((self.root / "src/hello.py").read_text(encoding="utf-8"), "print('hi')\n")
        read = T.read_file(self.ctx, {"path": "src/hello.py"})
        self.assertIn("1: print('hi')", read.text)
        edited = T.edit_file(self.ctx, {"path": "src/hello.py", "find": "hi", "replace": "hello"})
        self.assertTrue(edited.ok)
        self.assertIn("+print('hello')", edited.output)

    def test_an_edit_that_does_not_match_changes_nothing(self):
        (self.root / "a.txt").write_text("one two", encoding="utf-8")
        result = T.edit_file(self.ctx, {"path": "a.txt", "find": "three", "replace": "x"})
        self.assertFalse(result.ok)
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "one two")

    def test_a_tool_cannot_leave_the_folder(self):
        with self.assertRaises(RuntimeError):
            T.write_file(self.ctx, {"path": "../outside.txt", "content": "x"})
        self.assertFalse((self.root.parent / "outside.txt").exists())

    def test_listing_skips_what_nobody_reads(self):
        (self.root / "node_modules" / "x").mkdir(parents=True)
        (self.root / "main.py").write_text("", encoding="utf-8")
        listing = T.list_files(self.ctx, {"path": "."}).text
        self.assertIn("main.py", listing)
        self.assertIn("node_modules/ (skipped)", listing)
        self.assertNotIn("x", listing.split("node_modules/ (skipped)")[1])

    def test_a_command_runs_in_the_folder_and_is_cut_off(self):
        import os

        (self.root / "marker.txt").write_text("", encoding="utf-8")
        listed = T.run_command(self.ctx, {"command": "dir /b" if os.name == "nt" else "ls"})
        self.assertTrue(listed.ok)
        self.assertIn("marker.txt", listed.text)
        self.ctx.command_timeout = 2
        slow = T.run_command(self.ctx, {"command": "ping -n 20 127.0.0.1" if os.name == "nt" else "sleep 20"})
        self.assertFalse(slow.ok)
        self.assertLess(slow.meta["seconds"], 15)


class Decision(unittest.TestCase):
    def test_the_grammar_offers_each_tool_and_reply(self):
        schema = T.decision_schema(T.available(folder=True, web=False))
        names = [option["properties"]["name"]["const"] for option in schema["oneOf"]]
        self.assertIn("write_file", names)
        self.assertNotIn("web_search", names)
        self.assertEqual(names[-1], "reply")
        write = next(o for o in schema["oneOf"] if o["properties"]["name"]["const"] == "write_file")
        self.assertEqual(write["properties"]["arguments"]["required"], ["path", "content"])

    def test_planning_is_read_only(self):
        names = {t.name for t in T.available(folder=True, web=True, read_only=True)}
        self.assertEqual(names, {"list_files", "read_file", "search_files", "web_search", "fetch_url"})

    def test_a_call_written_as_prose_is_found_and_a_code_sample_is_not(self):
        found = T.call_in_text('Sure.\n<tool_call>\n{"name": "write_file", "arguments": {"path": "a.py", '
                               '"content": "x"}}\n</tool_call>')
        self.assertEqual(found[:2], ("write_file", {"path": "a.py", "content": "x"}))
        self.assertIsNone(T.call_in_text('```json\n{"name": "Ada", "age": 36}\n```'))

    def test_partial_arguments_decode_while_they_arrive(self):
        text = '{"name": "write_file", "arguments": {"path": "hello.py", "content": "print(\\"hi\\")\\nprint(\\"b'
        got = partial_strings(text, ("path", "content"))
        self.assertEqual(got["path"], "hello.py")
        self.assertEqual(got["content"], 'print("hi")\nprint("b')


class AgentTurn(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()) / "demo"
        self.root.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.root.parent, ignore_errors=True)

    def turn(self, request: str, answer: str = "allow", events: list | None = None) -> Turn:
        return Turn(llama=Echo(), profile=Profile("Echo", "chat", "You are a test.", True),
                    settings={"memory": 0, "agent": {"max_steps": 4}}, folder=self.root,
                    inside=lambda rel: inside(self.root, rel), web=None, history=[], request=request,
                    plan=False, emit=lambda e, d: (events.append((e, d)) if events is not None else None),
                    cancel=threading.Event(), ask=lambda key, payload: answer, needs_asking=lambda key: True,
                    first=True)

    def test_asked_to_make_a_file_it_makes_the_file(self):
        events: list = []
        turn = self.turn("make a file", events=events)
        message = turn.run(turn.sent_text([]))
        self.assertTrue((self.root / "echo.txt").is_file())
        self.assertEqual([s["name"] for s in message["steps"]], ["write_file"])
        self.assertIn("tool_result", [e for e, _ in events])
        self.assertTrue(message["content"])

    def test_a_refusal_changes_nothing_and_is_recorded(self):
        turn = self.turn("make a file", answer="deny")
        message = turn.run(turn.sent_text([]))
        self.assertFalse((self.root / "echo.txt").exists())
        self.assertEqual(message["steps"][0]["status"], "refused")

    def test_the_prompt_says_where_it_is(self):
        turn = self.turn("hello")
        system = turn.system_prompt()
        self.assertIn(str(self.root), system)
        self.assertIn("write_file(path, content)", system)
        self.assertIn("Internet: off", system)
        self.assertIn("[Files in the working folder:", turn.sent_text([]))

    def test_the_folder_name_written_into_a_path_is_understood(self):
        turn = self.turn("hello")
        self.assertEqual(turn._inside("demo/a.py"), (self.root / "a.py").resolve())
        self.assertEqual(turn._inside("./a.py"), (self.root / "a.py").resolve())
        with self.assertRaises(RuntimeError):
            turn._inside("demo/../../x")
        fixed = turn._normalise("run_command", {"command": r"python demo/hello.py && type demo\a.txt"})
        self.assertEqual(fixed["command"], "python hello.py && type a.txt")
        self.assertEqual(turn._normalise("write_file", {"path": "demo/x.py"})["path"], "x.py")
        (self.root / "demo").mkdir()
        self.assertEqual(turn._normalise("write_file", {"path": "demo/x.py"})["path"], "demo/x.py")


class Hub(unittest.TestCase):
    def test_every_way_of_naming_a_repository(self):
        cases = {
            "https://huggingface.co/Qwen/Qwen3-8B-GGUF/blob/main/Qwen3-8B-Q4_K_M.gguf":
                ("Qwen/Qwen3-8B-GGUF", "main", "Qwen3-8B-Q4_K_M.gguf", ""),
            "hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M":
                ("bartowski/Llama-3.2-3B-Instruct-GGUF", "main", "", "Q4_K_M"),
            "owner/name": ("owner/name", "main", "", ""),
            "https://huggingface.co/a/b/tree/dev": ("a/b", "dev", "", ""),
        }
        for link, want in cases.items():
            got = hub.parse(link)
            self.assertEqual((got["repo"], got["revision"], got["path"], got["quant"]), want, link)
        for bad in ("", "https://github.com/a/b", "https://huggingface.co/datasets/a/b", "just-one"):
            with self.assertRaises(hub.HubError):
                hub.parse(bad)

    def test_quantisations_are_read_from_names_and_shards_grouped(self):
        self.assertEqual(hub.quant_of("qwen2.5-coder-7b-instruct-q3_k_m.gguf"), "Q3_K_M")
        self.assertEqual(hub.quant_of("m-IQ4_XS.gguf"), "IQ4_XS")
        groups = hub._ggufs([{"path": f"big-Q8_0-0000{i}-of-00002.gguf", "bytes": 5, "sha256": ""} for i in (1, 2)]
                            + [{"path": "mmproj-f16.gguf", "bytes": 1, "sha256": ""}])
        model = next(g for g in groups if g["role"] == "model")
        self.assertEqual((model["bytes"], len(model["files"]), model["quant"]), (10, 2, "Q8_0"))
        self.assertEqual(next(g for g in groups if g["role"] == "projector")["name"], "mmproj-f16.gguf")


class Web(unittest.TestCase):
    def test_a_page_reads_as_its_text(self):
        title, text = html_to_text("<html><head><title>T &amp; U</title><style>x{}</style></head><body>"
                                   "<nav>menu</nav><main><h1>Head</h1><p>One <b>two</b></p><script>bad()</script>"
                                   "<ul><li>a</li><li>b</li></ul></main></body></html>")
        self.assertEqual(title, "T & U")
        self.assertIn("Head", text)
        self.assertIn("One two", text)
        self.assertIn("- a", text)
        self.assertNotIn("bad()", text)
        self.assertNotIn("menu", text)

    def test_this_computer_is_not_the_web(self):
        for host in ("127.0.0.1", "localhost", "10.0.0.5", "192.168.1.1"):
            with self.assertRaises(WebError):
                refuse_local(host)

    def test_results_are_found_by_path(self):
        self.assertEqual(_dig({"data": {"items": [{"u": 1}]}}, "data.items.0.u"), 1)
        self.assertIsNone(_dig({"a": 1}, "b.c"))


class Storage(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_chat_takes_its_title_from_the_first_message(self):
        chats = Chats(self.dir / "chats")
        chat = chats.create()
        chats.append(chat["id"], {"role": "user", "content": "Write me a parser"})
        chats.append(chat["id"], {"role": "assistant", "content": "Done", "model": "Echo"})
        listing = chats.listing()
        self.assertEqual((listing[0]["title"], listing[0]["count"], listing[0]["model"]),
                         ("Write me a parser", 2, "Echo"))
        with self.assertRaises(KeyError):
            chats.get("../settings")

    def test_conversations_from_2_0_are_brought_over_once(self):
        legacy = self.dir / "studio"
        legacy.mkdir()
        (legacy / "chats.json").write_text(json.dumps([{"id": "c1", "title": "Old", "created_at": "2026-01-01T00:00:00+00:00"}]),
                                           encoding="utf-8")
        (legacy / "messages.json").write_text(json.dumps([
            {"chat_id": "c1", "role": "user", "content": "hi", "created_at": "2026-01-01T00:00:01+00:00"},
            {"chat_id": "c1", "role": "assistant", "content": "hello", "created_at": "2026-01-01T00:00:02+00:00"}]),
            encoding="utf-8")
        chats = Chats(self.dir / "chats", legacy=legacy)
        self.assertEqual([c["title"] for c in chats.listing()], ["Old"])
        Chats(self.dir / "chats", legacy=legacy)
        self.assertEqual(len(chats.listing()), 1)

    def test_the_score_follows_the_contender_not_the_side(self):
        battles = Battles(self.dir / "battles.json")
        battles.save({"id": "b1", "sides": {"a": "adapter", "b": "base"}, "vote": None})
        battles.save({"id": "b2", "sides": {"a": "base", "b": "adapter"}, "vote": None})
        battles.vote("b1", "a")
        battles.vote("b2", "b")
        score = battles.score()
        self.assertEqual((score["adapter"], score["base"], score["votes"]), (2, 0, 2))


class Library(unittest.TestCase):
    # The public repository carries no GGUF; the build fetches the adapters.
    @unittest.skipUnless((REPO / "desktop/assets/nimbus-2-apex.gguf").is_file(), "adapters are not in this checkout")
    def test_the_shipped_adapters_say_what_they_are(self):
        from desktop.library import gguf_header

        one = gguf_header(REPO / "desktop/assets/nimbus-1-1-prime-ee.gguf")
        apex = gguf_header(REPO / "desktop/assets/nimbus-2-apex.gguf")
        self.assertEqual((one["type"], one["architecture"]), ("adapter", "qwen2"))
        self.assertEqual((apex["type"], apex["architecture"]), ("adapter", "qwen3"))


if __name__ == "__main__":
    unittest.main()
