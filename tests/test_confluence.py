import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from wiki_tools.confluence import discover_confluence_links, sync_confluence
from wiki_tools.feedback import _connect
from wiki_tools.sources import get_manifest
from wiki_tools import sources as sources_module


class ConfluenceLinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wiki-confluence-")
        self.root = Path(self.temp.name).resolve()
        (self.root / "sources" / "raw").mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    @property
    def links(self):
        return self.root / "sources" / "raw" / "human-written" / "confluence"

    def test_discovers_markdown_and_plain_links_and_deduplicates_by_page_identity(self):
        self.links.mkdir(parents=True)
        first = self.links / "team.md"
        second = self.links / "other.txt"
        first.write_text(
            "# Team pages\n"
            "- https://example.atlassian.net/wiki/spaces/OPS/pages/123/Original-title?view=1#Overview\n"
            "- [Same page](https://example.atlassian.net/wiki/pages/viewpage.action?pageId=123&focusedCommentId=8#Comments)\n",
            encoding="utf-8",
        )
        second.write_text(
            "https://example.atlassian.net/wiki/spaces/OPS/pages/123/Renamed-title?other=2\n",
            encoding="utf-8",
        )

        result = discover_confluence_links(self.root)

        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["pages"]), 1)
        page = result["pages"][0]
        self.assertEqual(page["site"], "https://example.atlassian.net/wiki")
        self.assertEqual(page["page_id"], "123")
        self.assertEqual(page["url"], "https://example.atlassian.net/wiki/pages/viewpage.action?pageId=123")
        self.assertEqual(page["source_id"], "confluence-" + hashlib.sha256(b"https://example.atlassian.net/wiki\n123").hexdigest())
        self.assertEqual(
            page["inputs"],
            [
                {"path": "sources/raw/human-written/confluence/other.txt", "line": 1},
                {"path": "sources/raw/human-written/confluence/team.md", "line": 2},
                {"path": "sources/raw/human-written/confluence/team.md", "line": 3},
            ],
        )

    def test_malformed_and_binary_lists_report_errors_while_valid_links_survive(self):
        self.links.mkdir(parents=True)
        (self.links / "valid.md").write_text(
            "- [Runbook](https://example.atlassian.net/wiki/spaces/OPS/pages/44/Runbook)\n"
            "https://example.atlassian.net/wiki/short/44\n",
            encoding="utf-8",
        )
        (self.links / "binary.bin").write_bytes(b"\xff\x00")

        result = discover_confluence_links(self.root)

        self.assertEqual(len(result["pages"]), 1)
        self.assertEqual(len(result["errors"]), 2)
        self.assertEqual(
            [(item["path"], item.get("line"), item["code"]) for item in result["errors"]],
            [
                ("sources/raw/human-written/confluence/binary.bin", None, "invalid_utf8"),
                ("sources/raw/human-written/confluence/valid.md", 2, "unsupported_confluence_url"),
            ],
        )

    def test_empty_link_directory_is_a_read_only_noop(self):
        self.links.mkdir(parents=True)

        result = sync_confluence(self.root, lambda _prompt: self.fail("empty folder must not fetch"))

        self.assertEqual(result, {"checked": 0, "changed": 0, "unchanged": 0, "items": [], "errors": []})
        self.assertFalse((self.root / ".state").exists())

    def test_missing_link_directory_is_a_read_only_noop(self):
        result = sync_confluence(self.root, lambda _prompt: self.fail("missing folder must not fetch"))
        self.assertEqual(result["checked"], 0)
        self.assertFalse((self.root / ".state").exists())

    def test_symlinked_inputs_are_not_followed_and_hidden_or_temporary_files_are_ignored(self):
        self.links.mkdir(parents=True)
        outside = self.root / "outside.md"
        outside.write_text("https://example.atlassian.net/wiki/spaces/OPS/pages/9/Outside\n", encoding="utf-8")
        (self.links / ".hidden.md").write_text("not a link\n", encoding="utf-8")
        (self.links / "unfinished.md.tmp").write_text("not a link\n", encoding="utf-8")
        try:
            (self.links / "linked.md").symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        result = discover_confluence_links(self.root)

        self.assertEqual(result["pages"], [])
        self.assertEqual([(item["code"], item["path"]) for item in result["errors"]], [
            ("unsafe_input", "sources/raw/human-written/confluence/linked.md")
        ])


class ConfluenceSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wiki-confluence-sync-")
        self.root = Path(self.temp.name).resolve()
        (self.root / "wiki").mkdir()
        self.links = self.root / "sources" / "raw" / "human-written" / "confluence"
        self.links.mkdir(parents=True)
        (self.links / "pages.md").write_text(
            "https://example.atlassian.net/wiki/spaces/OPS/pages/123/Original-title\n",
            encoding="utf-8",
        )
        self.page_url = "https://example.atlassian.net/wiki/pages/viewpage.action?pageId=123"

    def tearDown(self):
        self.temp.cleanup()

    def response(self, *, title="First title", content="First body\n", version="1", url=None, page_id="123"):
        return {
            "status": "ok",
            "url": url or self.page_url,
            "page_id": page_id,
            "title": title,
            "version": version,
            "content": content,
        }

    def jobs(self):
        connection = _connect(self.root)
        try:
            return [dict(row) for row in connection.execute(
                "SELECT job_id, source_id, revision, status, error FROM jobs WHERE job_type = 'ingest' ORDER BY created_at, job_id"
            ).fetchall()]
        finally:
            connection.close()

    def test_first_sync_registers_human_confluence_export_and_queues_its_snapshot(self):
        report = sync_confluence(self.root, lambda _prompt: self.response())

        self.assertEqual((report["checked"], report["changed"], report["unchanged"]), (1, 1, 0))
        item = report["items"][0]
        self.assertEqual(item["status"], "updated")
        manifest = get_manifest(self.root, item["source_id"], item["revision"])
        self.assertEqual(manifest["kind"], "confluence_export")
        self.assertEqual(manifest["authorship"], "human-written")
        self.assertEqual(manifest["origin"], self.page_url)
        self.assertEqual(manifest["upstream_revision"], "1")
        self.assertIsNone(sources_module.source_project(manifest))
        snapshot = (self.root / manifest["local_path"]).read_text(encoding="utf-8")
        self.assertIn("First title", snapshot)
        self.assertIn("First body\n", snapshot)
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.jobs()[0]["revision"], item["revision"])

    def test_unchanged_content_still_checks_but_does_not_create_revision_or_job(self):
        execute_calls = []

        def execute(prompt):
            execute_calls.append(prompt)
            return self.response()

        first = sync_confluence(self.root, execute)
        second = sync_confluence(self.root, execute)

        self.assertEqual(len(execute_calls), 2)
        self.assertEqual(second["checked"], 1)
        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(second["items"][0]["revision"], first["items"][0]["revision"])
        self.assertTrue(all("only this exact Confluence page" in prompt for prompt in execute_calls))
        self.assertEqual(len(self.jobs()), 1)

    def test_changed_then_reverted_content_creates_a_forward_revision_chain(self):
        first = sync_confluence(self.root, lambda _prompt: self.response())
        second = sync_confluence(self.root, lambda _prompt: self.response(title="Second title", content="Second body\n", version="2"))
        third = sync_confluence(self.root, lambda _prompt: self.response(version="3"))

        self.assertEqual((first["changed"], second["changed"], third["changed"]), (1, 1, 1))
        self.assertEqual(len({first["items"][0]["revision"], second["items"][0]["revision"], third["items"][0]["revision"]}), 3)
        current = get_manifest(self.root, third["items"][0]["source_id"], third["items"][0]["revision"])
        self.assertEqual(current["supersedes"], second["items"][0]["revision"])
        self.assertEqual((self.root / current["local_path"]).read_text(encoding="utf-8").count("First body"), 1)
        self.assertEqual(len(self.jobs()), 3)

    def test_version_only_change_and_missing_local_status_reuse_current_snapshot(self):
        first = sync_confluence(self.root, lambda _prompt: self.response(version="1"))
        state_path = self.root / ".state" / "confluence-sync.json"
        self.assertTrue(state_path.is_file())
        state_path.unlink()
        second = sync_confluence(self.root, lambda _prompt: self.response(version="99"))

        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(second["items"][0]["revision"], first["items"][0]["revision"])
        self.assertEqual(len(self.jobs()), 1)
        manifest = get_manifest(self.root, second["items"][0]["source_id"], second["items"][0]["revision"])
        self.assertEqual(manifest["upstream_revision"], "1")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["pages"][second["items"][0]["source_id"]]["version"], "99")

    def test_crlf_page_text_remains_unchanged_after_snapshot_reload(self):
        content = "First line\r\nSecond line\r\n"
        first = sync_confluence(self.root, lambda _prompt: self.response(content=content))
        second = sync_confluence(self.root, lambda _prompt: self.response(content=content))

        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(second["items"][0]["revision"], first["items"][0]["revision"])
        manifest = get_manifest(self.root, second["items"][0]["source_id"], second["items"][0]["revision"])
        self.assertIn(content, (self.root / manifest["local_path"]).read_bytes().decode("utf-8"))

    def test_empty_page_content_is_a_valid_change_and_then_reuses_its_snapshot(self):
        first = sync_confluence(self.root, lambda _prompt: self.response(content="Existing text\n"))
        emptied = sync_confluence(self.root, lambda _prompt: self.response(title="Now empty", content="", version="2"))
        repeated = sync_confluence(self.root, lambda _prompt: self.response(title="Now empty", content="", version="3"))

        self.assertEqual(emptied["changed"], 1)
        self.assertEqual(repeated["unchanged"], 1)
        self.assertNotEqual(emptied["items"][0]["revision"], first["items"][0]["revision"])
        manifest = get_manifest(self.root, repeated["items"][0]["source_id"], repeated["items"][0]["revision"])
        snapshot = (self.root / manifest["local_path"]).read_bytes().decode("utf-8")
        self.assertTrue(snapshot.endswith("# Now empty\n\n"))
        self.assertEqual(len(self.jobs()), 2)

    def test_binary_or_unencodable_page_text_is_rejected_before_registering_evidence(self):
        for content in ("body\x00text", "body\ud800text"):
            with self.subTest(content=repr(content)):
                report = sync_confluence(self.root, lambda _prompt, body=content: self.response(content=body))
                self.assertEqual(report["items"][0]["status"], "failed")
                self.assertEqual(report["items"][0]["error"]["code"], "invalid_confluence_response")
                self.assertEqual(self.jobs(), [])
                self.assertFalse((self.root / "sources" / "manifests").exists())

    def test_malformed_local_page_status_does_not_stop_syncing_other_valid_pages(self):
        self.links.joinpath("second.md").write_text(
            "https://example.atlassian.net/wiki/spaces/OPS/pages/456/Second\n", encoding="utf-8"
        )
        initial = sync_confluence(self.root, lambda prompt: self.response(page_id="123") if "pageId=123" in prompt else {
            "status": "ok",
            "url": "https://example.atlassian.net/wiki/pages/viewpage.action?pageId=456",
            "page_id": "456",
            "title": "Second",
            "version": "1",
            "content": "Second body",
        })
        state_path = self.root / ".state" / "confluence-sync.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["pages"][initial["items"][0]["source_id"]] = "malformed-record"
        state_path.write_text(json.dumps(state), encoding="utf-8")

        def fail_first(prompt):
            if "pageId=123" in prompt:
                return {"status": "error", "message": "temporarily unavailable"}
            return {
                "status": "ok",
                "url": "https://example.atlassian.net/wiki/pages/viewpage.action?pageId=456",
                "page_id": "456",
                "title": "Second",
                "version": "2",
                "content": "Second body",
            }

        report = sync_confluence(self.root, fail_first)

        self.assertEqual([item["status"] for item in report["items"]], ["failed", "unchanged"])
        self.assertEqual(report["checked"], 2)

    def test_fetch_failure_or_identity_mismatch_does_not_change_source_evidence(self):
        failed = sync_confluence(self.root, lambda _prompt: {"status": "error", "message": "offline"})
        self.assertEqual(failed["items"][0]["status"], "failed")
        self.assertEqual(failed["changed"], 0)
        self.assertFalse((self.root / "sources" / "manifests").exists())
        self.assertEqual(self.jobs(), [])

        mismatch = sync_confluence(self.root, lambda _prompt: self.response(page_id="999"))
        self.assertEqual(mismatch["items"][0]["status"], "failed")
        self.assertIn("page identity", mismatch["items"][0]["error"]["message"].lower())
        self.assertFalse((self.root / "sources" / "manifests").exists())
        self.assertEqual(self.jobs(), [])

    def test_successful_update_supersedes_only_old_unfinished_ingest_jobs(self):
        sync_confluence(self.root, lambda _prompt: self.response())
        job_a = self.jobs()[0]
        second = sync_confluence(self.root, lambda _prompt: self.response(title="Second", content="Second\n", version="2"))
        job_b = next(job for job in self.jobs() if job["revision"] == second["items"][0]["revision"])
        third = sync_confluence(self.root, lambda _prompt: self.response(title="Third", content="Third\n", version="3"))
        job_c = next(job for job in self.jobs() if job["revision"] == third["items"][0]["revision"])

        connection = _connect(self.root)
        try:
            connection.execute("UPDATE jobs SET status = 'resolved' WHERE job_id = ?", (job_a["job_id"],))
            connection.execute("UPDATE jobs SET status = 'pending' WHERE job_id = ?", (job_b["job_id"],))
            connection.execute("UPDATE jobs SET status = 'ready_for_review' WHERE job_id = ?", (job_c["job_id"],))
        finally:
            connection.close()
        fourth = sync_confluence(self.root, lambda _prompt: self.response(title="Fourth", content="Fourth\n", version="4"))

        by_id = {job["job_id"]: job for job in self.jobs()}
        self.assertEqual(by_id[job_a["job_id"]]["status"], "resolved")
        self.assertEqual(by_id[job_b["job_id"]]["status"], "failed")
        self.assertEqual(by_id[job_c["job_id"]]["status"], "failed")
        self.assertEqual(by_id[fourth["items"][0]["job_id"]]["status"], "pending")
        self.assertEqual(set(fourth["items"][0]["superseded_job_ids"]), {job_b["job_id"], job_c["job_id"]})
        self.assertIn(fourth["items"][0]["revision"], by_id[job_b["job_id"]]["error"])
        self.assertIn(fourth["items"][0]["job_id"], by_id[job_c["job_id"]]["error"])

    def test_unchanged_sync_also_clears_stale_pending_jobs_without_duplicate_ingest(self):
        first = sync_confluence(self.root, lambda _prompt: self.response())
        old_job = self.jobs()[0]
        second = sync_confluence(self.root, lambda _prompt: self.response(title="Second", content="Second\n", version="2"))
        connection = _connect(self.root)
        try:
            connection.execute("UPDATE jobs SET status = 'pending' WHERE job_id = ?", (old_job["job_id"],))
        finally:
            connection.close()

        unchanged = sync_confluence(self.root, lambda _prompt: self.response(title="Second", content="Second\n", version="2"))

        self.assertEqual(unchanged["unchanged"], 1)
        self.assertEqual(unchanged["items"][0]["revision"], second["items"][0]["revision"])
        self.assertEqual(unchanged["items"][0]["superseded_job_ids"], [old_job["job_id"]])
        self.assertEqual(next(job for job in self.jobs() if job["job_id"] == old_job["job_id"])["status"], "failed")
        self.assertEqual(len(self.jobs()), 2)


if __name__ == "__main__":
    unittest.main()
