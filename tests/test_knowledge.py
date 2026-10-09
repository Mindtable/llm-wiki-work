import sys
import tempfile
import unittest
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from wiki_tools.knowledge import (
    lint,
    project_inventory,
    search,
    validate_page_document,
    validate_source_reference,
    wiki_page_in_project,
)
from wiki_tools.errors import WikiError
from wiki_tools.sources import add_source


class KnowledgeTests(unittest.TestCase):
    def make_root(self):
        temporary = tempfile.TemporaryDirectory(prefix="wiki-knowledge-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "wiki" / "processes").mkdir(parents=True)
        (root / "wiki" / "concepts").mkdir(parents=True)
        (root / "sources" / "raw").mkdir(parents=True)
        (root / "sources" / "manifests").mkdir(parents=True)
        return root

    def set_authorship(self, root, source, authorship):
        manifest_path = root / "sources" / "manifests" / f"{source['source_id']}--{source['revision']}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if authorship is None:
            manifest.pop("authorship", None)
        else:
            manifest["authorship"] = authorship
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def set_project(self, root, source, project):
        manifest_path = root / "sources" / "manifests" / f"{source['source_id']}--{source['revision']}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        scope = manifest.setdefault("scope", {})
        if project is None:
            scope.pop("project", None)
        else:
            scope["project"] = project
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def register_source(self, root, source_id, content, project=None):
        incoming = root / f"{source_id}.md"
        incoming.write_text(content, encoding="utf-8")
        source = add_source(root, incoming, source_id=source_id, kind="unclassified")
        self.set_project(root, source, project)
        return source

    def page_metadata(self, page_id, *, project=None, source_refs=None, declared_scope=False):
        metadata = {
            "id": page_id,
            "title": page_id.replace("-", " ").title(),
            "kind": "process",
            "domain": "operations",
            "review_status": "draft",
            "source_refs": source_refs or [],
            "depends_on": [],
            "reviewed_at": None,
        }
        if project is not None or declared_scope:
            metadata["scope"] = {"project": project}
        return metadata

    def test_search_returns_matching_page_and_registered_source_with_locators(self):
        root = self.make_root()
        page = root / "wiki" / "processes" / "refund.md"
        metadata = {
            "id": "refund",
            "title": "Refund",
            "kind": "process",
            "domain": "billing",
            "review_status": "draft",
            "source_refs": [],
            "depends_on": [],
            "reviewed_at": None,
        }
        page.write_text(
            "---\n" + json.dumps(metadata) + "\n---\nRefund requests need an invoice.\n",
            encoding="utf-8",
        )
        incoming = root / "procedure.md"
        incoming.write_text("Refund review checks the original invoice.\n", encoding="utf-8")
        source = add_source(root, incoming, source_id="refund-procedure", kind="procedure")

        results = search(root, "invoice")
        self.assertEqual({row["type"] for row in results}, {"wiki_page", "source"})
        self.assertTrue(any(row["id"] == "refund" and row["path"] == "wiki/processes/refund.md" for row in results))
        self.assertTrue(any(row["id"] == "refund-procedure" and row["revision"] == source["revision"] for row in results))
        self.assertTrue(all(row["snippet"] for row in results))
        narrow = search(root, "original invoice")
        self.assertEqual([row["type"] for row in narrow], ["source"])

    def test_lint_detects_changed_source_bytes_unknown_revision_and_broken_local_link(self):
        root = self.make_root()
        incoming = root / "procedure.md"
        incoming.write_text("Refund follows the published procedure.\n", encoding="utf-8")
        registered = add_source(root, incoming, source_id="refund-procedure", kind="procedure")
        snapshot = root / registered["local_path"]
        snapshot.write_text("tampered\n", encoding="utf-8")

        page = root / "wiki" / "processes" / "refund.md"
        metadata = {
            "id": "refund",
            "title": "Refund",
            "kind": "process",
            "domain": "billing",
            "review_status": "draft",
            "source_refs": [{"source_id": "refund-procedure", "revision": registered["revision"]}],
            "depends_on": [],
            "reviewed_at": None,
        }
        page.write_text(
            "---\n" + json.dumps(metadata) + "\n---\n"
            "See [payment](../concepts/payment.md#payment), [second section](../concepts/payment.md#payment-1), "
            "and [missing](../missing.md).\n",
            encoding="utf-8",
        )
        (root / "wiki" / "concepts" / "payment.md").write_text("# Payment\n# Payment\n", encoding="utf-8")

        report = lint(root)
        error_codes = {item["code"] for item in report["errors"]}
        self.assertIn("source_hash_mismatch", error_codes)
        self.assertIn("broken_markdown_link", error_codes)
        self.assertNotIn("unsafe_markdown_link", error_codes)
        self.assertNotIn("unknown_source_ref", error_codes)

    def test_lint_rejects_a_page_reference_to_an_unregistered_revision(self):
        root = self.make_root()
        page = root / "wiki" / "processes" / "refund.md"
        metadata = {
            "id": "refund",
            "title": "Refund",
            "kind": "process",
            "domain": "billing",
            "review_status": "draft",
            "source_refs": [{"source_id": "missing", "revision": "deadbeef" * 8}],
            "depends_on": [],
            "reviewed_at": None,
        }
        page.write_text(
            "---\n" + json.dumps(metadata) + "\n---\n",
            encoding="utf-8",
        )
        report = lint(root)
        self.assertIn("unknown_source_ref", {item["code"] for item in report["errors"]})

    def test_lint_reports_malformed_nested_metadata_without_crashing(self):
        root = self.make_root()
        page = root / "wiki" / "processes" / "refund.md"
        metadata = {
            "id": "refund",
            "title": "Refund",
            "kind": [],
            "domain": "billing",
            "review_status": [],
            "source_refs": [{"source_id": []}],
            "depends_on": [{}],
            "reviewed_at": None,
        }
        page.write_text("---\n" + json.dumps(metadata) + "\n---\n", encoding="utf-8")

        report = lint(root)
        codes = {item["code"] for item in report["errors"]}
        self.assertIn("invalid_page_kind", codes)
        self.assertIn("invalid_review_status", codes)
        self.assertIn("invalid_source_refs", codes)
        self.assertIn("invalid_dependencies", codes)

    # Cyrillic headings and anchors are intentional fixtures for Unicode locator validation.
    def test_source_reference_validator_accepts_russian_section_and_wiki_anchors(self):
        root = self.make_root()
        incoming = root / "procedure.md"
        incoming.write_text("# Подтверждение оплаты\nСверить чек.\n", encoding="utf-8")
        source = add_source(root, incoming, source_id="payment-procedure", kind="procedure")
        page = root / "wiki" / "processes" / "refund.md"
        page.write_text("# Порядок возврата\n", encoding="utf-8")

        manifest, snapshot = validate_source_reference(
            root,
            "payment-procedure",
            source["revision"],
            "section:подтверждение-оплаты",
            "wiki/processes/refund.md#порядок-возврата",
        )
        self.assertEqual(manifest["source_id"], "payment-procedure")
        self.assertEqual(snapshot.read_text(encoding="utf-8").splitlines()[0], "# Подтверждение оплаты")
        with self.assertRaises(WikiError) as caught:
            validate_source_reference(root, "payment-procedure", source["revision"], "section:неизвестный-раздел")
        self.assertEqual(caught.exception.code, "invalid_source_locator")

    def test_search_uses_only_the_current_source_revision(self):
        root = self.make_root()
        incoming = root / "procedure.md"
        incoming.write_text("Old approval wording.\n", encoding="utf-8")
        add_source(root, incoming, source_id="approval-procedure", kind="procedure")
        incoming.write_text("Current authorization wording.\n", encoding="utf-8")
        current = add_source(root, incoming, source_id="approval-procedure", kind="procedure")

        self.assertEqual(search(root, "old approval"), [])
        result = search(root, "current authorization")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["revision"], current["revision"])

    def test_search_exposes_authorship_and_uses_it_only_to_break_score_ties(self):
        root = self.make_root()
        sources = [
            ("strong-ai", "Approval. Approval. Approval.", "ai-generated"),
            ("tie-ai", "Approval.", "ai-generated"),
            ("tie-human", "Approval.", "human-written"),
            ("tie-unknown", "Approval.", None),
        ]
        for source_id, content, authorship in sources:
            incoming = root / f"{source_id}.md"
            incoming.write_text(content + "\n", encoding="utf-8")
            source = add_source(root, incoming, source_id=source_id, kind="unclassified")
            self.set_authorship(root, source, authorship)

        page = root / "wiki" / "processes" / "approval.md"
        page.write_text("# Approval\nApproval.\n", encoding="utf-8")

        results = search(root, "approval")
        source_results = [item for item in results if item["type"] == "source"]

        self.assertEqual(
            [item["id"] for item in source_results],
            ["strong-ai", "tie-human", "tie-ai", "tie-unknown"],
        )
        self.assertEqual(
            {item["id"]: item["authorship"] for item in source_results},
            {
                "strong-ai": "ai-generated",
                "tie-human": "human-written",
                "tie-ai": "ai-generated",
                "tie-unknown": "unknown",
            },
        )
        page_result = next(item for item in results if item["type"] == "wiki_page")
        self.assertNotIn("authorship", page_result)

    def test_lint_accepts_legacy_authorship_as_unknown_and_rejects_invalid_metadata(self):
        root = self.make_root()
        incoming = root / "legacy.md"
        incoming.write_text("Legacy source content.\n", encoding="utf-8")
        source = add_source(root, incoming, source_id="legacy-source", kind="unclassified")
        self.set_authorship(root, source, None)

        result = search(root, "legacy source")
        self.assertEqual(result[0]["authorship"], "unknown")
        self.assertEqual(lint(root)["errors"], [])

        for invalid in ("machine-written", 42, ["ai-generated"]):
            with self.subTest(authorship=invalid):
                self.set_authorship(root, source, invalid)
                report = lint(root)
                self.assertIn("invalid_manifest", {item["code"] for item in report["errors"]})

    def test_project_search_filters_projects_and_prioritizes_scope_before_score(self):
        root = self.make_root()
        sources = [
            ("alpha-weak", "Transfer.\n", "alpha"),
            ("alpha-strong", "Transfer. Transfer. Transfer.\n", "alpha"),
            ("general-weak", "Transfer.\n", None),
            ("general-strong", "Transfer. Transfer. Transfer. Transfer.\n", None),
            ("beta-strong", "Transfer. Transfer. Transfer. Transfer. Transfer. Transfer.\n", "beta"),
        ]
        for source_id, content, project in sources:
            self.register_source(root, source_id, content, project)

        pages = [
            ("alpha-page", "alpha", "Transfer.\n"),
            ("general-page", None, "Transfer. Transfer. Transfer.\n"),
            ("beta-page", "beta", "Transfer. Transfer. Transfer. Transfer.\n"),
        ]
        for page_id, project, body in pages:
            page = root / "wiki" / "processes" / f"{page_id}.md"
            metadata = self.page_metadata(page_id, project=project)
            page.write_text("---\n" + json.dumps(metadata) + "\n---\n" + body, encoding="utf-8")
        (root / "wiki" / "index.md").write_text("# Transfer index\nTransfer. Transfer. Transfer.\n", encoding="utf-8")
        (root / "wiki" / "log.md").write_text("# Transfer log\nTransfer. Transfer. Transfer.\n", encoding="utf-8")

        scoped = search(root, "transfer", project="alpha")
        scoped_sources = [item for item in scoped if item["type"] == "source"]
        self.assertEqual(
            [item["id"] for item in scoped_sources],
            ["alpha-strong", "alpha-weak", "general-strong", "general-weak"],
        )
        scoped_paths = {item["path"] for item in scoped}
        self.assertIn("wiki/processes/alpha-page.md", scoped_paths)
        self.assertIn("wiki/processes/general-page.md", scoped_paths)
        self.assertNotIn("wiki/processes/beta-page.md", scoped_paths)
        self.assertNotIn("wiki/index.md", scoped_paths)
        self.assertNotIn("wiki/log.md", scoped_paths)
        self.assertTrue(all(item["project"] in {None, "alpha"} for item in scoped))

        unscoped = search(root, "transfer")
        unscoped_sources = [item for item in unscoped if item["type"] == "source"]
        projects = [item["project"] for item in unscoped_sources]
        first_projected = next(index for index, project in enumerate(projects) if project is not None)
        self.assertTrue(all(project is None for project in projects[:first_projected]))
        self.assertEqual(set(item["id"] for item in unscoped_sources), {source_id for source_id, _, _ in sources})
        self.assertTrue(any(item["path"] == "wiki/index.md" for item in unscoped))
        self.assertTrue(any(item["path"] == "wiki/log.md" for item in unscoped))

    def test_project_inventory_lists_eligible_history_and_scoped_pages(self):
        root = self.make_root()
        incoming = root / "alpha-rules.md"
        incoming.write_text("Old project rule.\n", encoding="utf-8")
        old = add_source(root, incoming, source_id="alpha-rules", kind="procedure")
        self.set_project(root, old, "alpha")
        incoming.write_text("Current project rule.\n", encoding="utf-8")
        current = add_source(root, incoming, source_id="alpha-rules", kind="procedure")
        self.set_project(root, current, "alpha")
        self.register_source(root, "general-rules", "Shared rule.\n")
        self.register_source(root, "beta-rules", "Other project rule.\n", "beta")

        for page_id, project in (("alpha-page", "alpha"), ("general-page", None), ("beta-page", "beta")):
            page = root / "wiki" / "processes" / f"{page_id}.md"
            metadata = self.page_metadata(page_id, project=project)
            page.write_text("---\n" + json.dumps(metadata) + "\n---\nPage contents.\n", encoding="utf-8")
        (root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
        (root / "wiki" / "log.md").write_text("# Log\n", encoding="utf-8")

        inventory = project_inventory(root, "alpha")

        source_rows = inventory["sources"]
        self.assertEqual({row["source_id"] for row in source_rows}, {"alpha-rules", "general-rules"})
        alpha_revisions = [row for row in source_rows if row["source_id"] == "alpha-rules"]
        self.assertEqual([row["revision"] for row in alpha_revisions], [current["revision"], old["revision"]])
        self.assertEqual([row["current"] for row in alpha_revisions], [True, False])
        self.assertTrue(all({"path", "manifest_path", "project", "authorship", "current"} <= row.keys() for row in source_rows))
        self.assertEqual(source_rows[-1]["project"], None)
        page_paths = {row["path"] for row in inventory["wiki_pages"]}
        self.assertEqual(page_paths, {"wiki/processes/alpha-page.md", "wiki/processes/general-page.md"})
        self.assertTrue(wiki_page_in_project(root, "wiki/processes/alpha-page.md", "alpha"))
        self.assertTrue(wiki_page_in_project(root, "wiki/processes/general-page.md", "alpha"))
        self.assertFalse(wiki_page_in_project(root, "wiki/processes/beta-page.md", "alpha"))
        self.assertFalse(wiki_page_in_project(root, "wiki/index.md", "alpha"))

        unscoped_inventory = project_inventory(root, None)
        unscoped_source_projects = [row["project"] for row in unscoped_inventory["sources"]]
        first_projected_source = next(index for index, value in enumerate(unscoped_source_projects) if value is not None)
        self.assertTrue(all(value is None for value in unscoped_source_projects[:first_projected_source]))
        unscoped_paths = {row["path"] for row in unscoped_inventory["wiki_pages"]}
        self.assertIn("wiki/index.md", unscoped_paths)
        self.assertIn("wiki/log.md", unscoped_paths)
        self.assertIn("wiki/processes/beta-page.md", unscoped_paths)

        with self.assertRaises(WikiError) as invalid:
            project_inventory(root, "../alpha")
        self.assertEqual(invalid.exception.code, "invalid_project")

    def test_scoped_inventory_and_search_ignore_corrupted_foreign_snapshots(self):
        root = self.make_root()
        atlas = self.register_source(root, "atlas-note", "Atlas shared term.\n", "atlas")
        foreign = self.register_source(root, "borealis-note", "Atlas shared term.\n", "borealis")
        (root / foreign["local_path"]).write_text("Corrupted foreign snapshot.\n", encoding="utf-8")

        inventory = project_inventory(root, "atlas")
        self.assertEqual([row["source_id"] for row in inventory["sources"]], ["atlas-note"])
        results = search(root, "Atlas shared", project="atlas")
        self.assertEqual([row["id"] for row in results if row["type"] == "source"], ["atlas-note"])
        self.assertEqual(inventory["sources"][0]["revision"], atlas["revision"])

    def test_page_scope_and_source_refs_must_be_compatible(self):
        root = self.make_root()
        alpha = self.register_source(root, "alpha-source", "Alpha source.\n", "alpha")
        general = self.register_source(root, "general-source", "Shared source.\n")
        beta = self.register_source(root, "beta-source", "Beta source.\n", "beta")
        valid_page = self.page_metadata(
            "alpha-page",
            project="alpha",
            source_refs=[
                {"source_id": "alpha-source", "revision": alpha["revision"]},
                {"source_id": "general-source", "revision": general["revision"]},
            ],
        )
        self.assertEqual(validate_page_document(root, "wiki/processes/alpha-page.md", "---\n" + json.dumps(valid_page) + "\n---\nPage.\n"), valid_page)

        empty_scope = self.page_metadata("empty-scope", declared_scope=True)
        self.assertIsNotNone(validate_page_document(root, "wiki/processes/empty-scope.md", "---\n" + json.dumps(empty_scope) + "\n---\nPage.\n"))
        extended_scope = self.page_metadata("extended-scope")
        extended_scope["scope"] = {"process": "returns", "project": None}
        self.assertIsNotNone(validate_page_document(root, "wiki/processes/extended-scope.md", "---\n" + json.dumps(extended_scope) + "\n---\nPage.\n"))

        foreign_ref_page = self.page_metadata(
            "alpha-foreign-ref",
            project="alpha",
            source_refs=[{"source_id": "beta-source", "revision": beta["revision"]}],
        )
        foreign_ref_path = root / "wiki" / "processes" / "alpha-foreign-ref.md"
        foreign_ref_path.write_text(
            "---\n" + json.dumps(foreign_ref_page) + "\n---\nAlpha unique term.\n",
            encoding="utf-8",
        )
        with self.assertRaises(WikiError) as foreign_ref:
            validate_page_document(root, "wiki/processes/alpha-foreign-ref.md", "---\n" + json.dumps(foreign_ref_page) + "\n---\nPage.\n")
        self.assertEqual(foreign_ref.exception.code, "invalid_page_metadata")
        self.assertFalse(wiki_page_in_project(root, "wiki/processes/alpha-foreign-ref.md", "alpha"))
        self.assertNotIn(
            "wiki/processes/alpha-foreign-ref.md",
            {item["path"] for item in search(root, "alpha unique term", project="alpha")},
        )
        self.assertNotIn(
            "wiki/processes/alpha-foreign-ref.md",
            {item["path"] for item in search(root, "alpha unique term")},
        )
        self.assertIn("invalid_source_refs", {item["code"] for item in lint(root)["errors"]})

        global_ref_page = self.page_metadata(
            "global-foreign-ref",
            source_refs=[{"source_id": "alpha-source", "revision": alpha["revision"]}],
        )
        with self.assertRaises(WikiError) as global_ref:
            validate_page_document(root, "wiki/processes/global-foreign-ref.md", "---\n" + json.dumps(global_ref_page) + "\n---\nPage.\n")
        self.assertEqual(global_ref.exception.code, "invalid_page_metadata")

        malformed = self.page_metadata("malformed-project", project="../alpha")
        with self.assertRaises(WikiError) as invalid_project:
            validate_page_document(root, "wiki/processes/malformed-project.md", "---\n" + json.dumps(malformed) + "\n---\nPage.\n")
        self.assertEqual(invalid_project.exception.code, "invalid_page_metadata")

    def test_lint_reports_invalid_project_in_source_manifest(self):
        root = self.make_root()
        source = self.register_source(root, "invalid-project-source", "Invalid project scope.\n")
        self.set_project(root, source, "../alpha")

        report = lint(root)

        self.assertIn("invalid_manifest", {item["code"] for item in report["errors"]})

    def test_malformed_frontmatter_is_not_treated_as_general_in_unscoped_retrieval(self):
        root = self.make_root()
        malformed = root / "wiki" / "processes" / "malformed.md"
        malformed.write_text("---\nnot JSON\n---\nShared transfer rule.\n", encoding="utf-8")

        self.assertNotIn("wiki/processes/malformed.md", {item["path"] for item in search(root, "transfer")})
        self.assertFalse(wiki_page_in_project(root, "wiki/processes/malformed.md", None))

        declared = root / "wiki" / "processes" / "invalid-project.md"
        metadata = self.page_metadata("invalid-project", project=[])
        declared.write_text("---\n" + json.dumps(metadata) + "\n---\nTransfer rule.\n", encoding="utf-8")
        with self.assertRaises(WikiError):
            search(root, "transfer")
        with self.assertRaises(WikiError):
            wiki_page_in_project(root, "wiki/processes/invalid-project.md", None)


if __name__ == "__main__":
    unittest.main()
