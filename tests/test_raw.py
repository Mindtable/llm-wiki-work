import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from wiki_tools.errors import WikiError
from wiki_tools.feedback import _connect
from wiki_tools.knowledge import lint, search
from wiki_tools.raw import discover_raw_sources
from wiki_tools import sources as sources_module
from wiki_tools.sources import get_manifest


class RawDropTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wiki-raw-drop-")
        self.root = Path(self.temp.name).resolve()
        (self.root / "wiki").mkdir()
        (self.root / "sources" / "raw").mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def jobs(self):
        connection = _connect(self.root)
        try:
            return connection.execute(
                "SELECT job_type, source_id, revision, status FROM jobs ORDER BY source_id, revision"
            ).fetchall()
        finally:
            connection.close()

    def test_search_discovers_raw_drop_and_registers_unclassified_snapshot(self):
        dropped = self.root / "sources" / "raw" / "Payroll policy.md"
        content = "# Payroll policy\nManagers approve expense reports.\n"
        dropped.write_bytes(content.encode("utf-8"))

        results = search(self.root, "Managers approve")

        self.assertEqual(len(results), 1)
        result = results[0]
        expected_id = "drop-" + hashlib.sha256(b"sources/raw/Payroll policy.md").hexdigest()
        expected_revision = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self.assertEqual(result["type"], "source")
        self.assertEqual(result["id"], expected_id)
        self.assertEqual(result["revision"], expected_revision)
        manifest = get_manifest(self.root, expected_id, expected_revision)
        self.assertEqual(manifest["kind"], "unclassified")
        self.assertEqual(manifest["origin"], "sources/raw/Payroll policy.md")
        self.assertEqual(dropped.read_bytes(), content.encode("utf-8"))
        self.assertEqual((self.root / manifest["local_path"]).read_bytes(), content.encode("utf-8"))
        self.assertEqual(len(self.jobs()), 1)

    def test_nested_same_basename_drops_get_distinct_stable_ids_and_repeat_is_idempotent(self):
        # Escaped Cyrillic folder names verify UTF-8 path hashing and same-basename separation.
        first = self.root / "sources" / "raw" / "\u043a\u043e\u043c\u0430\u043d\u0434\u0430 \u043e\u0434\u0438\u043d" / "notes.txt"
        second = self.root / "sources" / "raw" / "\u043a\u043e\u043c\u0430\u043d\u0434\u0430 \u0434\u0432\u0430" / "notes.txt"
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_text("first approval record\n", encoding="utf-8")
        second.write_text("second approval record\n", encoding="utf-8")

        search(self.root, "approval record")
        first_results = search(self.root, "first approval")
        second_results = search(self.root, "second approval")
        search(self.root, "approval record")

        expected_first = "drop-" + hashlib.sha256(first.relative_to(self.root).as_posix().encode("utf-8")).hexdigest()
        expected_second = "drop-" + hashlib.sha256(second.relative_to(self.root).as_posix().encode("utf-8")).hexdigest()
        self.assertEqual(first_results[0]["id"], expected_first)
        self.assertEqual(second_results[0]["id"], expected_second)
        self.assertNotEqual(first_results[0]["id"], second_results[0]["id"])
        self.assertEqual(len(self.jobs()), 2)
        self.assertEqual(len(list((self.root / "sources" / "manifests").glob("*.json"))), 2)

    def test_overwrite_of_a_drop_adds_a_revision_without_replacing_old_snapshot(self):
        dropped = self.root / "sources" / "raw" / "policy.txt"
        dropped.write_text("Old approval path.\n", encoding="utf-8")
        old = search(self.root, "Old approval")[0]
        old_snapshot = self.root / get_manifest(self.root, old["id"], old["revision"])["local_path"]

        dropped.write_text("New approval path.\n", encoding="utf-8")
        current = search(self.root, "New approval")[0]

        self.assertEqual(current["id"], old["id"])
        self.assertNotEqual(current["revision"], old["revision"])
        self.assertEqual(old_snapshot.read_text(encoding="utf-8"), "Old approval path.\n")
        self.assertEqual(search(self.root, "Old approval"), [])
        self.assertEqual(len(self.jobs()), 2)

    def test_lint_warns_about_unregistered_drop_without_importing_it(self):
        dropped = self.root / "sources" / "raw" / "pending.md"
        dropped.write_text("Waiting for a search.\n", encoding="utf-8")

        report = lint(self.root)

        pending = [item for item in report["warnings"] if item["code"] == "unregistered_raw_source"]
        self.assertEqual([item["path"] for item in pending], ["sources/raw/pending.md"])
        self.assertFalse((self.root / "sources" / "manifests").exists())
        self.assertFalse((self.root / ".state").exists())
        self.assertEqual(dropped.read_text(encoding="utf-8"), "Waiting for a search.\n")

    def test_scan_excludes_hidden_temporary_symlink_and_managed_snapshot_files(self):
        raw = self.root / "sources" / "raw"
        (raw / ".hidden.md").write_text("Hidden approval.\n", encoding="utf-8")
        (raw / "copy.md.tmp").write_text("Partial approval.\n", encoding="utf-8")
        (raw / "copy.md.part").write_text("Partial approval.\n", encoding="utf-8")
        outside = self.root / "outside.md"
        outside.write_text("Outside approval.\n", encoding="utf-8")
        try:
            (raw / "linked.md").symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        managed = raw / "manual-source"
        managed.mkdir()
        managed.joinpath("a" * 64 + ".md").write_text("Corrupted managed approval.\n", encoding="utf-8")

        self.assertEqual(search(self.root, "approval"), [])
        self.assertFalse((self.root / "sources" / "manifests").exists())
        self.assertFalse((self.root / ".state").exists())

    def test_discovery_excludes_only_the_reserved_human_written_confluence_tree(self):
        raw = self.root / "sources" / "raw"
        link_list = raw / "human-written" / "confluence" / "links.md"
        human_note = raw / "human-written" / "ordinary.md"
        other_bucket = raw / "other" / "confluence" / "ordinary.md"
        for path in (link_list, human_note, other_bucket):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("https://example.atlassian.net/wiki/spaces/OPS/pages/123/Title\n", encoding="utf-8")

        manifests = discover_raw_sources(self.root)

        self.assertEqual(
            {item["origin"] for item in manifests},
            {
                "sources/raw/human-written/ordinary.md",
                "sources/raw/other/confluence/ordinary.md",
            },
        )
        self.assertTrue(link_list.is_file())

    def test_scanner_reports_historical_revision_reappearance(self):
        dropped = self.root / "sources" / "raw" / "policy.md"
        dropped.write_text("Version A approval.\n", encoding="utf-8")
        search(self.root, "Version A")
        dropped.write_text("Version B approval.\n", encoding="utf-8")
        search(self.root, "Version B")
        dropped.write_text("Version A approval.\n", encoding="utf-8")

        with self.assertRaises(WikiError) as caught:
            search(self.root, "Version A")

        self.assertEqual(caught.exception.code, "historical_revision")
        self.assertEqual(len(self.jobs()), 2)

    def test_raw_drop_authorship_uses_only_the_first_bucket_directory(self):
        raw = self.root / "sources" / "raw"
        ai = raw / "ai-generated" / "project" / "human-written" / "draft.md"
        human = raw / "human-written" / "project" / "ai-generated" / "approved.md"
        unknown_nested = raw / "other" / "ai-generated" / "notes.md"
        unknown_root = raw / "direct.md"
        for path, content in (
            (ai, "AI working draft.\n"),
            (human, "Human-reviewed note.\n"),
            (unknown_nested, "Unclassified note.\n"),
            (unknown_root, "Root-level drop.\n"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        manifests = discover_raw_sources(self.root)
        authorship_by_origin = {manifest["origin"]: sources_module.source_authorship(manifest) for manifest in manifests}

        self.assertEqual(authorship_by_origin[ai.relative_to(self.root).as_posix()], "ai-generated")
        self.assertEqual(authorship_by_origin[human.relative_to(self.root).as_posix()], "human-written")
        self.assertEqual(authorship_by_origin[unknown_nested.relative_to(self.root).as_posix()], "unknown")
        self.assertEqual(authorship_by_origin[unknown_root.relative_to(self.root).as_posix()], "unknown")
        self.assertTrue(all(manifest["kind"] == "unclassified" for manifest in manifests))
        for path in (ai, human, unknown_nested, unknown_root):
            self.assertTrue(path.is_file())

    def test_legacy_manifest_in_bucket_stays_unknown_without_rewrite_and_changed_bytes_use_bucket(self):
        dropped = self.root / "sources" / "raw" / "ai-generated" / "old" / "policy.md"
        dropped.parent.mkdir(parents=True)
        dropped.write_text("Original bytes.\n", encoding="utf-8")
        first = discover_raw_sources(self.root)[0]
        manifest_path = self.root / "sources" / "manifests" / f"{first['source_id']}--{first['revision']}.json"
        legacy = json.loads(manifest_path.read_text(encoding="utf-8"))
        legacy.pop("authorship")
        manifest_path.write_text(json.dumps(legacy), encoding="utf-8")
        legacy_manifest_bytes = manifest_path.read_bytes()

        repeated = discover_raw_sources(self.root)[0]

        self.assertEqual(sources_module.source_authorship(repeated), "unknown")
        self.assertEqual(manifest_path.read_bytes(), legacy_manifest_bytes)
        self.assertEqual(len(self.jobs()), 1)

        dropped.write_text("Changed bytes.\n", encoding="utf-8")
        changed = discover_raw_sources(self.root)[0]
        self.assertNotEqual(changed["revision"], first["revision"])
        self.assertEqual(changed["supersedes"], first["revision"])
        self.assertEqual(sources_module.source_authorship(changed), "ai-generated")
        self.assertEqual(len(self.jobs()), 2)

    def test_raw_project_layout_is_reserved_and_project_is_stable_across_revisions(self):
        raw = self.root / "sources" / "raw"
        project_drop = raw / "ai-generated" / "projects" / "atlas" / "idea.md"
        global_project_drop = raw / "projects" / "atlas" / "notes.md"
        other_layout = raw / "other" / "projects" / "atlas" / "unassigned.md"
        nested_non_layout = raw / "ai-generated" / "ideas" / "projects" / "borealis" / "nested.md"
        for path, content in (
            (project_drop, "Project idea.\n"),
            (global_project_drop, "General project evidence.\n"),
            (other_layout, "Do not infer from nested directory names.\n"),
            (nested_non_layout, "Do not infer nested project folder.\n"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        manifests = discover_raw_sources(self.root)
        by_origin = {manifest["origin"]: manifest for manifest in manifests}

        self.assertEqual(by_origin[project_drop.relative_to(self.root).as_posix()]["scope"]["project"], "atlas")
        self.assertEqual(by_origin[global_project_drop.relative_to(self.root).as_posix()]["scope"]["project"], "atlas")
        self.assertEqual(by_origin[other_layout.relative_to(self.root).as_posix()]["scope"]["project"], None)
        self.assertEqual(by_origin[nested_non_layout.relative_to(self.root).as_posix()]["scope"]["project"], None)
        self.assertEqual(sources_module.source_authorship(by_origin[project_drop.relative_to(self.root).as_posix()]), "ai-generated")
        self.assertEqual(sources_module.source_authorship(by_origin[global_project_drop.relative_to(self.root).as_posix()]), "unknown")

        project_manifest = by_origin[project_drop.relative_to(self.root).as_posix()]
        repeated = discover_raw_sources(self.root)
        repeated_project = next(item for item in repeated if item["origin"] == project_manifest["origin"])
        self.assertEqual(repeated_project["revision"], project_manifest["revision"])
        self.assertEqual(repeated_project["scope"]["project"], "atlas")

        project_drop.write_text("Revised project idea.\n", encoding="utf-8")
        revised = discover_raw_sources(self.root)
        revised_project = next(item for item in revised if item["origin"] == project_manifest["origin"])
        self.assertNotEqual(revised_project["revision"], project_manifest["revision"])
        self.assertEqual(revised_project["scope"]["project"], "atlas")

    def test_explicit_projects_layout_rejects_missing_or_invalid_slug(self):
        raw = self.root / "sources" / "raw"
        missing = raw / "projects" / "not-a-project.md"
        invalid = raw / "human-written" / "projects" / "Bad Project" / "notes.md"
        missing.parent.mkdir(parents=True)
        invalid.parent.mkdir(parents=True)
        missing.write_text("Missing slug directory.\n", encoding="utf-8")
        invalid.write_text("Invalid project slug.\n", encoding="utf-8")

        with self.assertRaises(WikiError) as caught:
            discover_raw_sources(self.root)

        self.assertEqual(caught.exception.code, "invalid_project")

if __name__ == "__main__":
    unittest.main()
