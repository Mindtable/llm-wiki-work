import json
import re
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from wiki_tools.errors import WikiError
from wiki_tools.feedback import _connect
from wiki_tools.review import render_review, review_proposal
from wiki_tools.sources import add_source


class ProposalReviewTests(unittest.TestCase):
    def make_root(self):
        temporary = tempfile.TemporaryDirectory(prefix="wiki-review-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        (root / "wiki" / "processes").mkdir(parents=True)
        (root / "sources" / "raw").mkdir(parents=True)
        (root / "sources" / "manifests").mkdir(parents=True)
        return root

    def register_source(self, root, source_id="review-evidence", *, project=None, content="Verified approval source.\n"):
        source_path = root / f"{source_id}.md"
        source_path.write_text(content, encoding="utf-8")
        return add_source(root, source_path, source_id=source_id, kind="unclassified", project=project)

    def page_content(self, page_id, source, body, *, project=None, source_refs=None):
        metadata = {
            "id": page_id,
            "title": page_id.replace("-", " ").title(),
            "kind": "process",
            "domain": "review-tests",
            "review_status": "draft",
            "source_refs": source_refs if source_refs is not None else [{"source_id": source["source_id"], "revision": source["revision"]}],
            "depends_on": [],
            "reviewed_at": None,
        }
        if project is not None:
            metadata["scope"] = {"project": project}
        return "---\n" + json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n---\n" + body

    def citation(self, source):
        return {"source_id": source["source_id"], "revision": source["revision"], "locator": "line:1"}

    def proposal(self, changes=None, *, outcome="proposed", evidence=None, summary="A reviewable proposal."):
        return {
            "outcome": outcome,
            "summary": summary,
            "changes": list(changes or []),
            "evidence": list(evidence or []),
        }

    def create_job(
        self,
        root,
        source,
        proposal,
        *,
        status="ready_for_review",
        created_at="2026-01-01T00:00:00Z",
        job_type="ingest",
    ):
        job_id = uuid.uuid4().hex
        if job_type == "ingest":
            payload = {"source_id": source["source_id"], "revision": source["revision"]}
            source_id, revision, feedback_id = source["source_id"], source["revision"], None
        else:
            payload = {"answer_id": uuid.uuid4().hex}
            source_id, revision, feedback_id = None, None, uuid.uuid4().hex
        proposal_dir = root / ".state" / "proposals"
        proposal_dir.mkdir(parents=True, exist_ok=True)
        proposal_path = proposal_dir / f"{job_id}.json"
        proposal_path.write_text(json.dumps(proposal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        connection = _connect(root)
        try:
            connection.execute(
                "INSERT INTO jobs (job_id, job_type, status, payload, feedback_id, source_id, revision, proposal_path, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    job_type,
                    status,
                    json.dumps(payload),
                    feedback_id,
                    source_id,
                    revision,
                    f".state/proposals/{job_id}.json",
                    created_at,
                    created_at,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        return job_id, proposal_path

    def insert_unrelated_ready_job(self, root, source, *, created_at="2026-01-02T00:00:00Z"):
        job_id = uuid.uuid4().hex
        connection = _connect(root)
        try:
            connection.execute(
                "INSERT INTO jobs (job_id, job_type, status, payload, source_id, revision, proposal_path, created_at, updated_at) "
                "VALUES (?, 'ingest', 'ready_for_review', ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    json.dumps({"source_id": source["source_id"], "revision": source["revision"]}),
                    source["source_id"],
                    source["revision"],
                    f".state/proposals/{job_id}.json",
                    created_at,
                    created_at,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        return job_id

    def test_no_database_or_ready_jobs_returns_empty_without_creating_state(self):
        root = self.make_root()

        report = review_proposal(root)

        self.assertEqual(report, {"status": "no_ready_proposals", "job_id": None, "ready_job_ids": []})
        self.assertFalse((root / ".state").exists())

        source = self.register_source(root)
        pending, _ = self.create_job(root, source, self.proposal(outcome="needs_evidence"), status="pending")
        resolved, _ = self.create_job(
            root, source, self.proposal(outcome="needs_evidence"), status="resolved", job_type="feedback"
        )
        empty = review_proposal(root)
        self.assertEqual(empty, {"status": "no_ready_proposals", "job_id": None, "ready_job_ids": []})
        self.assertNotEqual(pending, resolved)

    def test_default_selects_oldest_ready_job_and_targeted_selection_checks_status(self):
        root = self.make_root()
        source = self.register_source(root)
        old, _ = self.create_job(
            root,
            source,
            self.proposal(outcome="needs_evidence"),
            created_at="2026-01-01T00:00:00Z",
        )
        new, _ = self.create_job(
            root,
            source,
            self.proposal(outcome="needs_evidence"),
            created_at="2026-01-02T00:00:00Z",
            job_type="feedback",
        )
        pending, _ = self.create_job(
            root, source, self.proposal(outcome="needs_evidence"), status="pending", job_type="feedback"
        )

        report = review_proposal(root)

        self.assertEqual(report["job_id"], old)
        self.assertEqual(report["ready_job_ids"], [old, new])
        self.assertEqual(report["outcome"], "needs_evidence")
        self.assertEqual(review_proposal(root, job_id=new)["job_id"], new)
        with self.assertRaises(WikiError) as not_ready:
            review_proposal(root, job_id=pending)
        self.assertEqual(not_ready.exception.code, "invalid_state")
        with self.assertRaises(WikiError) as missing:
            review_proposal(root, job_id="f" * 32)
        self.assertEqual(missing.exception.code, "job_not_found")
        with self.assertRaises(WikiError) as malformed:
            review_proposal(root, job_id="not-a-job")
        self.assertEqual(malformed.exception.code, "invalid_state")

    def test_report_contains_create_update_unchanged_diffs_and_leaves_everything_read_only(self):
        root = self.make_root()
        source = self.register_source(root, project="atlas")
        citation = self.citation(source)
        update_path = root / "wiki" / "processes" / "update.md"
        same_path = root / "wiki" / "processes" / "same.md"
        update_current = self.page_content("update", source, "Old procedure text.\n", project="atlas")
        update_proposed = self.page_content("update", source, "Revised procedure text.\n", project="atlas")
        same_content = self.page_content("same", source, "No textual change.\n", project="atlas")
        update_path.write_text(update_current, encoding="utf-8")
        same_path.write_text(same_content, encoding="utf-8")
        index_path = root / "wiki" / "index.md"
        index_path.write_text("# Old index\n", encoding="utf-8")
        index_proposed = "# New navigation\n\nIndex links across projects.\n"
        create_content = self.page_content("create", source, "New page body.\n", project="atlas")
        proposal = self.proposal(
            [
                {"path": "wiki/processes/create.md", "content": create_content},
                {"path": "wiki/processes/update.md", "content": update_proposed},
                {"path": "wiki/processes/same.md", "content": same_content},
                {"path": "wiki/index.md", "content": index_proposed},
            ],
            evidence=[citation],
            summary="Add a scoped process note.",
        )
        job_id, proposal_path = self.create_job(root, source, proposal)
        original_proposal = proposal_path.read_bytes()
        original_update = update_path.read_bytes()
        original_same = same_path.read_bytes()
        original_index = index_path.read_bytes()

        report = review_proposal(root, job_id=job_id)

        self.assertEqual(report["status"], "ready_for_review")
        self.assertEqual(report["job_id"], job_id)
        self.assertEqual(report["job_type"], "ingest")
        self.assertEqual(report["outcome"], "proposed")
        self.assertEqual(report["project"], "atlas")
        self.assertTrue(report["project_bound"])
        self.assertEqual([change["action"] for change in report["changes"]], ["create", "update", "unchanged", "update"])
        self.assertIsNone(report["changes"][0]["current_sha256"])
        self.assertIn("+New page body.", report["changes"][0]["diff"])
        self.assertIn("-Old procedure text.", report["changes"][1]["diff"])
        self.assertIn("+Revised procedure text.", report["changes"][1]["diff"])
        self.assertEqual(report["changes"][2]["diff"], "")
        self.assertEqual(report["changes"][2]["current_sha256"], report["changes"][2]["proposed_sha256"])
        self.assertEqual(report["changes"][1]["project"], "atlas")
        self.assertFalse(report["changes"][1]["cross_project_navigation"])
        self.assertIsNone(report["changes"][3]["project"])
        self.assertTrue(report["changes"][3]["cross_project_navigation"])
        self.assertEqual(report["evidence"][0]["source_id"], source["source_id"])
        self.assertEqual(report["evidence"][0]["revision"], source["revision"])
        self.assertEqual(report["evidence"][0]["locator"], "line:1")
        self.assertEqual(report["evidence"][0]["project"], "atlas")
        self.assertEqual(report["evidence"][0]["authorship"], "unknown")
        self.assertEqual(report["evidence"][0]["source_path"], source["local_path"])
        self.assertEqual(len(report["review_fingerprint"]), 64)

        connection = _connect(root)
        try:
            row = connection.execute("SELECT status, proposal_path FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        finally:
            connection.close()
        self.assertEqual((row["status"], row["proposal_path"]), ("ready_for_review", f".state/proposals/{job_id}.json"))
        self.assertEqual(proposal_path.read_bytes(), original_proposal)
        self.assertEqual(update_path.read_bytes(), original_update)
        self.assertEqual(same_path.read_bytes(), original_same)
        self.assertEqual(index_path.read_bytes(), original_index)
        self.assertFalse((root / "wiki" / "processes" / "create.md").exists())

    def test_review_fingerprint_tracks_only_selected_proposal_evidence_and_targets(self):
        root = self.make_root()
        source = self.register_source(root, source_id="fingerprint-source", content="Evidence line.\n")
        target = root / "wiki" / "processes" / "target.md"
        old_page = self.page_content("target", source, "Old target body.\n")
        proposed_page = self.page_content("target", source, "New target body.\n")
        target.write_text(old_page, encoding="utf-8")
        proposal = self.proposal(
            [{"path": "wiki/processes/target.md", "content": proposed_page}],
            evidence=[self.citation(source)],
            summary="Fingerprint this proposal.",
        )
        job_id, proposal_path = self.create_job(root, source, proposal)

        first = review_proposal(root, job_id)
        same = review_proposal(root, job_id)
        self.assertEqual(first["review_fingerprint"], same["review_fingerprint"])

        unrelated = root / "wiki" / "concepts" / "unrelated.md"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("Unrelated wiki content.\n", encoding="utf-8")
        other_ready, _ = self.create_job(
            root,
            source,
            self.proposal(outcome="needs_evidence"),
            created_at="2026-01-02T00:00:00Z",
            job_type="feedback",
        )
        unrelated_report = review_proposal(root, job_id)
        self.assertIn(other_ready, unrelated_report["ready_job_ids"])
        self.assertEqual(first["review_fingerprint"], unrelated_report["review_fingerprint"])

        target.write_text(self.page_content("target", source, "Target changed after review.\n"), encoding="utf-8")
        changed_target = review_proposal(root, job_id)
        self.assertNotEqual(unrelated_report["review_fingerprint"], changed_target["review_fingerprint"])

        proposal["summary"] = "The reviewer rationale changed."
        proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
        changed_proposal = review_proposal(root, job_id)
        self.assertNotEqual(changed_target["review_fingerprint"], changed_proposal["review_fingerprint"])

        source_manifest_path = root / "sources" / "manifests" / f"{source['source_id']}--{source['revision']}.json"
        source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
        source_manifest["authorship"] = "ai-generated"
        source_manifest_path.write_text(json.dumps(source_manifest), encoding="utf-8")
        changed_evidence = review_proposal(root, job_id)
        self.assertNotEqual(changed_proposal["review_fingerprint"], changed_evidence["review_fingerprint"])

        connection = _connect(root)
        try:
            payload_row = connection.execute("SELECT payload FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            selected_payload = json.loads(payload_row["payload"])
            selected_payload["context_note"] = "The selected job changed."
            connection.execute("UPDATE jobs SET payload = ? WHERE job_id = ?", (json.dumps(selected_payload), job_id))
            connection.commit()
        finally:
            connection.close()
        changed_job = review_proposal(root, job_id)
        self.assertNotEqual(changed_evidence["review_fingerprint"], changed_job["review_fingerprint"])

    def test_non_proposed_outcome_has_no_diffs_and_renders_manual_choices(self):
        root = self.make_root()
        source = self.register_source(root)
        job_id, _ = self.create_job(
            root,
            source,
            self.proposal(outcome="needs_evidence", changes=[], evidence=[], summary="Need a better source."),
        )

        report = review_proposal(root, job_id)
        rendered = render_review(report)

        self.assertEqual(report["outcome"], "needs_evidence")
        self.assertEqual(report["changes"], [])
        self.assertEqual(report["project"], None)
        self.assertTrue(report["project_bound"])
        self.assertIn("No wiki changes are proposed", rendered)
        self.assertIn("general sources", rendered)
        self.assertIn("Accept", rendered)
        self.assertIn("Revise", rendered)
        self.assertIn("Defer", rendered)

    def test_unscoped_feedback_review_is_displayed_separately_from_bound_general_scope(self):
        root = self.make_root()
        job_id, _ = self.create_job(
            root,
            None,
            self.proposal(outcome="needs_evidence", changes=[], evidence=[], summary="Need evidence."),
            job_type="feedback",
        )

        report = review_proposal(root, job_id)
        rendered = render_review(report)

        self.assertEqual(report["job_type"], "feedback")
        self.assertIsNone(report["project"])
        self.assertFalse(report["project_bound"])
        self.assertIn("Scope: unbound", rendered)

    def test_corrupt_or_symlinked_proposal_and_unsafe_change_path_are_rejected(self):
        root = self.make_root()
        source = self.register_source(root)
        unsafe_proposal = self.proposal(
            [{"path": "wiki/../outside.md", "content": "not safe"}],
            evidence=[self.citation(source)],
        )
        unsafe_id, _ = self.create_job(root, source, unsafe_proposal)
        with self.assertRaises(WikiError) as unsafe:
            review_proposal(root, unsafe_id)
        self.assertEqual(unsafe.exception.code, "invalid_proposal")

        corrupt_id, corrupt_path = self.create_job(
            root, source, self.proposal(outcome="needs_evidence"), job_type="feedback"
        )
        corrupt_path.write_text("{ this is not JSON", encoding="utf-8")
        with self.assertRaises(WikiError) as corrupt:
            review_proposal(root, corrupt_id)
        self.assertEqual(corrupt.exception.code, "invalid_proposal")

        symlink_id, symlink_path = self.create_job(
            root, source, self.proposal(outcome="needs_evidence"), job_type="feedback"
        )
        outside = root.parent / "outside-review-proposal.json"
        outside.write_text(json.dumps(self.proposal(outcome="needs_evidence")), encoding="utf-8")
        symlink_path.unlink()
        try:
            symlink_path.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaises(WikiError) as symlink:
            review_proposal(root, symlink_id)
        self.assertEqual(symlink.exception.code, "unsafe_path")

    def test_project_bound_proposal_with_mismatched_page_scope_is_rejected(self):
        root = self.make_root()
        source = self.register_source(root, source_id="atlas-evidence", project="atlas")
        mismatch = self.page_content(
            "wrong-project", source, "Wrong scope.\n", project="borealis", source_refs=[]
        )
        proposal = self.proposal(
            [{"path": "wiki/processes/wrong-project.md", "content": mismatch}],
            evidence=[self.citation(source)],
        )
        job_id, _ = self.create_job(root, source, proposal)

        with self.assertRaises(WikiError) as caught:
            review_proposal(root, job_id)

        self.assertEqual(caught.exception.code, "invalid_proposal")

    def test_render_review_uses_safe_fences_and_does_not_truncate_diffs(self):
        root = self.make_root()
        source = self.register_source(root, content="Evidence line.\n")
        body = "Example follows:\n```python\nprint('<script>')\n```\n" + ("long content line\n" * 500) + "END-OF-DIFF\n"
        content = self.page_content("fenced", source, body)
        job_id, _ = self.create_job(
            root,
            source,
            self.proposal(
                [{"path": "wiki/processes/fenced.md", "content": content}],
                evidence=[self.citation(source)],
                summary="<script> stays displayed as proposal text.",
            ),
        )

        rendered = render_review(review_proposal(root, job_id))

        self.assertIn(f"Proposal {job_id}", rendered)
        self.assertIn("<script> stays displayed", rendered)
        self.assertIn("````diff", rendered)
        self.assertIn("```python", rendered)
        self.assertIn("END-OF-DIFF", rendered)
        self.assertIn("line:1", rendered)

    def test_unified_diffs_mark_missing_final_newlines_for_updates_creates_and_newline_only_edits(self):
        root = self.make_root()
        source = self.register_source(root, source_id="newline-evidence")
        update_path = root / "wiki" / "processes" / "update-no-newline.md"
        newline_path = root / "wiki" / "processes" / "newline-only.md"
        update_current = self.page_content("update-no-newline", source, "Old final line.")
        update_proposed = self.page_content("update-no-newline", source, "New final line.")
        newline_current = self.page_content("newline-only", source, "Same final line")
        newline_proposed = self.page_content("newline-only", source, "Same final line\n")
        update_path.write_text(update_current, encoding="utf-8")
        newline_path.write_text(newline_current, encoding="utf-8")
        create_content = self.page_content("create-no-newline", source, "Created final line.")
        proposal = self.proposal(
            [
                {"path": "wiki/processes/update-no-newline.md", "content": update_proposed},
                {"path": "wiki/processes/create-no-newline.md", "content": create_content},
                {"path": "wiki/processes/newline-only.md", "content": newline_proposed},
            ],
            evidence=[self.citation(source)],
        )
        job_id, _ = self.create_job(root, source, proposal)

        report = review_proposal(root, job_id)
        diffs = {change["path"]: change["diff"] for change in report["changes"]}

        marker = "\\ No newline at end of file"
        self.assertIn(f"-Old final line.\n{marker}\n+New final line.\n{marker}", diffs["wiki/processes/update-no-newline.md"])
        self.assertIn(f"+Created final line.\n{marker}", diffs["wiki/processes/create-no-newline.md"])
        self.assertIn(f"-Same final line\n{marker}\n+Same final line\n", diffs["wiki/processes/newline-only.md"])


if __name__ == "__main__":
    unittest.main()
