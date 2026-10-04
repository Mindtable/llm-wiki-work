import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from wiki_tools.cli import main


PROJECT = Path(__file__).resolve().parents[1]


def make_root():
    root = Path(tempfile.mkdtemp(prefix="wiki-cli-")).resolve()
    (root / "wiki.toml").write_text("[ask]\n", encoding="utf-8")
    return root


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wiki-caller-")
        self.outside = Path(self.temp.name).resolve()
        self.root = make_root()
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


if __name__ == "__main__":
    unittest.main()
