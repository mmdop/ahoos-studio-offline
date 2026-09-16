"""The offline studio's store, family and downloader.

    python -m unittest tests.test_desktop

The store stands in for Supabase under an unchanged app.js, so these check the
query shapes app.js actually sends -- and that the path a model's answer names
cannot reach outside the files folder.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

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

        self.assertEqual(catalogue.build(catalogue.DEFAULT_BUILD).key, "q4_k_m")
        for build in catalogue.BUILDS:
            self.assertRegex(build.sha256, r"^[0-9a-f]{64}$")
            self.assertTrue(build.url.startswith("https://huggingface.co/Qwen/"))


if __name__ == "__main__":
    unittest.main()
