import sys
import tempfile
import unittest
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from wiki_tools.knowledge import lint, search, validate_source_reference
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


if __name__ == "__main__":
    unittest.main()
