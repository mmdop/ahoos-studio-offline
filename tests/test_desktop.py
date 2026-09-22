"""The offline studio's store, family and downloader.

    python -m unittest tests.test_desktop

The store stands in for Supabase under an unchanged app.js, so these check the
query shapes app.js actually sends -- and that the path a model's answer names
cannot reach outside the files folder.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

# At module level on purpose: this file uses postponed annotations, so a
# WebSocket imported inside a function is a name FastAPI cannot resolve
# when it reads the route's type hints, and the socket closes on connect.
from fastapi import WebSocket, WebSocketDisconnect

from desktop.store import Store


class StoreQueries(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store = Store(self.dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def q(self, table, **spec):
        result = self.store.query(table, spec)
        self.assertIsNone(result["error"], result["error"])
        return result["data"]

    def test_new_chat_returns_one_row_with_the_columns_asked_for(self):
        # sb.from('chats').insert({...}).select('id,title').single()
        row = self.q("chats", op="insert", values={"user_id": "u", "title": "Hello"},
                     columns="id,title", single=True)
        self.assertEqual(set(row), {"id", "title"})
        self.assertEqual(row["title"], "Hello")

    def test_history_is_newest_first_and_limited(self):
        for n in range(5):
            self.q("chats", op="insert", values={"title": f"c{n}", "updated_at": f"2026-01-0{n + 1}"})
        rows = self.q("chats", columns="id,title,updated_at",
                      order=[["updated_at", False]], limit=3)
        self.assertEqual([r["title"] for r in rows], ["c4", "c3", "c2"])

    def test_messages_filter_by_chat_in_order(self):
        chat = self.q("chats", op="insert", values={"title": "x"}, single=True)
        other = self.q("chats", op="insert", values={"title": "y"}, single=True)
        self.q("messages", op="insert", values={"chat_id": chat["id"], "role": "user",
                                                "content": "a", "created_at": "1"})
        self.q("messages", op="insert", values={"chat_id": other["id"], "role": "user",
                                                "content": "nope", "created_at": "2"})
        self.q("messages", op="insert", values={"chat_id": chat["id"], "role": "assistant",
                                                "content": "b", "created_at": "3"})
        rows = self.q("messages", filters=[["chat_id", "eq", chat["id"]]], order=[["created_at", True]])
        self.assertEqual([r["content"] for r in rows], ["a", "b"])

    def test_update_touches_only_the_matching_row(self):
        a = self.q("chats", op="insert", values={"title": "a", "updated_at": "old"}, single=True)
        b = self.q("chats", op="insert", values={"title": "b", "updated_at": "old"}, single=True)
        self.q("chats", op="update", values={"updated_at": "new"}, filters=[["id", "eq", a["id"]]])
        by_id = {r["id"]: r for r in self.q("chats")}
        self.assertEqual(by_id[a["id"]]["updated_at"], "new")
        self.assertEqual(by_id[b["id"]]["updated_at"], "old")

    def test_deleting_a_chat_takes_its_messages(self):
        chat = self.q("chats", op="insert", values={"title": "x"}, single=True)
        self.q("messages", op="insert", values={"chat_id": chat["id"], "content": "a"})
        self.q("chats", op="delete", filters=[["id", "eq", chat["id"]]])
        self.assertEqual(self.q("messages"), [])

    def test_an_unfiltered_delete_is_refused(self):
        self.q("chats", op="insert", values={"title": "keep me"})
        result = self.store.query("chats", {"op": "delete"})
        self.assertIsNotNone(result["error"])
        self.assertEqual(len(self.q("chats")), 1)

    def test_an_unimplemented_filter_fails_rather_than_matching_everything(self):
        result = self.store.query("chats", {"filters": [["title", "neq", "x"]]})
        self.assertIsNotNone(result["error"])

    def test_files_never_expire(self):
        row = self.q("files", op="insert", values={"name": "a.html", "path": "u/1-a.html"}, single=True)
        self.assertIsNone(row["expires_at"])

    def test_a_file_round_trips(self):
        self.store.put("u/123-page.html", b"<b>hi</b>")
        self.assertEqual(self.store.get("u/123-page.html"), b"<b>hi</b>")

    def test_a_path_cannot_leave_the_files_folder(self):
        # The name comes out of a model's answer.
        for path in ("../settings.json", "u/../../x", "/etc/passwd", "u\\..\\x", "", "u/a b.txt"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.store.put(path, b"x")
        self.assertFalse((self.dir / "settings.json").exists())

    def test_a_damaged_table_is_set_aside_not_fatal(self):
        (self.dir / "chats.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.q("chats"), [])
        self.assertTrue(any(p.name.startswith("chats.damaged-") for p in self.dir.iterdir()))


class OfflineFamily(unittest.TestCase):
    def test_one_model_answering_under_its_trained_prompt(self):
        from desktop import family
        from studio.compose import compose_system
        from studio.registry import Registry

        root = family.assemble(Path(tempfile.mkdtemp()) / "family")
        try:
            registry = Registry.load(root)
            self.assertEqual(list(registry.models), ["nimbus-1.1-prime-ee"])
            self.assertEqual([g.id for g in registry.gears], ["CR-file"])
            manager = registry.manager
            prompt = compose_system(registry.base, manager, registry.chips.for_model(manager.id))
            self.assertTrue(prompt.startswith("You are Nimbus, built by Ahoos Model Studio."))
            self.assertNotIn("delegat", prompt.split("# Skill chip")[0].lower())
        finally:
            shutil.rmtree(root.parent, ignore_errors=True)

    def test_the_repository_chip_is_not_rewritten(self):
        # Retargeting happens in the assembled copy, never in skills/ itself.
        text = (Path(__file__).resolve().parents[1] / "skills/rtl-web/chip.toml").read_text(encoding="utf-8")
        self.assertIn("nimbus-frontend-rza-1.0", text)


class Catalogue(unittest.TestCase):
    def test_default_build_exists_and_hashes_look_like_sha256(self):
        from desktop import catalogue

        default = catalogue.build(catalogue.DEFAULT_BUILD)
        self.assertEqual(default.model, catalogue.DEFAULT_MODEL)
        for build in catalogue.BUILDS:
            self.assertRegex(build.sha256, r"^[0-9a-f]{64}$")
            self.assertTrue(build.url.startswith("https://huggingface.co/Qwen/"))

    def test_every_build_belongs_to_a_model_and_every_model_has_builds(self):
        from desktop import catalogue

        for build in catalogue.BUILDS:
            catalogue.model(build.model)                      # raises if it does not exist
        for model in catalogue.MODELS:
            self.assertTrue(catalogue.builds_for(model.id), model.id)
            self.assertEqual(catalogue.default_build_for(model.id),
                             catalogue.builds_for(model.id)[0].key)

    def test_the_two_models_do_not_share_a_base_or_an_adapter(self):
        from desktop import catalogue

        bases = {m.base_repo for m in catalogue.MODELS}
        adapters = {m.adapter_file for m in catalogue.MODELS}
        self.assertEqual(len(bases), len(catalogue.MODELS))
        self.assertEqual(len(adapters), len(catalogue.MODELS))


class ApexEngine(unittest.TestCase):
    """The prompt and the dials, without a model to run them."""

    def test_the_prompt_ends_inside_an_open_reasoning_block(self):
        from desktop.apex import OPEN_THINK, Turn, prompt_for

        text = prompt_for("be helpful", [], "What is 2 + 2?")
        self.assertTrue(text.endswith("<|im_start|>assistant\n" + OPEN_THINK), repr(text[-60:]))
        self.assertIn("<|im_start|>system\nbe helpful<|im_end|>", text)
        self.assertIn("<|im_start|>user\nWhat is 2 + 2?<|im_end|>", text)

    def test_history_replays_answers_and_not_their_reasoning(self):
        from desktop.apex import Turn, prompt_for

        text = prompt_for("s", [Turn("user", "hello"),
                                Turn("assistant", "<think>\nlong private thoughts\n</think>\n\nhi")],
                          "again")
        self.assertIn("<|im_start|>assistant\nhi<|im_end|>", text)
        self.assertNotIn("long private thoughts", text)

    def test_the_dials_are_the_trained_ones(self):
        from nimbus2.controls import Controls, reasoning_words

        # Level 5 and temperature 5 are what the model was trained against and
        # what the benchmark measured; the app must not quietly send something else.
        controls = Controls(thinking_level=5, temperature=5)
        self.assertEqual(controls.sampling().temperature, 0.6)
        self.assertEqual(reasoning_words(5), 168)
        self.assertIn("Thinking level for this request: 5/20", controls.system_prompt())
        # The ceiling is 2.5x the target, in tokens, and Persian costs more of them.
        self.assertGreater(controls.think_token_limit("سوال به فارسی " * 5),
                           controls.think_token_limit("a question in English"))


class WorkingFolder(unittest.TestCase):
    """The one rule the terminal and every file write are held to."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()) / "work"
        (self.root / "src").mkdir(parents=True)
        (self.root / "src" / "main.py").write_text("x = 1", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.root.parent, ignore_errors=True)

    def test_a_path_inside_is_allowed(self):
        from desktop.app import inside

        self.assertEqual(inside(self.root, "src/main.py"), (self.root / "src" / "main.py").resolve())
        self.assertEqual(inside(self.root, ""), self.root.resolve())

    def test_a_path_that_climbs_out_is_refused(self):
        from desktop.app import inside

        # These come out of a model's answer or a person's typing, so each one
        # is refused rather than repaired.
        for path in ("..", "../secrets", "src/../../elsewhere", "src/../..",
                     str(Path(tempfile.gettempdir()) / "anywhere")):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                inside(self.root, path)


class Gate(unittest.TestCase):
    """Nothing runs until the person says so, and the socket is where they say it."""

    class Fake:
        """A Controller's shape, without starting a model server or an API."""

        def __init__(self, root: Path) -> None:
            self.root = root
            self.settings = {"permission": "ask"}
            self.allowed: set[str] = set()

        def folder(self):
            return self.root

        def needs_asking(self, key: str) -> bool:
            if self.settings.get("permission") != "session":
                return True
            return key not in self.allowed

        def allow(self, key: str, *, remember: bool = False) -> None:
            if remember:
                self.allowed.add(key)

    def socket_app(self, controller):
        """Just the shell route, wired the way desktop/server.py wires it."""
        from fastapi import FastAPI
        from starlette.concurrency import run_in_threadpool

        from desktop.terminal import Shell as ShellSession

        app = FastAPI()

        @app.websocket("/local/shell")
        async def shell(socket: WebSocket) -> None:
            await socket.accept()
            session = ShellSession(controller.folder())
            await socket.send_json({"folder": str(controller.folder())})
            try:
                while True:
                    message = await socket.receive_json()
                    command = str(message.get("command", "")).strip()
                    if not command:
                        continue
                    key = f"shell:{command}"
                    if not message.get("allowed") and controller.needs_asking(key):
                        await socket.send_json({"ask": command, "key": key})
                        continue
                    controller.allow(key, remember=bool(message.get("remember")))
                    await socket.send_json({"started": command})
                    queue = session.run(command)
                    while True:
                        item = await run_in_threadpool(queue.get)
                        if item is None:
                            break
                        await socket.send_json(item)
            except WebSocketDisconnect:
                session.stop()

        return app

    def test_a_command_is_asked_about_before_it_runs(self):
        from fastapi.testclient import TestClient

        root = Path(tempfile.mkdtemp())
        (root / "proof.txt").write_text("x", encoding="utf-8")
        controller = self.Fake(root)
        with TestClient(self.socket_app(controller)).websocket_connect("/local/shell") as socket:
            self.assertEqual(socket.receive_json()["folder"], str(root))
            listing = "dir /b" if os.name == "nt" else "ls"
            socket.send_json({"command": listing})
            asked = socket.receive_json()
            self.assertEqual(asked["ask"], listing)          # asked, and nothing run
            socket.send_json({"command": listing, "allowed": True})
            self.assertEqual(socket.receive_json()["started"], listing)
            seen = ""
            while True:
                message = socket.receive_json()
                if "code" in message:
                    break
                seen += str(message.get("line", ""))
            self.assertIn("proof.txt", seen)

    def test_session_mode_stops_asking_only_after_it_was_allowed(self):
        root = Path(tempfile.mkdtemp())
        controller = self.Fake(root)
        controller.settings["permission"] = "session"
        self.assertTrue(controller.needs_asking("shell:git status"))
        controller.allow("shell:git status", remember=True)
        self.assertFalse(controller.needs_asking("shell:git status"))
        # A different command is a different question, and switching back to
        # "ask" must make even an allowed one ask again.
        self.assertTrue(controller.needs_asking("shell:rm -rf ."))
        controller.settings["permission"] = "ask"
        self.assertTrue(controller.needs_asking("shell:git status"))


class Shell(unittest.TestCase):
    def test_a_command_runs_in_the_folder_it_was_given(self):
        import tempfile
        from desktop.terminal import Shell as ShellSession
        from desktop.terminal import drain

        folder = Path(tempfile.mkdtemp())
        (folder / "marker.txt").write_text("here", encoding="utf-8")
        session = ShellSession(folder)
        queue = session.run("dir /b" if os.name == "nt" else "ls")
        items, ended = drain(queue, timeout=20)
        self.assertTrue(ended)
        printed = " ".join(str(i.get("line", "")) for i in items)
        self.assertIn("marker.txt", printed)
        self.assertIn({"code": 0}, items)

    def test_an_empty_command_runs_nothing(self):
        import tempfile
        from desktop.terminal import Shell as ShellSession
        from desktop.terminal import drain

        session = ShellSession(Path(tempfile.mkdtemp()))
        items, ended = drain(session.run("   "), timeout=5)
        self.assertEqual((items, ended), ([], True))


if __name__ == "__main__":
    unittest.main()
