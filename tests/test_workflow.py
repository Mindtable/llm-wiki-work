import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _write_config(template_path: Path, destination: Path, executable: Path) -> None:
    values = tomllib.loads(template_path.read_text(encoding="utf-8"))
    ask = dict(values["ask"])
    ask.update(
        {
            "executable": str(executable),
            "model": "test/fake",
            "timeout_seconds": 15,
            "max_steps": 4,
        }
    )
    lines = ["[ask]"]
    for key, value in sorted(ask.items()):
        encoded = json.dumps(value, ensure_ascii=False) if isinstance(value, str) else str(value)
        lines.append(f"{key} = {encoded}")
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_fake_opencode(
    executable: Path,
    responses_path: Path,
    captures_path: Path,
) -> None:
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"responses_path = {str(responses_path)!r}\n"
        f"captures_path = {str(captures_path)!r}\n"
        "with open(responses_path, encoding='utf-8') as stream:\n"
        "    responses = json.load(stream)\n"
        "agent = sys.argv[sys.argv.index('--agent') + 1]\n"
        "prompt = sys.argv[-1]\n"
        "if os.path.exists(captures_path):\n"
        "    with open(captures_path, encoding='utf-8') as stream:\n"
        "        previous_calls = [json.loads(line) for line in stream if line.strip()]\n"
        "else:\n"
        "    previous_calls = []\n"
        "response = responses[agent]\n"
        "if isinstance(response, list):\n"
        "    response = response[sum(1 for call in previous_calls if call['agent'] == agent)]\n"
        "with open(captures_path, 'a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps({'agent': agent, 'cwd': os.getcwd(), 'args': sys.argv[1:], 'prompt': prompt}) + '\\n')\n"
        "response = json.dumps(response, ensure_ascii=False, separators=(',', ':'))\n"
        "session = 'fake-' + str(os.getpid())\n"
        "message = 'final-' + str(os.getpid())\n"
        "events = [\n"
        "    {'type': 'step_start', 'timestamp': 1, 'sessionID': session, 'part': {'id': 'start', 'messageID': message, 'type': 'step-start'}},\n"
        "    {'type': 'text', 'timestamp': 2, 'sessionID': session, 'part': {'id': 'text', 'messageID': message, 'type': 'text', 'text': response, 'time': {'start': 1, 'end': 2}}},\n"
        "    {'type': 'step_finish', 'timestamp': 3, 'sessionID': session, 'part': {'id': 'finish', 'messageID': message, 'type': 'step-finish', 'reason': 'stop'}},\n"
        "]\n"
        "for event in events:\n"
        "    print(json.dumps(event, ensure_ascii=False, separators=(',', ':')))\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)


class WorkflowIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wiki-workflow-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "wiki-root"
        self.caller = self.base / "caller-project"
        self.root.mkdir()
        self.caller.mkdir()
        self.environment = os.environ.copy()
        self.environment["TMPDIR"] = os.environ.get("TMPDIR", tempfile.gettempdir())
        source_path = str(REPO / "src")
        existing_pythonpath = self.environment.get("PYTHONPATH")
        self.environment["PYTHONPATH"] = (
            source_path + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
        )

    def _cli(self, *arguments: str) -> dict:
        command = [
            sys.executable,
            str(REPO / "scripts" / "wiki"),
            "--root",
            str(self.root),
            *arguments,
        ]
        completed = subprocess.run(
            command,
            cwd=self.caller,
            env=self.environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"CLI failed: {completed.stderr}\nstdout: {completed.stdout}",
        )
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"CLI must emit one JSON object: {exc}: {completed.stdout!r}")
        self.assertIsInstance(value, dict)
        return value

    def _git(self, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            env=self.environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"git {' '.join(arguments)} failed: {completed.stderr}",
        )
        return completed.stdout.strip()

    def _commit_all(self, message: str) -> str:
        self._git("add", "-A")
        self._git("commit", "-qm", message)
        return self._git("rev-parse", "HEAD")

    def test_external_cwd_source_answer_feedback_proposal_and_git_completion(self):
        (self.root / "wiki" / "processes").mkdir(parents=True)
        (self.root / "wiki" / "concepts").mkdir()
        (self.root / "wiki" / "systems").mkdir()
        (self.root / "wiki" / "sources").mkdir()
        (self.root / "sources" / "raw").mkdir(parents=True)
        (self.root / "sources" / "manifests").mkdir(parents=True)
        (self.root / "wiki" / "index.md").write_text("# Wiki index\n", encoding="utf-8")
        (self.root / "wiki" / "log.md").write_text("# Publication log\n", encoding="utf-8")
        shutil.copy2(REPO / ".gitignore", self.root / ".gitignore")
        shutil.copytree(REPO / ".opencode", self.root / ".opencode")

        source_id = "demo-workflow"
        source_file = self.caller / "source.md"
        source_file.write_text(
            "# Demo workflow\n\nReview is required before release.\n",
            encoding="utf-8",
        )
        responses_path = self.base / "fake-responses.json"
        captures_path = self.base / "fake-calls.jsonl"
        fake_executable = self.base / "fake-opencode"

        self._git("init", "-q")
        self._git("config", "user.name", "Wiki integration test")
        self._git("config", "user.email", "wiki-test@example.invalid")

        added = self._cli(
            "source",
            "add",
            str(source_file),
            "--id",
            source_id,
            "--kind",
            "workflow",
            "--origin",
            "synthetic:integration-test",
        )
        repeated = self._cli(
            "source",
            "add",
            str(source_file),
            "--id",
            source_id,
            "--kind",
            "workflow",
            "--origin",
            "synthetic:integration-test",
        )
        revision = added["revision"]
        self.assertEqual(repeated["revision"], revision)
        self.assertEqual(len(list((self.root / "sources" / "manifests").glob("*.json"))), 1)

        page_metadata = {
            "id": "demo-process",
            "title": "Demo process",
            "kind": "process",
            "domain": "demo",
            "review_status": "draft",
            "source_refs": [{"source_id": source_id, "revision": revision}],
            "depends_on": [],
            "reviewed_at": None,
        }
        proposed_page = (
            "---\n"
            + json.dumps(page_metadata, ensure_ascii=False, separators=(",", ":"))
            + "\n---\n"
            + "# Demo process\n\n"
            + "The registered workflow requires review before release.\n"
        )
        answer_before = {
            "scope": {
                "process": "demo process",
                "product": None,
                "environment": None,
                "version": None,
            },
            "summary": "Review is required before release.",
            "claims": [
                {
                    "claim_id": "review-required",
                    "text": "Review is required before release.",
                    "status": "supported",
                    "citation_ids": ["source-line-3"],
                }
            ],
            "citations": [
                {
                    "citation_id": "source-line-3",
                    "source_id": source_id,
                    "revision": revision,
                    "locator": "line:3",
                    "wiki_page": None,
                }
            ],
            "gaps": [],
            "conflicts": [],
        }
        answer_after = dict(answer_before)
        answer_after["summary"] = "The reviewed process page records the review step."
        answer_after["citations"] = [
            {
                "citation_id": "source-line-3",
                "source_id": source_id,
                "revision": revision,
                "locator": "line:3",
                "wiki_page": "wiki/processes/demo-process.md#demo-process",
            }
        ]
        answer_after["claims"] = [
            {
                "claim_id": "review-recorded",
                "text": "The process page records the review step.",
                "status": "supported",
                "citation_ids": ["source-line-3"],
            }
        ]
        maintenance_proposal = {
            "outcome": "proposed",
            "summary": "Add a sourced process page for the registered workflow.",
            "changes": [{"path": "wiki/processes/demo-process.md", "content": proposed_page}],
            "evidence": [
                {
                    "source_id": source_id,
                    "revision": revision,
                    "locator": "line:3",
                    "wiki_page": None,
                }
            ],
        }
        expected_stored_proposal = json.loads(json.dumps(maintenance_proposal))
        self.assertNotIn("authorship", maintenance_proposal["evidence"][0])
        self.assertNotIn("project", maintenance_proposal["evidence"][0])
        expected_stored_proposal["evidence"][0]["authorship"] = "unknown"
        expected_stored_proposal["evidence"][0]["project"] = None
        self.assertEqual(expected_stored_proposal["evidence"][0]["authorship"], "unknown")
        self.assertIsNone(expected_stored_proposal["evidence"][0]["project"])
        responses_path.write_text(
            json.dumps(
                {
                    "librarian": [answer_before, answer_after],
                    "wiki-maintainer": maintenance_proposal,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        _write_fake_opencode(fake_executable, responses_path, captures_path)
        _write_config(REPO / "wiki.toml", self.root / "wiki.toml", fake_executable)

        self._commit_all("Register workflow source")
        request = {
            "question": 'Where is review required?\n--model test/evil; $(touch pwned)',
            "scope": {"process": "demo process", "product": None, "environment": None, "version": None},
        }
        request_file = self.caller / "request.json"
        request_file.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        first_answer = self._cli("ask", "--request", str(request_file))
        self.assertEqual(first_answer["question"], request["question"])
        self.assertRegex(first_answer["wiki_revision"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(first_answer["citations"][0]["revision"], revision)

        feedback = {
            "feedback_id": "workflow-e2e-feedback",
            "answer_id": first_answer["answer_id"],
            "wiki_revision": first_answer["wiki_revision"],
            "target": "review-required",
            "description": "Preserve the cited review requirement in a process page.",
            "evidence": [
                {"source_id": source_id, "revision": revision, "locator": "line:3"}
            ],
            "suggested_correction": "Document the review step with its source revision.",
        }
        feedback_file = self.caller / "feedback.json"
        feedback_file.write_text(json.dumps(feedback, ensure_ascii=False), encoding="utf-8")
        submitted = self._cli("feedback", "submit", "--file", str(feedback_file))
        repeated_feedback = self._cli("feedback", "submit", "--file", str(feedback_file))
        self.assertEqual(submitted["job_id"], repeated_feedback["job_id"])
        self.assertFalse(submitted["duplicate"])
        self.assertTrue(repeated_feedback["duplicate"])

        proposal_result = self._cli("maintenance", "run", "--limit", "1")
        self.assertEqual(proposal_result["processed"], 1)
        job = proposal_result["items"][0]
        self.assertEqual(job["job_id"], submitted["job_id"])
        self.assertEqual(job["status"], "ready_for_review")
        target_page = self.root / "wiki" / "processes" / "demo-process.md"
        self.assertFalse(target_page.exists(), "maintenance must only save a proposal")
        proposal_file = self.root / job["proposal_path"]
        self.assertEqual(json.loads(proposal_file.read_text(encoding="utf-8")), expected_stored_proposal)

        target_page.write_text(proposed_page, encoding="utf-8")
        reviewed_revision = self._commit_all("Publish reviewed process page")
        completed = self._cli(
            "maintenance",
            "complete",
            "--id",
            submitted["job_id"],
            "--revision",
            reviewed_revision,
        )
        self.assertEqual(completed["status"], "resolved")
        self.assertEqual(completed["revision"], reviewed_revision)
        status = self._cli("feedback", "status", "--id", feedback["feedback_id"])
        self.assertEqual(status["status"], "resolved")
        self.assertEqual(status["published_revision"], reviewed_revision)

        second_answer = self._cli("ask", "--request", str(request_file))
        self.assertRegex(second_answer["wiki_revision"], r"^sha256:[0-9a-f]{64}$")
        self.assertNotEqual(first_answer["wiki_revision"], second_answer["wiki_revision"])
        self.assertEqual(second_answer["summary"], answer_after["summary"])
        self.assertEqual(
            second_answer["citations"][0]["wiki_page"],
            "wiki/processes/demo-process.md#demo-process",
        )

        captured_calls = [json.loads(line) for line in captures_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([item["agent"] for item in captured_calls], ["librarian", "wiki-maintainer", "librarian"])
        self.assertTrue(all(item["cwd"] == str(self.root) for item in captured_calls))
        self.assertTrue(all(item["args"][item["args"].index("--dir") + 1] == str(self.root) for item in captured_calls))
        self.assertEqual(captured_calls[0]["args"][-2:], ["--", captured_calls[0]["prompt"]])
        self.assertIn("$(touch pwned)", captured_calls[0]["prompt"])
        self.assertFalse((self.root / "pwned").exists())
        self.assertFalse((self.caller / "pwned").exists())


if __name__ == "__main__":
    unittest.main()
