import sys
import tempfile
import json
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from wiki_tools import sources as sources_module
from wiki_tools.errors import WikiError
from wiki_tools.sources import add_source, get_manifest, source_tip


class SourceRegistryTests(unittest.TestCase):
    def make_root(self):
        temporary = tempfile.TemporaryDirectory(prefix="wiki-sources-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "wiki").mkdir()
        (root / "sources" / "raw").mkdir(parents=True)
        (root / "sources" / "manifests").mkdir(parents=True)
        return root

    def test_same_source_hash_reuses_revision_and_changed_bytes_preserve_old_snapshot(self):
        root = self.make_root()
        incoming = root / "incoming.md"
        incoming.write_text("First revision\n", encoding="utf-8")

        first = add_source(root, incoming, source_id="payroll-run", kind="procedure")
        repeated = add_source(root, incoming, source_id="payroll-run", kind="procedure")
        self.assertEqual(first["revision"], repeated["revision"])

        old_snapshot = root / first["local_path"]
        self.assertEqual(old_snapshot.read_text(encoding="utf-8"), "First revision\n")
        incoming.write_text("Second revision\n", encoding="utf-8")
        second = add_source(root, incoming, source_id="payroll-run", kind="procedure")

        self.assertNotEqual(first["revision"], second["revision"])
        self.assertEqual(second["supersedes"], first["revision"])
        self.assertEqual(old_snapshot.read_text(encoding="utf-8"), "First revision\n")
        self.assertEqual((root / second["local_path"]).read_text(encoding="utf-8"), "Second revision\n")

    def test_rejects_unsafe_source_ids_and_symlinked_managed_directories(self):
        root = self.make_root()
        incoming = root / "incoming.md"
        incoming.write_text("procedure\n", encoding="utf-8")
        for unsafe in ("../escape", "a/b", "Uppercase", "double--dash"):
            with self.subTest(source_id=unsafe):
                with self.assertRaises(WikiError) as caught:
                    add_source(root, incoming, source_id=unsafe, kind="procedure")
                self.assertEqual(caught.exception.code, "unsafe_source_id")

        outside = root / "outside-managed"
        outside.mkdir()
        (root / "sources" / "raw").rmdir()
        (root / "sources" / "raw").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(WikiError) as caught:
            add_source(root, incoming, source_id="safe-source", kind="procedure")
        self.assertEqual(caught.exception.code, "unsafe_path")
        self.assertEqual(list(outside.iterdir()), [])

    def test_concurrent_revisions_for_one_source_form_one_explicit_history(self):
        root = self.make_root()
        incoming_a = root / "first.md"
        incoming_b = root / "second.md"
        incoming_a.write_text("First revision\n", encoding="utf-8")
        incoming_b.write_text("Second revision\n", encoding="utf-8")
        barrier = threading.Barrier(3)
        failures = []

        def register(path):
            try:
                barrier.wait(5)
                add_source(root, path, source_id="ordered-source", kind="procedure")
            except BaseException as exc:
                failures.append(exc)

        workers = [threading.Thread(target=register, args=(path,)) for path in (incoming_a, incoming_b)]
        for worker in workers:
            worker.start()
        barrier.wait(5)
        for worker in workers:
            worker.join(5)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(failures, [])

        manifests = [json.loads(path.read_text(encoding="utf-8")) for path in (root / "sources" / "manifests").glob("*.json")]
        self.assertEqual(len(manifests), 2)
        self.assertEqual(sum(item["supersedes"] is None for item in manifests), 1)
        self.assertEqual(source_tip(manifests), next(item["revision"] for item in manifests if item["supersedes"]))

    def test_duplicate_registration_revalidates_existing_manifest_metadata(self):
        root = self.make_root()
        incoming = root / "incoming.md"
        incoming.write_text("Stable bytes\n", encoding="utf-8")
        registered = add_source(root, incoming, source_id="stable-source", kind="procedure")
        manifest_path = root / "sources" / "manifests" / f"stable-source--{registered['revision']}.json"
        record = json.loads(manifest_path.read_text(encoding="utf-8"))
        record["local_path"] = "sources/raw/../escaped.md"
        manifest_path.write_text(json.dumps(record), encoding="utf-8")

        with self.assertRaises(WikiError) as caught:
            add_source(root, incoming, source_id="stable-source", kind="procedure")
        self.assertEqual(caught.exception.code, "invalid_manifest")

    def test_reimporting_old_bytes_does_not_appear_to_restore_old_revision(self):
        root = self.make_root()
        incoming = root / "incoming.md"
        incoming.write_text("Revision A\n", encoding="utf-8")
        first = add_source(root, incoming, source_id="reverted-source", kind="procedure")
        incoming.write_text("Revision B\n", encoding="utf-8")
        second = add_source(root, incoming, source_id="reverted-source", kind="procedure")
        incoming.write_text("Revision A\n", encoding="utf-8")

        with self.assertRaises(WikiError) as caught:
            add_source(root, incoming, source_id="reverted-source", kind="procedure")
        self.assertEqual(caught.exception.code, "historical_revision")
        manifests = [json.loads(path.read_text(encoding="utf-8")) for path in (root / "sources" / "manifests").glob("*.json")]
        self.assertEqual(source_tip(manifests), second["revision"])
        self.assertEqual((root / first["local_path"]).read_text(encoding="utf-8"), "Revision A\n")

    def test_registers_unknown_format_but_lint_reports_its_validation_limit(self):
        from wiki_tools.knowledge import lint

        root = self.make_root()
        incoming = root / "export.bin"
        incoming.write_bytes(b"opaque format\x00")
        add_source(root, incoming, source_id="opaque-export", kind="confluence_export")

        report = lint(root)
        self.assertTrue(any(item["code"] == "unsupported_source_format" for item in report["warnings"]))

    def test_code_summary_is_explicitly_reported_as_derived_material(self):
        from wiki_tools.knowledge import lint

        root = self.make_root()
        incoming = root / "summary.md"
        incoming.write_text("The service writes an audit event.\n", encoding="utf-8")
        add_source(root, incoming, source_id="service-summary", kind="code_summary")

        report = lint(root)
        self.assertTrue(any(item["code"] == "derived_summary_not_source_code" for item in report["warnings"]))

    def test_lint_reports_a_symlinked_manifest_directory_without_following_it(self):
        from wiki_tools.knowledge import lint

        root = self.make_root()
        outside = root / "outside"
        outside.mkdir()
        (root / "sources" / "manifests").rmdir()
        (root / "sources" / "manifests").symlink_to(outside, target_is_directory=True)

        report = lint(root)
        self.assertIn("unsafe_path", {item["code"] for item in report["errors"]})

    def test_authorship_contract_and_legacy_manifest_normalize_in_memory_only(self):
        self.assertEqual(
            sources_module.SOURCE_AUTHORSHIPS,
            {"human-written", "ai-generated", "unknown"},
        )
        self.assertEqual(sources_module.source_authorship({}), "unknown")
        for invalid in ("machine-written", None, 4, [], {}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(WikiError) as caught:
                    sources_module.source_authorship({"authorship": invalid})
                self.assertEqual(caught.exception.code, "invalid_manifest")

        root = self.make_root()
        incoming = root / "legacy.md"
        incoming.write_text("Legacy source.\n", encoding="utf-8")
        registered = add_source(root, incoming, source_id="legacy-source", kind="unclassified")
        manifest_path = root / "sources" / "manifests" / f"legacy-source--{registered['revision']}.json"
        legacy = json.loads(manifest_path.read_text(encoding="utf-8"))
        legacy.pop("authorship", None)
        manifest_path.write_text(json.dumps(legacy), encoding="utf-8")
        original_manifest_bytes = manifest_path.read_bytes()

        loaded = get_manifest(root, "legacy-source", registered["revision"])

        self.assertEqual(loaded["authorship"], "unknown")
        self.assertEqual(sources_module.source_authorship(loaded), "unknown")
        self.assertEqual(manifest_path.read_bytes(), original_manifest_bytes)

    def test_current_identical_import_reuses_authorship_and_conflict_preserves_disk(self):
        root = self.make_root()
        incoming = root / "draft.md"
        incoming.write_text("Authorship stays with this exact revision.\n", encoding="utf-8")

        first = add_source(root, incoming, source_id="labeled-source", kind="unclassified", authorship="ai-generated")
        repeated_without_declaration = add_source(root, incoming, source_id="labeled-source", kind="unclassified")
        repeated_with_same_declaration = add_source(
            root, incoming, source_id="labeled-source", kind="unclassified", authorship="ai-generated"
        )
        self.assertEqual(first["authorship"], "ai-generated")
        self.assertEqual(repeated_without_declaration["authorship"], "ai-generated")
        self.assertEqual(repeated_with_same_declaration["authorship"], "ai-generated")

        manifest_path = root / "sources" / "manifests" / f"labeled-source--{first['revision']}.json"
        snapshot_path = root / first["local_path"]
        manifest_before = manifest_path.read_bytes()
        snapshot_before = snapshot_path.read_bytes()
        with self.assertRaises(WikiError) as caught:
            add_source(root, incoming, source_id="labeled-source", kind="unclassified", authorship="human-written")
        self.assertEqual(caught.exception.code, "source_metadata_conflict")
        self.assertEqual(manifest_path.read_bytes(), manifest_before)
        self.assertEqual(snapshot_path.read_bytes(), snapshot_before)
        self.assertEqual(len(list((root / "sources" / "manifests").glob("*.json"))), 1)

    def test_authorship_defaults_to_unknown_for_new_revisions_and_does_not_inherit(self):
        root = self.make_root()
        incoming = root / "source.md"
        incoming.write_text("First bytes.\n", encoding="utf-8")
        first = add_source(root, incoming, source_id="versioned-source", kind="unclassified", authorship="human-written")
        self.assertEqual(first["authorship"], "human-written")

        incoming.write_text("Changed bytes without a declaration.\n", encoding="utf-8")
        second = add_source(root, incoming, source_id="versioned-source", kind="unclassified")
        self.assertNotEqual(first["revision"], second["revision"])
        self.assertEqual(second["supersedes"], first["revision"])
        self.assertEqual(second["authorship"], "unknown")

        incoming.write_text("A later declared AI draft.\n", encoding="utf-8")
        third = add_source(root, incoming, source_id="versioned-source", kind="unclassified", authorship="ai-generated")
        self.assertEqual(third["authorship"], "ai-generated")

    def test_add_source_rejects_invalid_authorship_declarations(self):
        root = self.make_root()
        incoming = root / "source.md"
        incoming.write_text("A source.\n", encoding="utf-8")
        for invalid in ("generated", 4, [], {}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(WikiError) as caught:
                    add_source(root, incoming, source_id="source", kind="unclassified", authorship=invalid)
                self.assertEqual(caught.exception.code, "invalid_authorship")

        for invalid in ("generated", 4, [], {}):
            with self.subTest(default_authorship=invalid):
                with self.assertRaises(WikiError) as caught:
                    add_source(root, incoming, source_id="source", kind="unclassified", default_authorship=invalid)
                self.assertEqual(caught.exception.code, "invalid_authorship")


if __name__ == "__main__":
    unittest.main()
