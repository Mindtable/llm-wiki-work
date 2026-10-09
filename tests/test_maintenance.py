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
from wiki_tools.maintenance import _job_project, _validate_proposal, complete_job, retry_job, run_maintenance
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

    def set_source_project(self, root, source_id, revision, project):
        manifest_path = root / "sources" / "manifests" / f"{source_id}--{revision}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        scope = manifest.setdefault("scope", {})
        if project is None:
            scope.pop("project", None)
        else:
            scope["project"] = project
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def project_page(self, page_id, project=None, source_refs=None):
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
        if project is not None:
            metadata["scope"] = {"project": project}
        return "---\n" + json.dumps(metadata) + "\n---\nProject-scoped page.\n"

    def set_source_project(self, root, source_id, revision, project):
        manifest_path = root / "sources" / "manifests" / f"{source_id}--{revision}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        scope = manifest.setdefault("scope", {})
        if project is None:
            scope.pop("project", None)
        else:
            scope["project"] = project
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def project_page(self, page_id, project=None, source_refs=None):
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
        if project is not None:
            metadata["scope"] = {"project": project}
        return "---\n" + json.dumps(metadata) + "\n---\nProject-scoped page.\n"

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
        expected_proposal = self.proposal()
        self.assertNotIn("authorship", expected_proposal["evidence"][0])
        self.assertNotIn("project", expected_proposal["evidence"][0])
        expected_proposal["evidence"][0]["authorship"] = "unknown"
        expected_proposal["evidence"][0]["project"] = None
        self.assertEqual(json.loads(proposal_path.read_text(encoding="utf-8")), expected_proposal)
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
        self.set_source_project(root, source_id, revision, "alpha")
        forged = dict(self.evidence[0], authorship="human-written", project="beta")
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
        self.assertEqual(saved["evidence"][0]["project"], "alpha")

        completed = complete_job(root, submitted["job_id"])
        self.assertEqual(completed["status"], "resolved")
        self.assertEqual(feedback_status(root, submitted["feedback_id"])["status"], "rejected")

    def test_scoped_maintenance_requires_project_page_and_matching_evidence(self):
        root = self.make_root()
        self.register_evidence(root)
        alpha_id = self.evidence[0]["source_id"]
        alpha_revision = self.evidence[0]["revision"]
        self.set_source_project(root, alpha_id, alpha_revision, "alpha")
        beta_path = root / "beta-evidence.md"
        beta_path.write_text("A beta-specific observation.\n", encoding="utf-8")
        beta_source = add_source(root, beta_path, source_id="beta-evidence", kind="procedure", project="beta")

        valid = {
            "outcome": "proposed",
            "summary": "Record the alpha-specific process.",
            "changes": [
                {
                    "path": "wiki/processes/alpha-process.md",
                    "content": self.project_page(
                        "alpha-process",
                        "alpha",
                        [{"source_id": alpha_id, "revision": alpha_revision}],
                    ),
                }
            ],
            "evidence": [{"source_id": alpha_id, "revision": alpha_revision, "locator": "line:1"}],
        }
        normalized = _validate_proposal(root, valid, expected_project="alpha")
        self.assertEqual(normalized["evidence"][0]["source_id"], alpha_id)
        self.assertEqual(normalized["evidence"][0]["project"], "alpha")

        shared_path = root / "shared-evidence.md"
        shared_path.write_text("A generally applicable rule.\n", encoding="utf-8")
        shared_source = add_source(root, shared_path, source_id="shared-evidence", kind="procedure")
        general_job = {
            "outcome": "proposed",
            "summary": "Record a generally applicable process.",
            "changes": [
                {
                    "path": "wiki/processes/shared-process.md",
                    "content": self.project_page(
                        "shared-process",
                        None,
                        [{"source_id": "shared-evidence", "revision": shared_source["revision"]}],
                    ),
                }
            ],
            "evidence": [{"source_id": "shared-evidence", "revision": shared_source["revision"], "locator": "line:1"}],
        }
        _validate_proposal(root, general_job, expected_project=None, project_bound=True)

        (root / "wiki" / "index.md").write_text("# Shared index\n", encoding="utf-8")
        (root / "wiki" / "log.md").write_text("# Publication history\n", encoding="utf-8")
        for navigation_page in ("wiki/index.md", "wiki/log.md"):
            with self.subTest(navigation_page=navigation_page):
                navigation_evidence = json.loads(json.dumps(general_job))
                navigation_evidence["evidence"][0]["wiki_page"] = navigation_page
                with self.assertRaises(WikiError) as rejected_navigation:
                    _validate_proposal(root, navigation_evidence, expected_project=None, project_bound=True)
                self.assertEqual(rejected_navigation.exception.code, "invalid_proposal")

        general_with_project_evidence = json.loads(json.dumps(general_job))
        general_with_project_evidence["evidence"] = [
            {"source_id": "beta-evidence", "revision": beta_source["revision"], "locator": "line:1"}
        ]
        with self.assertRaises(WikiError) as project_evidence:
            _validate_proposal(root, general_with_project_evidence, expected_project=None, project_bound=True)
        self.assertEqual(project_evidence.exception.code, "invalid_proposal")

        global_page = json.loads(json.dumps(valid))
        global_page["changes"][0]["content"] = self.project_page("alpha-process")
        with self.assertRaises(WikiError) as missing_scope:
            _validate_proposal(root, global_page, expected_project="alpha")
        self.assertEqual(missing_scope.exception.code, "invalid_proposal")

        wrong_evidence = json.loads(json.dumps(valid))
        wrong_evidence["evidence"] = [
            {"source_id": "beta-evidence", "revision": beta_source["revision"], "locator": "line:1"}
        ]
        with self.assertRaises(WikiError) as foreign_evidence:
            _validate_proposal(root, wrong_evidence, expected_project="alpha")
        self.assertEqual(foreign_evidence.exception.code, "invalid_proposal")

        beta_page = root / "wiki" / "processes" / "beta-evidence-page.md"
        beta_page.write_text(self.project_page("beta-evidence-page", "beta"), encoding="utf-8")
        foreign_page_evidence = json.loads(json.dumps(valid))
        foreign_page_evidence["evidence"][0]["wiki_page"] = "wiki/processes/beta-evidence-page.md"
        with self.assertRaises(WikiError) as foreign_page:
            _validate_proposal(root, foreign_page_evidence, expected_project="alpha")
        self.assertEqual(foreign_page.exception.code, "invalid_proposal")

    def test_scoped_maintenance_cannot_relabel_an_existing_general_or_foreign_page(self):
        for existing_project in (None, "beta"):
            with self.subTest(existing_project=existing_project):
                root = self.make_root()
                self.register_evidence(root)
                alpha_id = self.evidence[0]["source_id"]
                alpha_revision = self.evidence[0]["revision"]
                self.set_source_project(root, alpha_id, alpha_revision, "alpha")
                existing_refs = []
                if existing_project == "beta":
                    beta_path = root / "beta-existing.md"
                    beta_path.write_text("Existing beta page evidence.\n", encoding="utf-8")
                    beta_source = add_source(root, beta_path, source_id="beta-existing", kind="procedure", project="beta")
                    existing_refs = [{"source_id": "beta-existing", "revision": beta_source["revision"]}]
                existing_page = root / "wiki" / "processes" / "existing.md"
                existing_page.write_text(self.project_page("existing", existing_project, existing_refs), encoding="utf-8")
                proposal = {
                    "outcome": "proposed",
                    "summary": "Relabel the existing page.",
                    "changes": [
                        {
                            "path": "wiki/processes/existing.md",
                            "content": self.project_page(
                                "existing",
                                "alpha",
                                [{"source_id": alpha_id, "revision": alpha_revision}],
                            ),
                        }
                    ],
                    "evidence": [{"source_id": alpha_id, "revision": alpha_revision, "locator": "line:1"}],
                }

                with self.assertRaises(WikiError) as rejected:
                    _validate_proposal(root, proposal, expected_project="alpha")
                self.assertEqual(rejected.exception.code, "invalid_proposal")
                with self.assertRaises(WikiError) as unbound_rejected:
                    _validate_proposal(root, proposal, expected_project=None, project_bound=False)
                self.assertEqual(unbound_rejected.exception.code, "invalid_proposal")

    def test_scoped_ingest_completion_rechecks_edited_saved_page_scope(self):
        from wiki_tools.feedback import enqueue_ingest

        root = self.make_root()
        self.register_evidence(root)
        source_id = self.evidence[0]["source_id"]
        revision = self.evidence[0]["revision"]
        self.set_source_project(root, source_id, revision, "alpha")
        queued = enqueue_ingest(root, source_id, revision)
        proposal = {
            "outcome": "proposed",
            "summary": "Add an alpha-only process page.",
            "changes": [
                {
                    "path": "wiki/processes/alpha-process.md",
                    "content": self.project_page(
                        "alpha-process",
                        "alpha",
                        [{"source_id": source_id, "revision": revision}],
                    ),
                }
            ],
            "evidence": [{"source_id": source_id, "revision": revision, "locator": "line:1"}],
        }
        result = run_maintenance(root, lambda prompt: proposal)
        self.assertEqual(result["items"][0]["status"], "ready_for_review")

        proposal_path = root / ".state" / "proposals" / f"{queued['job_id']}.json"
        saved = json.loads(proposal_path.read_text(encoding="utf-8"))
        saved["changes"][0]["content"] = self.project_page("alpha-process")
        proposal_path.write_text(json.dumps(saved), encoding="utf-8")
        with self.assertRaises(WikiError) as rejected:
            complete_job(root, queued["job_id"])
        self.assertEqual(rejected.exception.code, "invalid_proposal")

    def test_feedback_project_is_read_from_safe_local_answer_not_feedback_payload(self):
        root = self.make_root()
        answer_id = "a" * 32
        answer_dir = root / ".state" / "answers"
        answer_dir.mkdir(parents=True)
        (answer_dir / f"{answer_id}.json").write_text(
            json.dumps({"answer_id": answer_id, "scope": {"project": "alpha"}}),
            encoding="utf-8",
        )
        feedback_job = {
            "job_type": "feedback",
            "payload": json.dumps({"answer_id": answer_id, "project": "beta"}),
        }
        scoped = _job_project(root, feedback_job)
        self.assertEqual(scoped.project, "alpha")
        self.assertTrue(scoped.bound)

        absent_answer = {
            "job_type": "feedback",
            "payload": json.dumps({"answer_id": "b" * 32, "project": "beta"}),
        }
        unbound = _job_project(root, absent_answer)
        self.assertIsNone(unbound.project)
        self.assertFalse(unbound.bound)

        unscoped_answer_id = "d" * 32
        (answer_dir / f"{unscoped_answer_id}.json").write_text(
            json.dumps({"answer_id": unscoped_answer_id, "scope": {"project": None}}),
            encoding="utf-8",
        )
        unscoped_answer = {
            "job_type": "feedback",
            "payload": json.dumps({"answer_id": unscoped_answer_id}),
        }
        unscoped_binding = _job_project(root, unscoped_answer)
        self.assertIsNone(unscoped_binding.project)
        self.assertFalse(unscoped_binding.bound)

        unsafe_answer = {
            "job_type": "feedback",
            "payload": json.dumps({"answer_id": "../../outside", "project": "beta"}),
        }
        unsafe = _job_project(root, unsafe_answer)
        self.assertIsNone(unsafe.project)
        self.assertFalse(unsafe.bound)

        (answer_dir / f"{'c' * 32}.json").write_text(
            json.dumps({"answer_id": "c" * 32, "scope": "malformed"}),
            encoding="utf-8",
        )
        malformed_answer = {
            "job_type": "feedback",
            "payload": json.dumps({"answer_id": "c" * 32}),
        }
        with self.assertRaises(WikiError) as malformed_scope:
            _job_project(root, malformed_answer)
        self.assertEqual(malformed_scope.exception.code, "invalid_state")

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
