"""The studio's agent and the parts around it: tools, the decision, writing and
editing files, the checks, undo, background commands, the Hub, the web, storage.

    python -m unittest tests.test_desktop_agent

Nothing here starts a model or touches the network: the agent is exercised
against the echo engine and pages are parsed from strings.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from desktop import checks, hub, procs, writer
from desktop import tools as T
from desktop.agent import Plan, Profile, Turn, language_of, partial_strings, plan_from_args, slug
from desktop.app import inside
from desktop.battle import Battles
from desktop.chats import Chats
from desktop.engine import Echo
from desktop import runtime
from desktop.runtime import blocked, offload_note
from desktop.web import WebError, _dig, html_to_text, refuse_local

REPO = Path(__file__).resolve().parents[1]
PY = f'"{sys.executable}"'


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

    def test_listing_skips_what_nobody_reads_and_names_binaries(self):
        (self.root / "node_modules" / "x").mkdir(parents=True)
        (self.root / "main.py").write_text("", encoding="utf-8")
        (self.root / "game.exe").write_bytes(b"MZ\x00\x00")
        listing = T.list_files(self.ctx, {"path": "."}).text
        self.assertIn("main.py", listing)
        self.assertIn("node_modules/ (skipped)", listing)
        self.assertIn("game.exe  (4 B, binary)", listing)
        self.assertNotIn("x", listing.split("node_modules/ (skipped)")[1].split("game.exe")[0])

    def test_a_binary_file_is_refused_by_name(self):
        (self.root / "setup.exe").write_bytes(b"MZ\x90\x00binary")
        result = T.read_file(self.ctx, {"path": "setup.exe"})
        self.assertFalse(result.ok)
        self.assertIn("binary", result.text)
        self.assertIn("do not try to read it again", result.text)

    def test_a_command_runs_in_the_folder_and_is_cut_off(self):
        (self.root / "marker.txt").write_text("", encoding="utf-8")
        listed = T.run_command(self.ctx, {"command": "dir /b" if os.name == "nt" else "ls"})
        self.assertTrue(listed.ok)
        self.assertIn("marker.txt", listed.text)
        self.ctx.command_timeout = 2
        slow = T.run_command(self.ctx, {"command": f'{PY} -c "import time; time.sleep(20)"'})
        self.assertFalse(slow.ok)
        self.assertIn("background", slow.text)
        self.assertLess(slow.meta["seconds"], 15)

    def test_a_server_keeps_running_in_the_background(self):
        line = "print('Serving on http://127.0.0.1:8765', flush=True); import time; time.sleep(60)"
        result = T.run_command(self.ctx, {"command": f'{PY} -c "{line}"', "background": "yes"})
        self.assertTrue(result.ok, result.text)
        proc_id = result.meta["background"]
        self.assertEqual(result.meta["url"], "http://127.0.0.1:8765")
        self.assertTrue(procs.PROCS.get(proc_id).running)
        stopped = T.stop_command(self.ctx, {"id": proc_id})
        self.assertTrue(stopped.ok)
        for _ in range(50):
            if not procs.PROCS.get(proc_id).running:
                break
            time.sleep(0.1)
        self.assertFalse(procs.PROCS.get(proc_id).running)

    def test_a_quiet_command_that_said_its_address_is_kept_not_killed(self):
        self.ctx.command_timeout = 3
        line = "print('listening at http://localhost:5001', flush=True); import time; time.sleep(60)"
        result = T.run_command(self.ctx, {"command": f'{PY} -c "{line}"'})
        self.assertTrue(result.ok, result.text)
        self.assertIn("moved to the background", result.text)
        procs.PROCS.stop(result.meta["background"])

    def test_servers_are_known_by_their_command(self):
        for command in ("python -m http.server 8000", "npm run dev", "npx serve .", "flask run", "uvicorn app:app"):
            self.assertTrue(procs.looks_like_server(command), command)
        for command in ("python main.py", "npm install", "node --check a.js"):
            self.assertFalse(procs.looks_like_server(command), command)
        self.assertEqual(procs.address_in("Serving HTTP on 0.0.0.0 port 8000 (http://0.0.0.0:8000/) ..."),
                         "http://localhost:8000/")

    def test_only_pages_and_pictures_are_opened(self):
        (self.root / "run.bat").write_text("echo hi", encoding="utf-8")
        result = T.open_file(self.ctx, {"path": "run.bat"})
        self.assertFalse(result.ok)
        (self.root / "index.html").write_text("<p>hi</p>", encoding="utf-8")
        with mock.patch("desktop.tools.webbrowser.open") as opened:
            self.assertTrue(T.open_file(self.ctx, {"path": "index.html"}).ok)
            self.assertTrue(opened.call_args[0][0].startswith("file:"))


class Decision(unittest.TestCase):
    def test_the_grammar_offers_each_tool_and_reply(self):
        schema = T.decision_schema(T.available(folder=True, web=False))
        names = [option["properties"]["name"]["const"] for option in schema["oneOf"]]
        self.assertIn("write_file", names)
        self.assertNotIn("web_search", names)
        self.assertEqual(names[-1], "reply")
        write = next(o for o in schema["oneOf"] if o["properties"]["name"]["const"] == "write_file")
        # 3.0: a file is not an argument -- it is written afterwards, as a code block.
        self.assertEqual(write["properties"]["arguments"]["required"], ["path"])
        self.assertNotIn("content", write["properties"]["arguments"]["properties"])

    def test_the_plan_is_the_arguments_of_one_call(self):
        schema = T.decision_schema([], plan=True, resume=True, step_done=True)
        names = [option["properties"]["name"]["const"] for option in schema["oneOf"]]
        self.assertEqual(names, ["plan", "resume", "step_done", "reply"])
        plan = schema["oneOf"][0]["properties"]["arguments"]
        self.assertEqual(plan["required"], ["goal", "folder", "steps"])
        self.assertEqual(plan["properties"]["steps"]["items"]["properties"]["do"]["enum"],
                         ["create", "edit", "run", "other"])

    def test_read_only_is_what_changes_nothing(self):
        names = {t.name for t in T.available(folder=True, web=True, read_only=True)}
        self.assertEqual(names, {"list_files", "read_file", "search_files", "stop_command", "open_file",
                                 "web_search", "fetch_url"})

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

    def test_a_plan_is_cleaned_as_it_is_read(self):
        plan = plan_from_args({"goal": "g", "folder": "Snake Game!", "steps": [
            {"do": "create", "path": "./index.html", "detail": "the page"},
            {"do": "create", "path": "", "detail": "something with no file"},
            {"do": "fly", "path": "x", "detail": "?"},
            "not a step"]})
        self.assertEqual([(s.do, s.path) for s in plan.steps], [("create", "index.html"), ("other", ""), ("other", "x")])
        self.assertEqual(plan.steps[0].title, "index.html")
        self.assertEqual(slug(plan.folder), "snake-game")
        again = Plan.from_dict(json.loads(json.dumps(plan.to_dict())))
        self.assertEqual(again.to_dict(), plan.to_dict())

    def test_the_answer_is_in_the_language_asked(self):
        self.assertEqual(language_of("یه بازی مار بساز"), "Persian")
        self.assertEqual(language_of("make a snake game"), "")
        self.assertEqual(slug("بازی مار"), "")


class Writing(unittest.TestCase):
    def test_a_file_comes_out_of_its_code_block(self):
        self.assertEqual(writer.clean_file("print(1)\n```\nThat is the file."), "print(1)\n")
        self.assertEqual(writer.clean_file("```python\nprint(1)\n```"), "print(1)\n")
        self.assertEqual(writer.opening("app.js"), "```javascript\n")
        self.assertEqual(writer.closing("app.js"), "\n```")

    def test_a_readme_keeps_its_own_code_blocks(self):
        body = "# App\n\n```bash\npython app.py\n```\n\nThat is all.\n"
        self.assertEqual(writer.opening("README.md"), "````markdown\n")
        self.assertEqual(writer.clean_file(body + "````\n", "README.md"), body)

    def test_placeholders_are_noticed(self):
        found = writer.placeholders("def a():\n    pass\n# ... rest of the code\n// TODO: add scoring\n")
        self.assertEqual(len(found), 2)
        self.assertEqual(writer.placeholders("x = 1  # the total\n"), [])

    def test_search_replace_blocks_are_applied_where_they_are_unique(self):
        original = "def area(r):\n    return 3 * r * r\n\nprint(area(2))\n"
        edits = writer.parse_edits("Fix pi:\n<<<<<<< SEARCH\n    return 3 * r * r\n=======\n"
                                   "    return 3.14159 * r * r\n>>>>>>> REPLACE\n")
        new, problems = writer.apply_edits(original, edits)
        self.assertEqual(problems, [])
        self.assertIn("3.14159", new)
        self.assertTrue(new.endswith("print(area(2))\n"))

    def test_an_edit_with_its_indentation_off_still_lands(self):
        original = "class A:\n    def f(self):\n        return 1\n"
        edits = [writer.Edit("def f(self):\n    return 1", "def f(self):\n    return 2")]
        new, problems = writer.apply_edits(original, edits)
        self.assertEqual(problems, [])
        self.assertEqual(new, "class A:\n    def f(self):\n        return 2\n")

    def test_an_edit_that_is_not_unique_is_refused(self):
        original = "x = 1\nx = 1\n"
        new, problems = writer.apply_edits(original, [writer.Edit("x = 1", "x = 2")])
        self.assertEqual(new, original)
        self.assertIn("2 times", problems[0])
        new, problems = writer.apply_edits(original, [writer.Edit("y = 1", "y = 2")])
        self.assertIn("not in the file", problems[0])


class Checks(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, name: str, text: str) -> None:
        (self.root / name).write_text(text, encoding="utf-8")

    def test_python_that_does_not_compile(self):
        self.write("main.py", "def f(:\n    pass\n")
        found = checks.check(self.root, ["main.py"])
        self.assertEqual(found[0].path, "main.py")
        self.assertIn("syntax error", found[0].message)

    def test_json_css_and_good_files(self):
        self.write("data.json", '{"a": 1,}')
        self.write("style.css", "body { color: red;\n")
        self.write("ok.py", "print('ok')\n")
        found = {p.path: p.message for p in checks.check(self.root, ["data.json", "style.css", "ok.py"])}
        self.assertIn("not valid JSON", found["data.json"])
        self.assertIn("not closed", found["style.css"])
        self.assertNotIn("ok.py", found)

    def test_a_page_and_its_script_agree(self):
        self.write("index.html", '<html><head><link rel="stylesheet" href="styles.css"></head><body>'
                                 '<canvas id="board" width="400" height="400"></canvas><span id="score"></span>'
                                 '<script src="game.js"></script></body></html>')
        self.write("game.js", "const board = document.getElementById('board');\n"
                              "const best = document.querySelector('#best');\n"
                              "const score = document.getElementById('score');\n"
                              "const made = document.createElement('div'); made.id = 'overlay';\n"
                              "document.getElementById('overlay');\n")
        found = [p.message for p in checks.check(self.root, ["index.html", "game.js"])]
        self.assertTrue(any('loads stylesheet "styles.css"' in m for m in found), found)
        self.assertTrue(any("#best" in m for m in found), found)
        self.assertFalse(any("#board" in m or "#score" in m or "#overlay" in m for m in found), found)

    def test_a_const_assigned_again_is_caught(self):
        self.write("game.js", "const snake = [];\nconst size = 20;\nlet score = 0;\n"
                              "function restart() { snake = []; score = 0; }\n"
                              "const label = 'snake = 1'; // snake = 2 in a comment\n")
        found = [p.message for p in checks.check(self.root, ["game.js"])]
        self.assertEqual(len(found), 1, found)
        self.assertIn("`snake`", found[0])
        self.assertEqual(checks.reassigned_consts("const a = 1;\nfunction f() { const a = 2; }\na = 3;"), [])
        self.assertEqual(checks.reassigned_consts("const n = 1;\nn++;"), ["n"])
        self.assertEqual(checks.reassigned_consts("const o = {};\no.x = 1; if (o == 1) {}"), [])

    def test_a_canvas_with_no_size(self):
        self.write("index.html", '<canvas id="board"></canvas><script src="game.js"></script>')
        self.write("game.js", "const c = document.getElementById('board');\n")
        found = [p.message for p in checks.check(self.root, ["index.html"])]
        self.assertTrue(any("300×150" in m for m in found), found)
        self.write("game.js", "const c = document.getElementById('board');\nc.width = 400; c.height = 400;\n")
        self.assertFalse(any("300×150" in p.message for p in checks.check(self.root, ["index.html"])))
        self.write("index.html", '<canvas id="board" width="400" height="400"></canvas>')
        self.assertEqual(checks.check(self.root, ["index.html"]), [])

    def test_how_it_can_be_tried(self):
        self.assertEqual(checks.runnable(self.root, ["style.css", "index.html", "game.js"])["page"], "index.html")
        self.assertEqual(checks.runnable(self.root, ["tool.py"])["script"], "tool.py")


class Undo(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.project = self.dir / "project"
        self.project.mkdir()
        self.patch = mock.patch("desktop.checkpoints.data_dir", lambda: self.dir / "data")
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_turn_is_put_back_whole(self):
        from desktop import checkpoints

        (self.project / "kept.txt").write_text("before", encoding="utf-8")
        point = checkpoints.Checkpoint("t1", self.project)
        point.before(self.project / "kept.txt")
        (self.project / "kept.txt").write_text("after", encoding="utf-8")
        point.before(self.project / "kept.txt")          # only the first copy counts
        point.before(self.project / "src" / "new.py")
        (self.project / "src").mkdir()
        (self.project / "src" / "new.py").write_text("x", encoding="utf-8")
        self.assertEqual(sorted(point.paths), ["kept.txt", "src/new.py"])
        restored = checkpoints.undo("t1")
        self.assertEqual(sorted(restored), ["kept.txt", "src/new.py"])
        self.assertEqual((self.project / "kept.txt").read_text(encoding="utf-8"), "before")
        self.assertFalse((self.project / "src").exists())
        with self.assertRaises(KeyError):
            checkpoints.undo("t1")


class Settings(unittest.TestCase):
    def test_2x_defaults_move_to_3_0_and_choices_stay(self):
        from desktop.app import DEFAULT_SETTINGS, _merge, migrate

        old = {"context": 8192, "agent": {"max_steps": 10, "files": True}}
        moved = migrate(_merge(DEFAULT_SETTINGS, old), old)
        self.assertEqual((moved["context"], moved["agent"]["max_steps"], moved["settings_version"]), (16384, 40, 3))
        chosen = {"context": 32768, "agent": {"max_steps": 15}}
        kept = migrate(_merge(DEFAULT_SETTINGS, chosen), chosen)
        self.assertEqual((kept["context"], kept["agent"]["max_steps"]), (32768, 15))
        new = {"settings_version": 3, "context": 8192, "agent": {"max_steps": 10}}
        same = migrate(_merge(DEFAULT_SETTINGS, new), new)
        self.assertEqual((same["context"], same["agent"]["max_steps"]), (8192, 10))


class Runtime(unittest.TestCase):
    def test_windows_refusing_the_engine_is_recognised(self):
        self.assertTrue(blocked(-1058471934))
        self.assertTrue(blocked(0xC0E90002))
        self.assertFalse(blocked(1))
        self.assertFalse(blocked(None))

    def test_what_went_on_the_graphics_card(self):
        log = ["ggml_vulkan: 0 = NVIDIA GeForce GTX 1050 Ti (NVIDIA) | uma: 0 | fp16: 0",
               "load_tensors: offloaded 29/29 layers to GPU"]
        self.assertEqual(offload_note(log), "NVIDIA GeForce GTX 1050 Ti · 29/29 layers")
        self.assertEqual(offload_note(["load_tensors: offloaded 0/29 layers to GPU"]), "CPU")
        # As b10991 says it, at --log-verbosity 4.
        log = ["0.01.40 I llama_prepare_model_devices: using device Vulkan1 (NVIDIA GeForce GTX 1050 Ti) "
               "(0000:01:00.0) - 3628 MiB free", "0.02.24 I load_tensors: offloaded 15/29 layers to GPU"]
        self.assertEqual(offload_note(log), "NVIDIA GeForce GTX 1050 Ti · 15/29 layers")

    def test_the_card_is_given_small_blocks_and_asked_what_it_holds(self):
        folder = Path(tempfile.mkdtemp())
        try:
            model = folder / "m.gguf"
            model.write_bytes(b"GGUF")
            server = runtime.ModelServer(runtime.Settings(model=model), binary=folder / "llama-server")
            argv = server.command()
            self.assertEqual(argv[argv.index("--log-verbosity") + 1], "4")
            with mock.patch("desktop.runtime.subprocess.Popen") as popen,                     mock.patch.dict(os.environ, {"GGML_VK_SUBALLOCATION_BLOCK_SIZE": ""}, clear=False):
                os.environ.pop("GGML_VK_SUBALLOCATION_BLOCK_SIZE")
                popen.return_value.stdout = iter(())
                server.start()
                env = popen.call_args.kwargs["env"]
                self.assertEqual(env["GGML_VK_SUBALLOCATION_BLOCK_SIZE"], str(512 * 1024 * 1024))
            with mock.patch("desktop.runtime.subprocess.Popen") as popen,                     mock.patch.dict(os.environ, {"GGML_VK_SUBALLOCATION_BLOCK_SIZE": "268435456"}):
                popen.return_value.stdout = iter(())
                server.start()
                self.assertEqual(popen.call_args.kwargs["env"]["GGML_VK_SUBALLOCATION_BLOCK_SIZE"], "268435456")
        finally:
            shutil.rmtree(folder, ignore_errors=True)


class AgentTurn(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()) / "demo"
        self.root.mkdir()
        self.data = Path(tempfile.mkdtemp())
        self.patch = mock.patch("desktop.checkpoints.data_dir", lambda: self.data)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        shutil.rmtree(self.root.parent, ignore_errors=True)
        shutil.rmtree(self.data, ignore_errors=True)

    def turn(self, request: str, answer: str = "allow", events: list | None = None, **extra) -> Turn:
        return Turn(llama=Echo(delay=0), profile=Profile("Echo", "chat", "You are a test.", True),
                    settings={"memory": 0, "agent": {"max_steps": 12}}, folder=extra.pop("folder", self.root),
                    inside=None, web=None, history=extra.pop("history", []), request=request,
                    plan=extra.pop("plan", False),
                    emit=lambda e, d: (events.append((e, d)) if events is not None else None),
                    cancel=threading.Event(), ask=lambda key, payload: answer, needs_asking=lambda key: True,
                    first=True, **extra)

    def test_asked_to_make_a_file_it_plans_writes_and_checks(self):
        events: list = []
        turn = self.turn("make a file", events=events)
        message = turn.run(turn.sent_text([]))
        self.assertTrue((self.root / "echo.txt").is_file())
        self.assertEqual([s["name"] for s in message["steps"]], ["write_file"])
        self.assertEqual([s["status"] for s in message["plan"]["steps"]], ["done"])
        names = [e for e, _ in events]
        for event in ("plan", "plan_step", "tool_result", "check"):
            self.assertIn(event, names)
        self.assertEqual(message["changes"], ["echo.txt"])
        self.assertTrue(message["content"])

    def test_a_refused_plan_changes_nothing(self):
        turn = self.turn("make a file", answer="deny")
        message = turn.run(turn.sent_text([]))
        self.assertFalse((self.root / "echo.txt").exists())
        self.assertEqual([s["status"] for s in message["plan"]["steps"]], ["skipped"])
        self.assertNotIn("steps", message)

    def test_planning_only_writes_nothing(self):
        turn = self.turn("make a file", plan=True)
        message = turn.run(turn.sent_text([]))
        self.assertFalse((self.root / "echo.txt").exists())
        self.assertTrue(message["plan_only"])
        self.assertEqual(message["plan"]["steps"][0]["status"], "pending")

    def test_continue_picks_up_the_first_step_not_done(self):
        plan = {"goal": "two files", "folder": "", "steps": [
            {"title": "a", "do": "create", "path": "a.txt", "detail": "", "status": "done", "note": ""},
            {"title": "echo", "do": "create", "path": "echo.txt", "detail": "", "status": "pending", "note": ""}]}
        turn = self.turn("Continue.", resume=plan)
        message = turn.run(turn.sent_text([]))
        self.assertTrue((self.root / "echo.txt").is_file())
        self.assertFalse((self.root / "a.txt").exists())
        self.assertEqual([s["status"] for s in message["plan"]["steps"]], ["done", "done"])

    def test_with_no_folder_a_new_project_gets_one(self):
        workspace = self.root.parent / "workspace"
        made: list = []
        turn = self.turn("make a file", folder=None, workspace=workspace, on_folder=made.append)
        message = turn.run(turn.sent_text([]))
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0].parent, workspace.resolve())
        self.assertTrue((made[0] / "echo.txt").is_file())
        self.assertEqual(message["folder"], str(made[0]))
        from desktop import checkpoints

        checkpoints.undo(message["checkpoint"])
        self.assertFalse(made[0].exists())          # the folder the turn made goes too

    def test_a_refused_plan_makes_no_folder(self):
        workspace = self.root.parent / "workspace"
        turn = self.turn("make a file", answer="deny", folder=None, workspace=workspace)
        message = turn.run(turn.sent_text([]))
        self.assertFalse(workspace.exists() and any(workspace.iterdir()))
        self.assertNotIn("folder", message)

    def test_the_prompt_says_where_it_is_and_how_it_works(self):
        turn = self.turn("hello")
        system = turn.system_prompt()
        self.assertIn(str(self.root.resolve()), system)
        self.assertIn("write_file(path, about?)", system)
        self.assertIn("plan(goal, folder, steps)", system)
        self.assertIn("Internet: off", system)
        self.assertIn("[Files in the project folder:", turn.sent_text([]))

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

    def test_a_question_is_answered_without_a_plan(self):
        turn = self.turn("what is two and two?")
        message = turn.run(turn.sent_text([]))
        self.assertNotIn("plan", message)
        self.assertTrue(message["content"])


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
