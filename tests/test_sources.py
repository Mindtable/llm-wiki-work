import sys
import tempfile
import hashlib
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

    def test_project_ids_are_lowercase_ascii_slugs_and_project_matches_include_general(self):
        self.assertIsNone(sources_module.validate_project(None))
        self.assertEqual(sources_module.validate_project("atlas"), "atlas")
        self.assertEqual(sources_module.validate_project("team-2"), "team-2")
        for invalid in ("", "Atlas", "two words", "a/b", 3, [], {}):
            with self.subTest(project=invalid):
                with self.assertRaises(WikiError) as caught:
                    sources_module.validate_project(invalid)
                self.assertEqual(caught.exception.code, "invalid_project")

        self.assertTrue(sources_module.project_matches("atlas", None))
        self.assertTrue(sources_module.project_matches(None, "atlas"))
        self.assertTrue(sources_module.project_matches("atlas", "atlas"))
        self.assertFalse(sources_module.project_matches("atlas", "borealis"))

    def test_source_project_validates_manifest_scope_and_legacy_scope_fields(self):
        self.assertIsNone(sources_module.source_project({}))
        self.assertIsNone(sources_module.source_project({"scope": {}}))
        self.assertEqual(
            sources_module.source_project({"scope": {"project": "atlas", "region": "eu"}}),
            "atlas",
        )
        for scope in (None, "atlas", []):
            with self.subTest(scope=scope):
                with self.assertRaises(WikiError) as caught:
                    sources_module.source_project({"scope": scope})
                self.assertEqual(caught.exception.code, "invalid_manifest")
        for project in ("Atlas", "a/b", 8, [], {}):
            with self.subTest(project=project):
                with self.assertRaises(WikiError) as caught:
                    sources_module.source_project({"scope": {"project": project}})
                self.assertEqual(caught.exception.code, "invalid_manifest")

        root = self.make_root()
        incoming = root / "legacy.md"
        incoming.write_text("Legacy source.\n", encoding="utf-8")
        registered = add_source(root, incoming, source_id="legacy-project", kind="unclassified")
        manifest_path = root / "sources" / "manifests" / f"legacy-project--{registered['revision']}.json"
        legacy = json.loads(manifest_path.read_text(encoding="utf-8"))
        legacy["scope"] = {"region": "eu"}
        manifest_path.write_text(json.dumps(legacy), encoding="utf-8")
        original = manifest_path.read_bytes()

        loaded = get_manifest(root, "legacy-project", registered["revision"])

        self.assertEqual(loaded["scope"], {"region": "eu", "project": None})
        self.assertEqual(sources_module.source_project(loaded), None)
        self.assertEqual(manifest_path.read_bytes(), original)

    def test_project_persists_per_revision_inherits_on_content_change_and_conflicts_are_immutable(self):
        root = self.make_root()
        incoming = root / "project.md"
        incoming.write_text("Original idea.\n", encoding="utf-8")
        first = add_source(root, incoming, source_id="project-idea", kind="unclassified", project="atlas")
        self.assertEqual(first["scope"]["project"], "atlas")

        repeated = add_source(root, incoming, source_id="project-idea", kind="unclassified")
        self.assertEqual(repeated["scope"]["project"], "atlas")
        repeated_with_same_project = add_source(
            root, incoming, source_id="project-idea", kind="unclassified", project="atlas"
        )
        self.assertEqual(repeated_with_same_project["scope"]["project"], "atlas")
        manifest_path = root / "sources" / "manifests" / f"project-idea--{first['revision']}.json"
        snapshot_path = root / first["local_path"]
        manifest_before = manifest_path.read_bytes()
        snapshot_before = snapshot_path.read_bytes()
        with self.assertRaises(WikiError) as caught:
            add_source(root, incoming, source_id="project-idea", kind="unclassified", project="borealis")
        self.assertEqual(caught.exception.code, "source_metadata_conflict")
        self.assertEqual(manifest_path.read_bytes(), manifest_before)
        self.assertEqual(snapshot_path.read_bytes(), snapshot_before)

        incoming.write_text("Revised idea.\n", encoding="utf-8")
        inherited = add_source(root, incoming, source_id="project-idea", kind="unclassified")
        self.assertNotEqual(inherited["revision"], first["revision"])
        self.assertEqual(inherited["supersedes"], first["revision"])
        self.assertEqual(inherited["scope"]["project"], "atlas")

        incoming.write_text("Raw bucket declaration.\n", encoding="utf-8")
        bucket_default = add_source(
            root,
            incoming,
            source_id="project-idea",
            kind="unclassified",
            default_project="borealis",
        )
        self.assertEqual(bucket_default["scope"]["project"], "borealis")

        incoming.write_text("New global material.\n", encoding="utf-8")
        new_id = add_source(root, incoming, source_id="global-note", kind="unclassified")
        self.assertIsNone(new_id["scope"]["project"])

    def test_project_and_default_project_reject_invalid_values(self):
        root = self.make_root()
        incoming = root / "source.md"
        incoming.write_text("Source.\n", encoding="utf-8")
        for argument in ("project", "default_project"):
            for invalid in ("Atlas", "two words", 2, [], {}):
                with self.subTest(argument=argument, project=invalid):
                    with self.assertRaises(WikiError) as caught:
                        add_source(
                            root,
                            incoming,
                            source_id="source",
                            kind="unclassified",
                            **{argument: invalid},
                        )
                    self.assertEqual(caught.exception.code, "invalid_project")

    def test_invalid_previous_project_metadata_does_not_leave_new_snapshot(self):
        root = self.make_root()
        incoming = root / "source.md"
        incoming.write_text("Original project record.\n", encoding="utf-8")
        first = add_source(root, incoming, source_id="project-record", kind="unclassified", project="atlas")
        manifest_path = root / "sources" / "manifests" / f"project-record--{first['revision']}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["scope"]["project"] = "Atlas"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        incoming.write_text("Changed content with invalid previous scope.\n", encoding="utf-8")
        next_revision = hashlib.sha256(incoming.read_bytes()).hexdigest()
        next_snapshot = root / "sources" / "raw" / "project-record" / f"{next_revision}.md"
        next_manifest = root / "sources" / "manifests" / f"project-record--{next_revision}.json"
        with self.assertRaises(WikiError) as caught:
            add_source(root, incoming, source_id="project-record", kind="unclassified")

        self.assertEqual(caught.exception.code, "invalid_manifest")
        self.assertFalse(next_snapshot.exists())
        self.assertFalse(next_manifest.exists())


if __name__ == "__main__":
    unittest.main()
