import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from wiki_tools.errors import WikiError
from wiki_tools.feedback import feedback_status, submit_feedback
from wiki_tools.maintenance import complete_job, retry_job, run_maintenance
from wiki_tools.sources import add_source


class MaintenanceTests(unittest.TestCase):
    def make_root(self):
        temporary = tempfile.TemporaryDirectory(prefix="wiki-maintenance-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "wiki" / "processes").mkdir(parents=True)
        return root

    def set_source_authorship(self, root, source_id, revision, authorship):
        manifest_path = root / "sources" / "manifests" / f"{source_id}--{revision}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if authorship is None:
            manifest.pop("authorship", None)
        else:
            manifest["authorship"] = authorship
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def payload(self):
        return {
            "feedback_id": "fb-maintenance-1",
            "answer_id": "answer-1",
            "wiki_revision": "abc123",
            "target": "refund#approval",
            "description": "The approval step is missing.",
            "evidence": [],
        }

    def proposal(self, content=None):
        if content is None:
            metadata = {
                "id": "refund",
                "title": "Refund",
                "kind": "process",
                "domain": "billing",
                "review_status": "draft",
                "source_refs": [{"source_id": row["source_id"], "revision": row["revision"]} for row in self.evidence],
                "depends_on": [],
                "reviewed_at": None,
            }
            content = "---\n" + json.dumps(metadata) + "\n---\nApproved refunds are logged.\n"
        return {
            "outcome": "proposed",
            "summary": "Document the approval and audit log steps.",
            "changes": [{"path": "wiki/processes/refund.md", "content": content}],
            "evidence": self.evidence,
        }

    def register_evidence(self, root):
        (root / "sources" / "raw").mkdir(parents=True, exist_ok=True)
        (root / "sources" / "manifests").mkdir(parents=True, exist_ok=True)
        evidence_path = root / "evidence.md"
        evidence_path.write_text("Approved refunds are logged.\n", encoding="utf-8")
        source = add_source(root, evidence_path, source_id="refund-procedure", kind="procedure")
        self.evidence = [
            {
                "source_id": "refund-procedure",
                "revision": source["revision"],
                "locator": "line:1",
                "authorship": source["authorship"],
            }
        ]

    def test_run_saves_proposal_for_review_without_writing_wiki(self):
        root = self.make_root()
        self.register_evidence(root)
        original = '---\n{"id":"refund"}\n---\nCurrent page.\n'
        page = root / "wiki" / "processes" / "refund.md"
        page.write_text(original, encoding="utf-8")
        submitted = submit_feedback(root, self.payload())

        result = run_maintenance(root, lambda prompt: self.proposal())

        self.assertEqual(result["items"][0]["status"], "ready_for_review")
        self.assertEqual(page.read_text(encoding="utf-8"), original)
        proposal_path = root / ".state" / "proposals" / f"{submitted['job_id']}.json"
        self.assertEqual(json.loads(proposal_path.read_text(encoding="utf-8")), self.proposal())
        self.assertEqual(feedback_status(root, submitted["feedback_id"])["status"], "ready_for_review")

    def test_maintenance_discovers_raw_drop_and_queues_ingest_under_writer_lock(self):
        from wiki_tools.feedback import _connect

        root = self.make_root()
        dropped = root / "sources" / "raw" / "Review note.md"
        dropped.parent.mkdir(parents=True)
        dropped.write_text("A second reviewer checks payment approval.\n", encoding="utf-8")
        seen = []

        def execute(prompt):
            seen.append(prompt)
            return {
                "outcome": "needs_evidence",
                "summary": "The dropped source needs a human to identify its process and scope.",
                "changes": [],
                "evidence": [],
            }

        result = run_maintenance(root, execute)

        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["items"][0]["status"], "ready_for_review")
        self.assertIn('"operation": "ingest_source"', seen[0])
        self.assertIn('"kind": "unclassified"', seen[0])
        connection = _connect(root)
        try:
            jobs = connection.execute("SELECT job_type, status FROM jobs").fetchall()
        finally:
            connection.close()
        self.assertEqual([(row["job_type"], row["status"]) for row in jobs], [("ingest", "ready_for_review")])
        self.assertEqual(dropped.read_text(encoding="utf-8"), "A second reviewer checks payment approval.\n")

    def test_legacy_ingest_job_prompt_looks_up_manifest_authorship(self):
        from wiki_tools.feedback import _connect, enqueue_ingest

        root = self.make_root()
        self.register_evidence(root)
        source_id = self.evidence[0]["source_id"]
        revision = self.evidence[0]["revision"]
        self.set_source_authorship(root, source_id, revision, None)
        queued = enqueue_ingest(root, source_id, revision)

        connection = _connect(root)
        try:
            legacy_payload = {"source_id": source_id, "revision": revision, "kind": "procedure"}
            connection.execute(
                "UPDATE jobs SET payload = ? WHERE job_id = ?",
                (json.dumps(legacy_payload), queued["job_id"]),
            )
        finally:
            connection.close()

        seen = []

        def execute(prompt):
            seen.append(prompt)
            return {"outcome": "needs_evidence", "summary": "No change is proposed.", "changes": [], "evidence": []}

        run_maintenance(root, execute)
        envelope = json.loads(seen[0].splitlines()[-1])
        self.assertEqual(envelope["payload"]["authorship"], "unknown")

    def test_proposal_evidence_uses_manifest_authorship_and_revalidates_on_completion(self):
        root = self.make_root()
        self.register_evidence(root)
        source_id = self.evidence[0]["source_id"]
        revision = self.evidence[0]["revision"]
        self.set_source_authorship(root, source_id, revision, "ai-generated")
        forged = dict(self.evidence[0], authorship="human-written")
        proposal = {
            "outcome": "rejected",
            "summary": "The request is not supported by the cited source.",
            "changes": [],
            "evidence": [forged],
        }
        submitted = submit_feedback(root, self.payload())

        result = run_maintenance(root, lambda prompt: proposal)

        self.assertEqual(result["items"][0]["status"], "ready_for_review")
        proposal_path = root / ".state" / "proposals" / f"{submitted['job_id']}.json"
        saved = json.loads(proposal_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["evidence"][0]["authorship"], "ai-generated")

        completed = complete_job(root, submitted["job_id"])
        self.assertEqual(completed["status"], "resolved")
        self.assertEqual(feedback_status(root, submitted["feedback_id"])["status"], "rejected")

    def test_executor_failure_and_invalid_proposal_fail_durably_and_retry_is_explicit(self):
        for executor in (lambda prompt: (_ for _ in ()).throw(RuntimeError("temporary outage")), lambda prompt: {"outcome": "proposed"}):
            with self.subTest(executor=executor):
                root = self.make_root()
                self.register_evidence(root)
                submitted = submit_feedback(root, self.payload())
                result = run_maintenance(root, executor)
                self.assertEqual(result["items"][0]["status"], "failed")
                with self.assertRaises(WikiError) as caught:
                    complete_job(root, submitted["job_id"], "a" * 40)
                self.assertEqual(caught.exception.code, "invalid_state")

                retried = retry_job(root, submitted["job_id"])
                self.assertEqual(retried["status"], "pending")
                self.assertEqual(feedback_status(root, submitted["feedback_id"])["status"], "pending")

    def test_proposed_correction_requires_real_citations_and_valid_page_metadata(self):
        for case in ("no_evidence", "bad_evidence", "bad_page"):
            with self.subTest(case=case):
                root = self.make_root()
                self.register_evidence(root)
                proposal = self.proposal()
                if case == "no_evidence":
                    proposal["evidence"] = []
                elif case == "bad_evidence":
                    proposal["evidence"] = [{"source_id": "missing-source", "revision": "a" * 64, "locator": "line:1"}]
                else:
                    proposal["changes"][0]["content"] = '---\n{"id":"refund"}\n---\nPage without required metadata.\n'
                submitted = submit_feedback(root, self.payload())
                result = run_maintenance(root, lambda prompt, proposal=proposal: proposal)
                self.assertEqual(result["items"][0]["status"], "failed")
                self.assertFalse((root / ".state" / "proposals" / f"{submitted['job_id']}.json").exists())

    def test_rejected_and_needs_evidence_can_finish_without_a_commit(self):
        for outcome, expected_feedback_status in (
            ("needs_evidence", "needs_evidence"),
            ("rejected", "rejected"),
        ):
            root = self.make_root()
            self.register_evidence(root)
            submitted = submit_feedback(root, self.payload())
            citation = dict(self.evidence[0])
            proposal = {"outcome": outcome, "summary": "Reviewed based on available evidence.", "changes": [], "evidence": []}
            if outcome == "rejected":
                proposal["evidence"] = [citation]
            run_maintenance(root, lambda prompt: proposal)
            completed = complete_job(root, submitted["job_id"])
            self.assertEqual(completed["status"], "resolved")
            self.assertIsNone(completed["revision"])
            status = feedback_status(root, submitted["feedback_id"])
            self.assertEqual(status["status"], expected_feedback_status)
            self.assertIsNone(status["published_revision"])

    def test_another_worker_cannot_claim_jobs_while_lock_is_held(self):
        root = self.make_root()
        self.register_evidence(root)
        submit_feedback(root, self.payload())
        entered = threading.Event()
        release = threading.Event()

        def execute(prompt):
            entered.set()
            release.wait(5)
            return self.proposal()

        worker = threading.Thread(target=lambda: run_maintenance(root, execute))
        worker.start()
        self.assertTrue(entered.wait(5))
        try:
            with self.assertRaises(WikiError) as caught:
                run_maintenance(root, lambda prompt: self.proposal())
            self.assertEqual(caught.exception.code, "maintenance_locked")
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())

    def test_invocation_limit_leaves_remaining_jobs_pending(self):
        root = self.make_root()
        self.register_evidence(root)
        first_payload = self.payload()
        second_payload = dict(first_payload, feedback_id="fb-maintenance-2")
        first = submit_feedback(root, first_payload)
        second = submit_feedback(root, second_payload)

        result = run_maintenance(root, lambda prompt: self.proposal(), limit=1)

        self.assertEqual(result["processed"], 1)
        statuses = {
            first["feedback_id"]: feedback_status(root, first["feedback_id"])["status"],
            second["feedback_id"]: feedback_status(root, second["feedback_id"])["status"],
        }
        self.assertEqual(list(statuses.values()).count("ready_for_review"), 1)
        self.assertEqual(list(statuses.values()).count("pending"), 1)

    def test_interrupted_processing_job_requires_explicit_recovery(self):
        from wiki_tools.feedback import _connect

        root = self.make_root()
        self.register_evidence(root)
        submitted = submit_feedback(root, self.payload())
        connection = _connect(root)
        try:
            connection.execute("UPDATE jobs SET status = 'processing' WHERE job_id = ?", (submitted["job_id"],))
            connection.execute("UPDATE feedback SET status = 'processing' WHERE feedback_id = ?", (submitted["feedback_id"],))
        finally:
            connection.close()

        idle_run = run_maintenance(root, lambda prompt: self.proposal())
        self.assertEqual(idle_run["processed"], 0)
        self.assertEqual(feedback_status(root, submitted["feedback_id"])["status"], "processing")
        self.assertEqual(retry_job(root, submitted["job_id"])["status"], "pending")
        result = run_maintenance(root, lambda prompt: self.proposal())
        self.assertEqual(result["items"][0]["status"], "ready_for_review")

    def init_git(self, root):
        env = {**os.environ, "TMPDIR": os.environ.get("TMPDIR", tempfile.gettempdir())}
        commands = [
            ["git", "init", "-q"],
            ["git", "config", "user.email", "wiki@example.invalid"],
            ["git", "config", "user.name", "Wiki Test"],
            ["git", "add", "wiki/processes/refund.md"],
            ["git", "commit", "-qm", "Reviewed proposal"],
        ]
        for command in commands:
            subprocess.run(command, cwd=root, env=env, check=True, capture_output=True, text=True)
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, env=env, check=True, capture_output=True, text=True).stdout.strip()
        return revision

    def test_completion_requires_commit_with_exact_proposed_contents(self):
        root = self.make_root()
        self.register_evidence(root)
        original = '---\n{"id":"refund"}\n---\nCurrent page.\n'
        page = root / "wiki" / "processes" / "refund.md"
        page.write_text(original, encoding="utf-8")
        submitted = submit_feedback(root, self.payload())
        run_maintenance(root, lambda prompt: self.proposal())

        with self.assertRaises(WikiError) as missing:
            complete_job(root, submitted["job_id"], "a" * 40)
        self.assertEqual(missing.exception.code, "invalid_commit")

        wrong_commit = self.init_git(root)
        with self.assertRaises(WikiError) as wrong:
            complete_job(root, submitted["job_id"], wrong_commit)
        self.assertEqual(wrong.exception.code, "proposal_not_in_commit")

        page.write_text(self.proposal()["changes"][0]["content"], encoding="utf-8")
        revision = self.init_git(root)
        completed = complete_job(root, submitted["job_id"], revision)
        self.assertEqual(completed["status"], "resolved")
        self.assertEqual(completed["revision"], revision)
        self.assertEqual(feedback_status(root, submitted["feedback_id"])["status"], "resolved")

    def test_completion_rejects_matching_content_in_a_non_head_commit(self):
        root = self.make_root()
        self.register_evidence(root)
        page = root / "wiki" / "processes" / "refund.md"
        original = '---\n{"id":"refund","title":"Refund","kind":"process","domain":"billing","review_status":"draft","source_refs":[],"depends_on":[],"reviewed_at":null}\n---\nCurrent page.\n'
        page.write_text(original, encoding="utf-8")
        submitted = submit_feedback(root, self.payload())
        run_maintenance(root, lambda prompt: self.proposal())
        self.init_git(root)

        page.write_text(self.proposal()["changes"][0]["content"], encoding="utf-8")
        proposed_commit = self.init_git(root)
        page.write_text(original, encoding="utf-8")
        self.init_git(root)
        with self.assertRaises(WikiError) as caught:
            complete_job(root, submitted["job_id"], proposed_commit)
        self.assertEqual(caught.exception.code, "not_current_commit")

if __name__ == "__main__":
    unittest.main()
