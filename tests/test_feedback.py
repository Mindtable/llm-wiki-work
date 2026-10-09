import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from wiki_tools.errors import WikiError
from wiki_tools.feedback import _connect, enqueue_ingest, feedback_status, submit_feedback
from wiki_tools.sources import add_source


class FeedbackQueueTests(unittest.TestCase):
    def make_root(self):
        temporary = tempfile.TemporaryDirectory(prefix="wiki-feedback-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "sources" / "raw").mkdir(parents=True)
        (root / "sources" / "manifests").mkdir(parents=True)
        return root

    def payload(self):
        return {
            "feedback_id": "fb-2026-01",
            "answer_id": "answer-1",
            "wiki_revision": "abc123",
            "target": "refund#approval",
            "description": "The approval step is missing.",
            "evidence": [],
        }

    def test_same_feedback_id_and_payload_is_idempotent_but_changed_payload_conflicts(self):
        root = self.make_root()
        first = submit_feedback(root, self.payload())
        repeated = submit_feedback(root, dict(reversed(list(self.payload().items()))))
        self.assertEqual(first["feedback_id"], repeated["feedback_id"])
        self.assertEqual(first["job_id"], repeated["job_id"])

        changed = self.payload()
        changed["description"] = "Different claim under the same ID."
        with self.assertRaises(WikiError) as caught:
            submit_feedback(root, changed)
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_feedback_status_survives_database_reopen(self):
        root = self.make_root()
        submitted = submit_feedback(root, self.payload())
        status = feedback_status(root, submitted["feedback_id"])
        self.assertEqual(status["status"], "pending")
        self.assertEqual(status["job_id"], submitted["job_id"])

    def test_ingest_requires_registered_manifest_and_deduplicates_job(self):
        root = self.make_root()
        with self.assertRaises(WikiError) as caught:
            enqueue_ingest(root, "procedure", "a" * 64)
        self.assertEqual(caught.exception.code, "source_not_found")

        incoming = root / "procedure.md"
        incoming.write_text("Start processing only after approval.\n", encoding="utf-8")
        registered = add_source(
            root,
            incoming,
            source_id="procedure",
            kind="unclassified",
            authorship="ai-generated",
            project="atlas",
        )
        first = enqueue_ingest(root, "procedure", registered["revision"])
        repeated = enqueue_ingest(root, "procedure", registered["revision"])
        self.assertEqual(first["job_id"], repeated["job_id"])
        connection = _connect(root)
        try:
            queued_payload = json.loads(connection.execute("SELECT payload FROM jobs WHERE job_id = ?", (first["job_id"],)).fetchone()["payload"])
        finally:
            connection.close()
        self.assertEqual(queued_payload["authorship"], "ai-generated")
        self.assertEqual(queued_payload["scope"]["project"], "atlas")


if __name__ == "__main__":
    unittest.main()
