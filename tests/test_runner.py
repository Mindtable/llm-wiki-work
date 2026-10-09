import json
import hashlib
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from wiki_tools.config import RunConfig
from wiki_tools.errors import WikiError
from wiki_tools.runner import ask, run_opencode
from wiki_tools.sources import add_source


def answer_json(summary="accepted"):
    return json.dumps(
        {
            "scope": {
                "process": None,
                "product": None,
                "environment": None,
                "version": None,
            },
            "summary": summary,
            "claims": [],
            "citations": [],
            "gaps": [],
            "conflicts": [],
        },
        separators=(",", ":"),
    )


def event(event_type, message_id=None, **fields):
    value = {"type": event_type, "timestamp": 1, "sessionID": "session-1"}
    if message_id is not None:
        value["part"] = {"id": f"part-{event_type}", "messageID": message_id}
        if event_type == "step_start":
            value["part"]["type"] = "step-start"
        elif event_type == "step_finish":
            value["part"]["type"] = "step-finish"
        elif event_type == "text":
            value["part"]["type"] = "text"
        elif event_type == "tool_use":
            value["part"].update({"type": "tool", "tool": "read", "state": {"status": "completed"}})
    if event_type == "text":
        value["part"].update({"text": fields.pop("text"), "time": {"start": 1, "end": 1}})
    if event_type == "step_finish":
        value["part"]["reason"] = fields.pop("reason")
    value.update(fields)
    return value


def stream_for(events, *, final_newline=True):
    text = "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in events)
    return text if final_newline else text[:-1]


def make_config(root: Path, executable: Path, *, timeout=5, max_steps=4):
    return RunConfig(
        root=root.resolve(),
        executable=str(executable),
        agent="librarian",
        model="test/fake",
        variant=None,
        timeout_seconds=timeout,
        max_steps=max_steps,
        purpose="ask",
        profile_prompt="trusted test profile",
    )


def write_fake(root: Path, output: str, *, status=0, stderr="", capture=True):
    executable = root / "fake-opencode"
    capture_path = root / "capture.json"
    source = (
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"capture = {str(capture_path)!r}\n"
        "if " + repr(capture) + ":\n"
        "    with open(capture, 'w', encoding='utf-8') as handle:\n"
        "        json.dump({"
        "'args': sys.argv[1:], 'cwd': os.getcwd(), "
        "'config': os.environ.get('OPENCODE_CONFIG_CONTENT'), "
        "'permission': os.environ.get('OPENCODE_PERMISSION'), "
        "'auto_share': os.environ.get('OPENCODE_AUTO_SHARE'), "
        "'disable_project_config': os.environ.get('OPENCODE_DISABLE_PROJECT_CONFIG'), "
        "'disable_default_plugins': os.environ.get('OPENCODE_DISABLE_DEFAULT_PLUGINS'), "
        "'has_provider_key': 'ANTHROPIC_API_KEY' in os.environ"
        "}, handle)\n"
        f"sys.stdout.write({output!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({status})\n"
    )
    executable.write_text(source, encoding="utf-8")
    executable.chmod(0o755)
    return executable, capture_path


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wiki-runner-")
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def configure_wiki(self, executable: Path):
        (self.root / ".opencode" / "agents").mkdir(parents=True, exist_ok=True)
        (self.root / "wiki").mkdir(exist_ok=True)
        (self.root / "sources").mkdir(exist_ok=True)
        (self.root / ".opencode" / "agents" / "librarian.md").write_text(
            "---\ndescription: test profile\nmode: primary\n---\nTrusted librarian instructions.\n",
            encoding="utf-8",
        )
        (self.root / "wiki.toml").write_text(
            "[ask]\n"
            f"executable = {json.dumps(str(executable))}\n"
            'agent = "librarian"\n'
            'model = "test/fake"\n'
            "timeout_seconds = 5\n"
            "max_steps = 4\n",
            encoding="utf-8",
        )

    def install_answer(self, model_answer: dict):
        events = [
            event("step_start", "msg-final"),
            event("text", "msg-final", text=json.dumps(model_answer, ensure_ascii=False, separators=(",", ":"))),
            event("step_finish", "msg-final", reason="stop"),
        ]
        executable, capture_path = write_fake(self.root, stream_for(events))
        self.configure_wiki(executable)
        return executable, capture_path

    def register_markdown_source(self):
        content = "# Approval\nCheck the release window.\n"
        revision = hashlib.sha256(content.encode("utf-8")).hexdigest()
        raw = self.root / "sources" / "raw" / "policy"
        raw.mkdir(parents=True)
        (raw / f"{revision}.md").write_text(content, encoding="utf-8")
        manifests = self.root / "sources" / "manifests"
        manifests.mkdir()
        manifest = {
            "source_id": "policy",
            "revision": revision,
            "kind": "procedure",
            "origin": "fixture",
            "upstream_revision": "",
            "sha256": revision,
            "captured_at": "2026-10-04T00:00:00Z",
            "local_path": f"sources/raw/policy/{revision}.md",
            "scope": {},
            "supersedes": None,
            "derived_from": [],
        }
        (manifests / f"policy--{revision}.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        page = self.root / "wiki" / "processes" / "policy.md"
        page.parent.mkdir(parents=True)
        page.write_text("# Approval\nVerified page.\n", encoding="utf-8")
        return revision

    def set_authorship(self, source_id, revision, authorship):
        manifest_path = self.root / "sources" / "manifests" / f"{source_id}--{revision}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if authorship is None:
            manifest.pop("authorship", None)
        else:
            manifest["authorship"] = authorship
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_prompt_is_one_literal_argument_and_runtime_policy_is_explicit(self):
        response = answer_json()
        events = [
            event("step_start", "msg-final"),
            event("text", "msg-final", text=response),
            event("step_finish", "msg-final", reason="stop"),
        ]
        executable, capture_path = write_fake(self.root, stream_for(events))
        prompt = "quote \"hello\"\n--model=evil; $(touch should-not-exist)"

        with patch.dict(
            os.environ,
            {
                "OPENCODE_CONFIG_CONTENT": "{\"agent\":{\"unsafe\":{}}}",
                "OPENCODE_CONFIG": "/tmp/caller-opencode.json",
                "OPENCODE_PERMISSION": "{\"*\":\"allow\"}",
                "OPENCODE_AUTO_SHARE": "true",
                "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
                "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
                "ANTHROPIC_API_KEY": "test-key-preserved",
            },
        ):
            result = run_opencode(self.root, make_config(self.root, executable), prompt)

        self.assertEqual(json.loads(response), result)
        capture = json.loads(capture_path.read_text(encoding="utf-8"))
        self.assertEqual(capture["cwd"], str(self.root))
        self.assertEqual(capture["args"][-2:], ["--", prompt])
        self.assertEqual(capture["permission"], None)
        self.assertEqual(capture["auto_share"], "false")
        self.assertEqual(capture["disable_project_config"], None)
        self.assertEqual(capture["disable_default_plugins"], "false")
        self.assertTrue(capture["has_provider_key"])
        overlay = json.loads(capture["config"])
        self.assertEqual(overlay["default_agent"], "librarian")
        self.assertEqual(overlay["share"], "disabled")
        profile = overlay["agent"]["librarian"]
        self.assertEqual(profile["mode"], "primary")
        self.assertEqual(profile["prompt"], "trusted test profile")
        self.assertEqual(profile["steps"], 4)
        self.assertEqual(profile["permission"]["*"], "deny")
        self.assertEqual(profile["permission"]["read"], "allow")
        self.assertEqual(profile["permission"]["skill"], "deny")
        self.assertFalse((self.root / "should-not-exist").exists())

    def test_ask_registers_a_raw_drop_before_the_librarian_process_starts(self):
        import hashlib

        dropped = self.root / "sources" / "raw" / "Approval notes.md"
        dropped.parent.mkdir(parents=True)
        dropped.write_text("Approval is required before payment.\n", encoding="utf-8")
        outside = self.root / "outside.md"
        outside.write_text("Untrusted linked approval file.\n", encoding="utf-8")
        try:
            (dropped.parent / "linked.md").symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        relative = dropped.relative_to(self.root).as_posix()
        source_id = "drop-" + hashlib.sha256(relative.encode("utf-8")).hexdigest()
        revision = hashlib.sha256(dropped.read_bytes()).hexdigest()
        manifest = self.root / "sources" / "manifests" / f"{source_id}--{revision}.json"
        answer = {
            "scope": {"process": None, "product": None, "environment": None, "version": None},
            "summary": "Approval is required before payment.",
            "claims": [],
            "citations": [{
                "citation_id": "approval-note",
                "source_id": source_id,
                "revision": revision,
                "locator": "line:1",
            }],
            "gaps": [],
            "conflicts": [],
        }
        events = [
            event("step_start", "msg-final"),
            event("text", "msg-final", text=json.dumps(answer, separators=(",", ":"))),
            event("step_finish", "msg-final", reason="stop"),
        ]
        executable = self.root / "fake-opencode"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            f"from pathlib import Path\n"
            f"manifest = Path({str(manifest)!r})\n"
            "if not manifest.is_file():\n"
            "    sys.stderr.write('raw manifest missing before model start\\n')\n"
            "    sys.exit(9)\n"
            f"sys.stdout.write({stream_for(events)!r})\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        self.configure_wiki(executable)

        result = ask(self.root, {"question": "What is required before payment?"})

        self.assertEqual(result["citations"][0]["source_id"], source_id)
        self.assertEqual(result["citations"][0]["revision"], revision)
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["kind"], "unclassified")

    def test_final_json_is_selected_from_the_last_completed_assistant_message(self):
        response = answer_json("last message")
        events = [
            event("step_start", "msg-tool"),
            event("text", "msg-tool", text=answer_json("intermediate")),
            event("step_finish", "msg-tool", reason="tool-calls"),
            event("tool_use", "msg-tool"),
            event("step_start", "msg-final"),
            event("text", "msg-final", text=response),
            event("step_finish", "msg-final", reason="stop"),
        ]
        executable, _ = write_fake(self.root, stream_for(events))

        result = run_opencode(self.root, make_config(self.root, executable), "question")

        self.assertEqual(result["summary"], "last message")

    def test_intermediate_text_without_a_final_stop_is_rejected(self):
        events = [
            event("step_start", "msg-tool"),
            event("text", "msg-tool", text=answer_json("intermediate")),
            event("step_finish", "msg-tool", reason="tool-calls"),
            event("tool_use", "msg-tool"),
        ]
        executable, _ = write_fake(self.root, stream_for(events))

        with self.assertRaises(WikiError) as raised:
            run_opencode(self.root, make_config(self.root, executable), "question")

        self.assertIn(raised.exception.code, {"truncated_output", "protocol_error"})

    def test_error_event_rejects_an_otherwise_valid_answer(self):
        events = [
            event("step_start", "msg-final"),
            event("text", "msg-final", text=answer_json()),
            event("step_finish", "msg-final", reason="stop"),
            event("error", error={"name": "ProviderError"}),
        ]
        executable, _ = write_fake(self.root, stream_for(events))

        with self.assertRaises(WikiError):
            run_opencode(self.root, make_config(self.root, executable), "question")

    def test_nonzero_process_exit_rejects_an_otherwise_valid_answer(self):
        events = [
            event("step_start", "msg-final"),
            event("text", "msg-final", text=answer_json()),
            event("step_finish", "msg-final", reason="stop"),
        ]
        executable, _ = write_fake(self.root, stream_for(events), status=7)

        with self.assertRaises(WikiError) as raised:
            run_opencode(self.root, make_config(self.root, executable), "question")

        self.assertEqual(raised.exception.code, "process_error")

    def test_truncated_ndjson_stream_is_rejected(self):
        events = [
            event("step_start", "msg-final"),
            event("text", "msg-final", text=answer_json()),
        ]
        executable, _ = write_fake(self.root, stream_for(events, final_newline=False))

        with self.assertRaises(WikiError) as raised:
            run_opencode(self.root, make_config(self.root, executable), "question")

        self.assertIn(raised.exception.code, {"truncated_output", "protocol_error"})

    def test_non_json_warning_is_rejected(self):
        executable, _ = write_fake(self.root, "warning: agent fallback\n")

        with self.assertRaises(WikiError) as raised:
            run_opencode(self.root, make_config(self.root, executable), "question")

        self.assertEqual(raised.exception.code, "protocol_error")

    def test_successful_event_stream_allows_stderr_diagnostics(self):
        events = [
            event("step_start", "msg-final"),
            event("text", "msg-final", text=answer_json()),
            event("step_finish", "msg-final", reason="stop"),
        ]
        executable, _ = write_fake(self.root, stream_for(events), stderr="OpenCode info: ready\n")

        result = run_opencode(self.root, make_config(self.root, executable), "question")

        self.assertEqual(result["summary"], "accepted")

    def test_invalid_executable_argument_becomes_a_process_error(self):
        config = make_config(self.root, Path("ignored"))
        config = RunConfig(
            root=config.root,
            executable="bad\\x00executable",
            agent=config.agent,
            model=config.model,
            variant=config.variant,
            timeout_seconds=config.timeout_seconds,
            max_steps=config.max_steps,
            purpose=config.purpose,
            profile_prompt=config.profile_prompt,
        )

        with self.assertRaises(WikiError) as raised:
            run_opencode(self.root, config, "question")

        self.assertEqual(raised.exception.code, "process_error")

    def test_ask_adds_trusted_id_and_revision_and_persists_answer(self):
        model_answer = json.loads(answer_json())
        executable, capture_path = self.install_answer(model_answer)
        question = 'Quote "this"\n--model=caller/override; $(touch no-file)'

        answer = ask(
            self.root,
            {"question": question, "scope": {"process": "onboarding"}},
        )

        self.assertEqual(answer["question"], question)
        self.assertRegex(answer["answer_id"], r"^[0-9a-f]{32}$")
        self.assertRegex(answer["wiki_revision"], r"^sha256:[0-9a-f]{64}$")
        record_path = self.root / ".state" / "answers" / f"{answer['answer_id']}.json"
        self.assertEqual(json.loads(record_path.read_text(encoding="utf-8")), answer)
        captured = json.loads(capture_path.read_text(encoding="utf-8"))
        prompt = captured["args"][-1]
        self.assertIn(json.dumps({"question": question, "scope": {"process": "onboarding", "product": None, "environment": None, "version": None}}, ensure_ascii=False, separators=(",", ":")), prompt)
        self.assertFalse((self.root / "no-file").exists())

    def test_ask_rejects_citation_to_missing_source_locator(self):
        revision = self.register_markdown_source()
        model_answer = json.loads(answer_json())
        model_answer["citations"] = [
            {
                "citation_id": "src1",
                "source_id": "policy",
                "revision": revision,
                "locator": "line:99",
                "wiki_page": "wiki/processes/policy.md#approval",
            }
        ]
        model_answer["claims"] = [
            {
                "claim_id": "claim1",
                "text": "A supported rule.",
                "status": "supported",
                "citation_ids": ["src1"],
            }
        ]
        self.install_answer(model_answer)

        with self.assertRaises(WikiError) as raised:
            ask(self.root, {"question": "What is the rule?"})

        self.assertEqual(raised.exception.code, "answer_validation_error")
        self.assertFalse((self.root / ".state" / "answers").exists())

    def test_ask_accepts_hash_verified_source_and_existing_wiki_anchor(self):
        revision = self.register_markdown_source()
        model_answer = json.loads(answer_json())
        model_answer["citations"] = [
            {
                "citation_id": "src1",
                "source_id": "policy",
                "revision": revision,
                "locator": "line:2",
                "wiki_page": "wiki/processes/policy.md#approval",
            }
        ]
        model_answer["claims"] = [
            {
                "claim_id": "claim1",
                "text": "Check the release window.",
                "status": "supported",
                "citation_ids": ["src1"],
            }
        ]
        self.install_answer(model_answer)

        answer = ask(self.root, {"question": "When should we check?"})

        self.assertEqual(answer["claims"][0]["citation_ids"], ["src1"])
        self.assertEqual(answer["citations"][0]["revision"], revision)

    def test_ask_enriches_citation_authorship_from_manifest_and_rejects_model_label(self):
        revision = self.register_markdown_source()
        self.set_authorship("policy", revision, "ai-generated")
        model_answer = json.loads(answer_json())
        model_answer["citations"] = [
            {
                "citation_id": "src1",
                "source_id": "policy",
                "revision": revision,
                "locator": "line:2",
                "wiki_page": "wiki/processes/policy.md#approval",
            }
        ]
        model_answer["claims"] = [
            {
                "claim_id": "claim1",
                "text": "The note says to check the release window.",
                "status": "supported",
                "citation_ids": ["src1"],
            }
        ]
        page_metadata = {
            "id": "policy",
            "title": "Policy",
            "kind": "process",
            "domain": "operations",
            "review_status": "reviewed",
            "source_refs": [{"source_id": "policy", "revision": revision}],
            "depends_on": [],
            "reviewed_at": "2026-10-09",
        }
        page = self.root / "wiki" / "processes" / "policy.md"
        page.write_text(
            "---\n" + json.dumps(page_metadata) + "\n---\n# Approval\nReviewed page.\n",
            encoding="utf-8",
        )
        self.install_answer(model_answer)

        answer = ask(self.root, {"question": "What does the note record?"})
        self.assertEqual(answer["citations"][0]["authorship"], "ai-generated")
        self.assertEqual(answer["citations"][0]["wiki_page"], "wiki/processes/policy.md#approval")

        model_answer["citations"][0]["authorship"] = "human-written"
        self.install_answer(model_answer)
        with self.assertRaises(WikiError) as raised:
            ask(self.root, {"question": "What does the note record?"})
        self.assertEqual(raised.exception.code, "answer_validation_error")

    def test_ask_keeps_mixed_authorship_distinct_across_citations(self):
        ai_revision = self.register_markdown_source()
        self.set_authorship("policy", ai_revision, "ai-generated")
        human_path = self.root / "human-policy.md"
        human_path.write_text("A human reviewer confirms the release window.\n", encoding="utf-8")
        human_source = add_source(self.root, human_path, source_id="human-policy", kind="procedure")
        self.set_authorship("human-policy", human_source["revision"], "human-written")

        model_answer = json.loads(answer_json())
        model_answer["citations"] = [
            {
                "citation_id": "ai1",
                "source_id": "policy",
                "revision": ai_revision,
                "locator": "line:2",
            },
            {
                "citation_id": "human1",
                "source_id": "human-policy",
                "revision": human_source["revision"],
                "locator": "line:1",
            },
        ]
        model_answer["claims"] = [
            {"claim_id": "claim-ai", "text": "AI note claim.", "status": "supported", "citation_ids": ["ai1"]},
            {"claim_id": "claim-human", "text": "Human note claim.", "status": "supported", "citation_ids": ["human1"]},
        ]
        self.install_answer(model_answer)

        answer = ask(self.root, {"question": "What does each source say?"})

        self.assertEqual(
            {citation["citation_id"]: citation["authorship"] for citation in answer["citations"]},
            {"ai1": "ai-generated", "human1": "human-written"},
        )

    def test_ask_rejects_non_string_claim_status(self):
        model_answer = json.loads(answer_json())
        model_answer["claims"] = [
            {
                "claim_id": "claim1",
                "text": "Malformed status.",
                "status": [],
                "citation_ids": [],
            }
        ]
        self.install_answer(model_answer)

        with self.assertRaises(WikiError) as raised:
            ask(self.root, {"question": "What is known?"})

        self.assertEqual(raised.exception.code, "answer_validation_error")

    def test_ask_rejects_knowledge_changed_during_query(self):
        executable, _ = self.install_answer(json.loads(answer_json()))
        fake_source = executable.read_text(encoding="utf-8")
        changed_page = self.root / "wiki" / "changed.md"
        fake_source = fake_source.replace(
            "sys.stdout.write(",
            f"open({str(changed_page)!r}, 'w', encoding='utf-8').write('changed')\nsys.stdout.write(",
            1,
        )
        executable.write_text(fake_source, encoding="utf-8")

        with self.assertRaises(WikiError) as raised:
            ask(self.root, {"question": "What is known?"})

        self.assertEqual(raised.exception.code, "knowledge_changed")

    def test_ask_ignores_finder_metadata_changes_during_query(self):
        dropped = self.root / "sources" / "raw" / "note.md"
        dropped.parent.mkdir(parents=True)
        dropped.write_text("A stable raw note.\n", encoding="utf-8")
        executable, _ = self.install_answer(json.loads(answer_json()))
        fake_source = executable.read_text(encoding="utf-8")
        service_file = dropped.parent / ".DS_Store"
        fake_source = fake_source.replace(
            "sys.stdout.write(",
            f"open({str(service_file)!r}, 'w', encoding='utf-8').write('Finder metadata')\nsys.stdout.write(",
            1,
        )
        executable.write_text(fake_source, encoding="utf-8")

        answer = ask(self.root, {"question": "What is in the note?"})

        self.assertEqual(answer["summary"], "accepted")
        self.assertEqual(service_file.read_text(encoding="utf-8"), "Finder metadata")

    def test_ask_ignores_temporary_files_and_hidden_directories_during_query(self):
        dropped = self.root / "sources" / "raw" / "note.md"
        dropped.parent.mkdir(parents=True)
        dropped.write_text("A stable raw note.\n", encoding="utf-8")
        executable, _ = self.install_answer(json.loads(answer_json()))
        fake_source = executable.read_text(encoding="utf-8")
        partial_file = dropped.parent / "copy.md.tmp"
        hidden_file = dropped.parent / ".copying" / "temporary.md"
        statement = (
            f"open({str(partial_file)!r}, 'w', encoding='utf-8').write('partial copy')\n"
            f"from pathlib import Path\nPath({str(hidden_file.parent)!r}).mkdir(exist_ok=True)\n"
            f"open({str(hidden_file)!r}, 'w', encoding='utf-8').write('hidden copy')\n"
            "sys.stdout.write("
        )
        fake_source = fake_source.replace("sys.stdout.write(", statement, 1)
        executable.write_text(fake_source, encoding="utf-8")

        answer = ask(self.root, {"question": "What is in the note?"})

        self.assertEqual(answer["summary"], "accepted")
        self.assertEqual(partial_file.read_text(encoding="utf-8"), "partial copy")
        self.assertEqual(hidden_file.read_text(encoding="utf-8"), "hidden copy")

    def test_ask_still_detects_changes_to_a_managed_source_snapshot(self):
        dropped = self.root / "sources" / "raw" / "note.md"
        dropped.parent.mkdir(parents=True)
        dropped.write_text("A stable raw note.\n", encoding="utf-8")
        relative = dropped.relative_to(self.root).as_posix()
        source_id = "drop-" + hashlib.sha256(relative.encode("utf-8")).hexdigest()
        revision = hashlib.sha256(dropped.read_bytes()).hexdigest()
        snapshot = self.root / "sources" / "raw" / source_id / f"{revision}.md"
        executable, _ = self.install_answer(json.loads(answer_json()))
        fake_source = executable.read_text(encoding="utf-8")
        fake_source = fake_source.replace(
            "sys.stdout.write(",
            f"open({str(snapshot)!r}, 'w', encoding='utf-8').write('changed snapshot')\nsys.stdout.write(",
            1,
        )
        executable.write_text(fake_source, encoding="utf-8")

        with self.assertRaises(WikiError) as raised:
            ask(self.root, {"question": "What is in the note?"})

        self.assertEqual(raised.exception.code, "knowledge_changed")

    def test_ask_rejects_caller_execution_overrides(self):
        with self.assertRaises(WikiError) as raised:
            ask(self.root, {"question": "question", "model": "provider/override"})

        self.assertEqual(raised.exception.code, "argument_error")

    def test_keyboard_interrupt_terminates_child_process_group(self):
        marker = self.root / "child-stopped"
        child_pid_file = self.root / "child.pid"
        runner_pid_file = self.root / "runner.pid"
        child_ready_file = self.root / "child-ready"
        executable = self.root / "fake-opencode"
        child_script = self.root / "fake-child.py"
        child_script.write_text(
            f"import signal,time\nmarker = {str(marker)!r}\nready = {str(child_ready_file)!r}\n"
            "def stop(*_):\n"
            "    open(marker, 'w', encoding='utf-8').write('stopped')\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "open(ready, 'w', encoding='utf-8').write('ready')\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        fake_source = (
            f"#!{sys.executable}\n"
            "import os,subprocess,sys,time\n"
            f"open({str(runner_pid_file)!r}, 'w').write(str(os.getpid()))\n"
            f"child = subprocess.Popen([sys.executable, {str(child_script)!r}])\n"
            f"open({str(child_pid_file)!r}, 'w').write(str(child.pid))\n"
            "time.sleep(60)\n"
        )
        executable.write_text(fake_source, encoding="utf-8")
        executable.chmod(0o755)
        wrapper = (
            "import pathlib,sys\n"
            "from wiki_tools.config import RunConfig\n"
            "from wiki_tools.runner import run_opencode\n"
            "root=pathlib.Path(sys.argv[1]).resolve()\n"
            "cfg=RunConfig(root=root, executable=sys.argv[2], agent='librarian', "
            "model='test/fake', variant=None, timeout_seconds=30, max_steps=4, purpose='ask')\n"
            "run_opencode(root,cfg,'question')\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", wrapper, str(self.root), str(executable)],
            cwd=str(self.root),
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 5
            while not child_ready_file.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(child_pid_file.exists(), "fake OpenCode child did not start")
            self.assertTrue(child_ready_file.exists(), "fake OpenCode child did not install its signal handler")
            process.send_signal(signal.SIGINT)
            process.wait(timeout=5)
            self.assertTrue(marker.exists(), "interrupted runner left its child alive")
        finally:
            process.kill()
            process.wait()
            if runner_pid_file.exists():
                try:
                    os.killpg(int(runner_pid_file.read_text(encoding="utf-8")), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_timeout_terminates_the_spawned_process_group(self):
        marker = self.root / "child-stopped"
        executable = self.root / "fake-opencode"
        source = (
            f"#!{sys.executable}\n"
            "import subprocess, sys, time\n"
            f"code = {('import signal,time; marker=' + repr(str(marker)) + '; '
                       'signal.signal(signal.SIGTERM, lambda *_: (open(marker, \'w\').write(\'stopped\'), '
                       '(_ for _ in ()).throw(SystemExit(0)))); time.sleep(60)')!r}\n"
            "subprocess.Popen([sys.executable, '-c', code])\n"
            "time.sleep(60)\n"
        )
        executable.write_text(source, encoding="utf-8")
        executable.chmod(0o755)

        with self.assertRaises(WikiError) as raised:
            run_opencode(self.root, make_config(self.root, executable, timeout=1), "question")

        self.assertEqual(raised.exception.code, "timeout_error")
        self.assertTrue(marker.exists())


if __name__ == "__main__":
    unittest.main()
