import json
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from wiki_tools.cli import main
from wiki_tools.feedback import _connect


PROJECT = Path(__file__).resolve().parents[1]


def make_root():
    root = Path(tempfile.mkdtemp(prefix="wiki-cli-")).resolve()
    (root / "wiki.toml").write_text("[ask]\n", encoding="utf-8")
    return root


def create_ready_review_job(root):
    job_id = uuid.uuid4().hex
    proposal_path = root / ".state" / "proposals" / f"{job_id}.json"
    proposal_path.parent.mkdir(parents=True)
    proposal_path.write_text(
        json.dumps(
            {
                "outcome": "needs_evidence",
                "summary": "The available material does not yet support a change.",
                "changes": [],
                "evidence": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    connection = _connect(root)
    try:
        created_at = "2026-10-09T10:00:00Z"
        connection.execute(
            "INSERT INTO jobs (job_id, job_type, status, payload, proposal_path, created_at, updated_at) "
            "VALUES (?, 'feedback', 'ready_for_review', ?, ?, ?, ?)",
            (job_id, "{}", f".state/proposals/{job_id}.json", created_at, created_at),
        )
        connection.commit()
    finally:
        connection.close()
    return job_id, proposal_path


def read_job_status(root, job_id):
    database_uri = f"{(root / '.state' / 'queue.sqlite3').as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        row = connection.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return row[0]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wiki-caller-")
        self.outside = Path(self.temp.name).resolve()
        self.root = make_root()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT / "src")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        self.env = env

    def tearDown(self):
        self.temp.cleanup()

    def assert_one_json_line(self, stdout):
        self.assertEqual(stdout.count("\n"), 1, stdout)
        return json.loads(stdout)

    def invoke_mocked_result(self, argv, result):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("wiki_tools.cli._dispatch", return_value=result):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(argv)
        return exit_code, json.loads(stdout.getvalue())

    def test_module_cli_uses_explicit_root_from_external_cwd_and_keeps_leading_flag_literal(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "wiki_tools",
                "--root",
                str(self.root),
                "ask",
                "--model",
                "caller/model",
            ],
            cwd=str(self.outside),
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(completed.returncode, 0)
        value = self.assert_one_json_line(completed.stdout)
        self.assertEqual(value["error"]["code"], "configuration_error")
        self.assertIn("model", value["error"]["message"].lower())

    def test_launcher_runs_without_installation_from_external_cwd(self):
        completed = subprocess.run(
            [
                str(PROJECT / "scripts" / "wiki"),
                "--root",
                str(self.root),
                "ask",
                "question from another repository",
            ],
            cwd=str(self.outside),
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(completed.returncode, 0)
        value = self.assert_one_json_line(completed.stdout)
        self.assertEqual(value["error"]["code"], "configuration_error")

    def test_python_launcher_runs_from_external_cwd(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT / "scripts" / "wiki"),
                "--root",
                str(self.root),
                "ask",
                "question from another repository",
            ],
            cwd=str(self.outside),
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(completed.returncode, 0)
        value = self.assert_one_json_line(completed.stdout)
        self.assertEqual(value["error"]["code"], "configuration_error")

    def test_argument_errors_also_emit_exactly_one_json_value(self):
        completed = subprocess.run(
            [sys.executable, "-m", "wiki_tools", "--root", str(self.root), "unknown-command"],
            cwd=str(self.outside),
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(completed.returncode, 0)
        value = self.assert_one_json_line(completed.stdout)
        self.assertEqual(value["error"]["code"], "argument_error")

    def test_lint_errors_produce_nonzero_exit_while_warnings_remain_success(self):
        error_result = {"errors": [{"code": "broken_link"}], "warnings": []}
        code_with_errors, json_with_errors = self.invoke_mocked_result(
            ["--root", str(self.root), "lint"], error_result
        )
        self.assertNotEqual(code_with_errors, 0)
        self.assertEqual(json_with_errors, error_result)

        warning_result = {"errors": [], "warnings": [{"code": "unknown_format"}]}
        code_with_warnings, json_with_warnings = self.invoke_mocked_result(
            ["--root", str(self.root), "lint"], warning_result
        )
        self.assertEqual(code_with_warnings, 0)
        self.assertEqual(json_with_warnings, warning_result)

    def test_failed_maintenance_item_produces_nonzero_exit(self):
        failed_result = {
            "processed": 1,
            "items": [{"job_id": "job-1", "status": "failed", "error": "executor_error"}],
        }

        code, value = self.invoke_mocked_result(
            ["--root", str(self.root), "maintenance", "run"], failed_result
        )

        self.assertNotEqual(code, 0)
        self.assertEqual(value, failed_result)

    def test_ready_maintenance_item_and_empty_queue_are_success(self):
        for result in (
            {"processed": 0, "items": []},
            {"processed": 1, "items": [{"job_id": "job-1", "status": "ready_for_review"}]},
        ):
            code, value = self.invoke_mocked_result(
                ["--root", str(self.root), "maintenance", "run"], result
            )
            self.assertEqual(code, 0)
            self.assertEqual(value, result)

    def test_maintenance_run_forwards_target_id_and_limit(self):
        result = {"processed": 1, "items": [{"job_id": "a" * 32, "status": "ready_for_review"}]}
        with patch("wiki_tools.cli.load_config", return_value=object()), patch(
            "wiki_tools.maintenance.run_maintenance", return_value=result
        ) as runner:
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                exit_code = main([
                    "--root", str(self.root), "maintenance", "run", "--limit", "5", "--id", "a" * 32,
                ])

        self.assertEqual(exit_code, 0)
        self.assertEqual(self.assert_one_json_line(stdout.getvalue()), result)
        runner.assert_called_once()
        self.assertEqual(runner.call_args.args[0], self.root)
        self.assertEqual(runner.call_args.kwargs["limit"], 5)
        self.assertEqual(runner.call_args.kwargs["job_id"], "a" * 32)

    def test_maintenance_review_without_queue_returns_json_without_creating_database(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            exit_code = main(["--root", str(self.root), "maintenance", "review"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            self.assert_one_json_line(stdout.getvalue()),
            {"status": "no_ready_proposals", "job_id": None, "ready_job_ids": []},
        )
        self.assertFalse((self.root / ".state").exists())

    def test_maintenance_review_json_uses_real_backend_and_leaves_target_ready(self):
        job_id, proposal_path = create_ready_review_job(self.root)
        proposal_before = proposal_path.read_bytes()
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            exit_code = main(["--root", str(self.root), "maintenance", "review", "--id", job_id])

        self.assertEqual(exit_code, 0)
        report = self.assert_one_json_line(stdout.getvalue())
        self.assertEqual(report["status"], "ready_for_review")
        self.assertEqual(report["job_id"], job_id)
        self.assertEqual(report["ready_job_ids"], [job_id])
        self.assertEqual(report["outcome"], "needs_evidence")
        self.assertEqual(report["summary"], "The available material does not yet support a change.")
        self.assertEqual(report["changes"], [])
        self.assertEqual(report["evidence"], [])
        self.assertEqual(read_job_status(self.root, job_id), "ready_for_review")
        self.assertEqual(proposal_path.read_bytes(), proposal_before)

    def test_maintenance_review_markdown_is_readable_and_leaves_target_ready(self):
        job_id, proposal_path = create_ready_review_job(self.root)
        proposal_before = proposal_path.read_bytes()
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            exit_code = main([
                "--root", str(self.root), "maintenance", "review", "--id", job_id, "--format", "markdown",
            ])

        markdown = stdout.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn(f"# Proposal {job_id}", markdown)
        self.assertIn("**needs_evidence**", markdown)
        self.assertIn("## Rationale", markdown)
        self.assertIn("The available material does not yet support a change.", markdown)
        self.assertIn("No wiki changes are proposed for this outcome.", markdown)
        self.assertIn("No verified evidence citations are included.", markdown)
        self.assertIn("Accept, Revise, or Defer.", markdown)
        self.assertNotIn('"status":"ready_for_review"', markdown)
        self.assertEqual(read_job_status(self.root, job_id), "ready_for_review")
        self.assertEqual(proposal_path.read_bytes(), proposal_before)

    def test_maintenance_review_missing_target_returns_structured_error_without_creating_database(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            exit_code = main([
                "--root", str(self.root), "maintenance", "review", "--id", "a" * 32,
            ])

        self.assertNotEqual(exit_code, 0)
        self.assertEqual(
            self.assert_one_json_line(stdout.getvalue())["error"]["code"],
            "job_not_found",
        )
        self.assertFalse((self.root / ".state").exists())

    def test_source_add_authorship_flag_is_validated_and_propagated(self):
        source_file = self.outside / "draft.md"
        source_file.write_text("A draft.\n", encoding="utf-8")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main([
                "--root", str(self.root), "source", "add", str(source_file),
                "--id", "cli-draft", "--kind", "unclassified", "--authorship", "ai-generated",
            ])
        self.assertEqual(exit_code, 0)
        self.assertEqual(self.assert_one_json_line(stdout.getvalue())["authorship"], "ai-generated")

        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            default_exit = main([
                "--root", str(self.root), "source", "add", str(source_file),
                "--id", "cli-default", "--kind", "unclassified",
            ])
        self.assertEqual(default_exit, 0)
        self.assertEqual(self.assert_one_json_line(stdout.getvalue())["authorship"], "unknown")

        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            invalid_exit = main([
                "--root", str(self.root), "source", "add", str(source_file),
                "--id", "cli-invalid", "--kind", "unclassified", "--authorship", "machine-written",
            ])
        self.assertNotEqual(invalid_exit, 0)
        invalid = self.assert_one_json_line(stdout.getvalue())
        self.assertEqual(invalid["error"]["code"], "argument_error")

    def test_source_add_help_documents_authorship_in_english(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            exit_code = main(["--root", str(self.root), "source", "add", "--help"])

        self.assertEqual(exit_code, 0)
        help_text = self.assert_one_json_line(stdout.getvalue())["help"]
        self.assertIn("--authorship", help_text)
        self.assertIn("human-written", help_text)
        self.assertIn("ai-generated", help_text)
        self.assertIn("unknown", help_text)
        self.assertIn("Authorship", help_text)

    def test_source_add_project_option_is_validated_and_persisted(self):
        source_file = self.outside / "project-note.md"
        source_file.write_text("Idea for Atlas.\n", encoding="utf-8")
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            exit_code = main([
                "--root", str(self.root), "source", "add", str(source_file),
                "--id", "atlas-note", "--kind", "unclassified", "--project", "atlas",
            ])
        self.assertEqual(exit_code, 0)
        value = self.assert_one_json_line(stdout.getvalue())
        self.assertEqual(value["scope"]["project"], "atlas")

        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            invalid_exit = main([
                "--root", str(self.root), "source", "add", str(source_file),
                "--id", "bad-project", "--kind", "unclassified", "--project", "Atlas",
            ])
        self.assertNotEqual(invalid_exit, 0)
        self.assertEqual(self.assert_one_json_line(stdout.getvalue())["error"]["code"], "invalid_project")

    def test_search_project_option_is_forwarded_to_knowledge_search(self):
        with patch("wiki_tools.knowledge.search", return_value=[{"id": "atlas-note"}]) as searcher:
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                exit_code = main(["--root", str(self.root), "search", "--project", "atlas", "expense", "approval"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(self.assert_one_json_line(stdout.getvalue()), [{"id": "atlas-note"}])
        searcher.assert_called_once_with(self.root, "expense approval", project="atlas")

    def test_ask_project_flag_merges_with_request_and_keeps_literal_tail(self):
        calls = []
        stdout = io.StringIO()
        with patch("wiki_tools.cli.ask", side_effect=lambda root, request: calls.append(request) or request):
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                exit_code = main([
                    "--root", str(self.root), "ask", "--project", "atlas", "--", "--model", "literal question",
                ])
        self.assertEqual(exit_code, 0)
        self.assertEqual(calls[-1], {"question": "--model literal question", "scope": {"project": "atlas"}})
        self.assertEqual(self.assert_one_json_line(stdout.getvalue()), calls[-1])

        request_file = self.outside / "request.json"
        request_file.write_text(json.dumps({"question": "Where is approval?", "scope": {"process": "refund"}}), encoding="utf-8")
        stdout = io.StringIO()
        with patch("wiki_tools.cli.ask", side_effect=lambda root, request: calls.append(request) or request):
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                exit_code = main([
                    "--root", str(self.root), "ask", "--request", str(request_file), "--project", "atlas",
                ])
        self.assertEqual(exit_code, 0)
        self.assertEqual(calls[-1], {"question": "Where is approval?", "scope": {"process": "refund", "project": "atlas"}})

    def test_ask_project_accepts_natural_positional_question_without_separator(self):
        calls = []
        stdout = io.StringIO()
        with patch("wiki_tools.cli.ask", side_effect=lambda root, request: calls.append(request) or request):
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                exit_code = main([
                    "--root", str(self.root), "ask", "--project", "atlas", "What ideas did we record?",
                ])

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [{"question": "What ideas did we record?", "scope": {"project": "atlas"}}])
        self.assertEqual(self.assert_one_json_line(stdout.getvalue()), calls[0])

    def test_ask_rejects_conflicting_or_malformed_project_scope(self):
        request_file = self.outside / "request.json"
        cases = (
            ({"question": "Where?", "scope": {"project": "borealis"}}, "atlas", "argument_error"),
            ({"question": "Where?", "scope": "atlas"}, "atlas", "argument_error"),
            ({"question": "Where?", "scope": {"project": []}}, None, "invalid_project"),
        )
        for payload, selected_project, expected_code in cases:
            with self.subTest(payload=payload, selected_project=selected_project):
                request_file.write_text(json.dumps(payload), encoding="utf-8")
                argv = ["--root", str(self.root), "ask", "--request", str(request_file)]
                if selected_project is not None:
                    argv.extend(["--project", selected_project])
                stdout = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    exit_code = main(argv)
                self.assertNotEqual(exit_code, 0)
                self.assertEqual(self.assert_one_json_line(stdout.getvalue())["error"]["code"], expected_code)

if __name__ == "__main__":
    unittest.main()
