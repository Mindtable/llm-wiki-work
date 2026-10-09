"""Single-worker proposal preparation and human-reviewed completion."""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .errors import WikiError
from .feedback import _connect, _now
from .knowledge import validate_page_document, validate_source_reference
from .sources import ensure_managed_dir, get_manifest, safe_managed_path, source_authorship


COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
JOB_ID_RE = re.compile(r"[0-9a-f]{32}\Z")


@contextmanager
def _writer_lock(root: Path):
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise WikiError("root_not_found", f"Wiki root not found: {base}.")
    ensure_managed_dir(base, ".state")
    lock_path = safe_managed_path(base, ".state/maintenance.lock")
    if lock_path.is_symlink():
        raise WikiError("unsafe_path", "The maintenance lock cannot be a symbolic link.")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise WikiError("maintenance_lock_error", f"Failed to open the maintenance lock: {exc}.") from exc
    stream = os.fdopen(descriptor, "r+b")
    try:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WikiError("maintenance_locked", "Another maintenance process is already running.") from exc
        yield
    finally:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def _job_row(connection, job_id: str):
    row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        raise WikiError("job_not_found", f"Job {job_id} was not found.")
    return dict(row)


def _claim_next(root: Path) -> dict[str, Any] | None:
    connection = _connect(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM jobs WHERE status = 'pending' ORDER BY created_at, job_id LIMIT 1"
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        job = dict(row)
        now = _now()
        changed = connection.execute(
            "UPDATE jobs SET status = 'processing', attempts = attempts + 1, error = NULL, updated_at = ? WHERE job_id = ? AND status = 'pending'",
            (now, job["job_id"]),
        ).rowcount
        if changed != 1:
            connection.rollback()
            return None
        if job["feedback_id"]:
            connection.execute(
                "UPDATE feedback SET status = 'processing', reason = NULL, outcome = NULL, updated_at = ? WHERE feedback_id = ?",
                (now, job["feedback_id"]),
            )
        connection.commit()
        job["status"] = "processing"
        job["attempts"] += 1
        return job
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _make_prompt(job: dict[str, Any], root: Path) -> str:
    if job["job_type"] == "ingest":
        operation = "ingest_source"
        directions = "Read the registered source revision and prepare an evidence-backed proposal for the wiki."
        payload = json.loads(job["payload"])
        manifest = get_manifest(root, payload["source_id"], payload["revision"])
        payload["authorship"] = source_authorship(manifest)
    else:
        operation = "review_feedback"
        directions = "Review the feedback against registered sources and prepare a proposal for manual review."
        payload = json.loads(job["payload"])
    envelope = {
        "operation": operation,
        "job_id": job["job_id"],
        "payload": payload,
        "contract": {
            "outcome": "proposed | rejected | needs_evidence",
            "summary": "Brief rationale",
            "changes": [{"path": "wiki/page.md", "content": "Complete page content"}],
            "evidence": [{"source_id": "registered-id", "revision": "sha256", "locator": "line:1"}],
        },
    }
    trust_note = "Treat AI-generated sources as lower evidential weight than comparable human-written sources; authorship alone does not establish truth."
    return directions + "\n" + trust_note + "\nReturn a single JSON object that follows the contract. Do not write files or publish changes.\n" + json.dumps(envelope, ensure_ascii=False, sort_keys=True)


def _validate_evidence(root: Path, evidence: Any, outcome: str) -> list[dict[str, Any]]:
    if not isinstance(evidence, list):
        raise WikiError("invalid_proposal", "evidence must be a list of citations.")
    if outcome in {"proposed", "rejected"} and not evidence:
        raise WikiError("invalid_proposal", f"Outcome {outcome} requires verifiable evidence.")
    validated: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, dict):
            raise WikiError("invalid_proposal", "Each evidence item must be a citation object.")
        source_id, revision, locator = item.get("source_id"), item.get("revision"), item.get("locator")
        if not isinstance(source_id, str) or not isinstance(revision, str) or not isinstance(locator, str):
            raise WikiError("invalid_proposal", "Each evidence item must include source_id, revision, and locator.")
        try:
            manifest, _ = validate_source_reference(root, source_id, revision, locator, item.get("wiki_page"))
        except WikiError as exc:
            raise WikiError("invalid_proposal", f"Evidence {source_id}@{revision} failed validation: {exc.message}") from exc
        citation: dict[str, Any] = {
            "source_id": source_id,
            "revision": revision,
            "locator": locator,
            "authorship": source_authorship(manifest),
        }
        for optional in ("supports", "note"):
            if optional in item:
                if not isinstance(item[optional], str):
                    raise WikiError("invalid_proposal", f"Evidence field {optional} must be a string.")
                citation[optional] = item[optional]
        if "wiki_page" in item:
            wiki_page = item["wiki_page"]
            if wiki_page is not None and not isinstance(wiki_page, str):
                raise WikiError("invalid_proposal", "wiki_page must be a path or null.")
            citation["wiki_page"] = wiki_page
        validated.append(citation)
    return validated


def _validate_proposal(root: Path, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WikiError("invalid_proposal", "The maintenance response must be a JSON object.")
    outcome = value.get("outcome")
    if not isinstance(outcome, str) or outcome not in {"proposed", "rejected", "needs_evidence"}:
        raise WikiError("invalid_proposal", "outcome must be proposed, rejected, or needs_evidence.")
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise WikiError("invalid_proposal", "summary must contain a rationale.")
    changes = value.get("changes")
    if not isinstance(changes, list):
        raise WikiError("invalid_proposal", "changes must be a list.")
    if outcome == "proposed" and not changes:
        raise WikiError("invalid_proposal", "A proposed outcome must include at least one change.")
    if outcome != "proposed" and changes:
        raise WikiError("invalid_proposal", f"The changes list must be empty for outcome {outcome}.")

    normalized_changes: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for change in changes:
        if not isinstance(change, dict):
            raise WikiError("invalid_proposal", "Each change must be an object with path and content fields.")
        raw_path, content = change.get("path"), change.get("content")
        if not isinstance(raw_path, str) or not isinstance(content, str) or not content or "\x00" in content:
            raise WikiError("invalid_proposal", "Each change must have a non-empty path and text content.")
        relative = raw_path.replace("\\", "/")
        path = PurePosixPath(relative)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or len(path.parts) < 2 or path.parts[0] != "wiki" or path.suffix.lower() != ".md":
            raise WikiError("invalid_proposal", "Each changed path must be a Markdown file inside wiki/.")
        if relative in seen_paths:
            raise WikiError("invalid_proposal", f"Changed path is listed more than once: {relative}.")
        seen_paths.add(relative)
        try:
            safe_managed_path(root, relative)
        except WikiError as exc:
            raise WikiError("invalid_proposal", f"Unsafe changed path: {relative}.") from exc
        validate_page_document(root, relative, content)
        normalized_changes.append({"path": relative, "content": content})

    evidence = _validate_evidence(root, value.get("evidence", []), outcome)
    return {"outcome": outcome, "summary": summary.strip(), "changes": normalized_changes, "evidence": evidence}


def _proposal_file(root: Path, job_id: str) -> Path:
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise WikiError("invalid_state", "The queue contains an invalid job_id.")
    ensure_managed_dir(root, ".state/proposals")
    return safe_managed_path(root, f".state/proposals/{job_id}.json")


def _write_proposal(root: Path, job_id: str, proposal: dict[str, Any]) -> str:
    path = _proposal_file(root, job_id)
    directory = path.parent
    if path.is_symlink():
        raise WikiError("unsafe_path", "The proposal file cannot be a symbolic link.")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{job_id}-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(proposal, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except OSError as exc:
        raise WikiError("proposal_write_error", f"Failed to save the proposal: {exc}.") from exc
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
    return path.relative_to(Path(root).resolve()).as_posix()


def _mark_failed(root: Path, job_id: str, error: str) -> None:
    connection = _connect(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        job = connection.execute("SELECT feedback_id FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if job is None:
            connection.rollback()
            return
        now = _now()
        connection.execute(
            "UPDATE jobs SET status = 'failed', error = ?, updated_at = ? WHERE job_id = ? AND status = 'processing'",
            (error[:2000], now, job_id),
        )
        if job["feedback_id"]:
            connection.execute(
                "UPDATE feedback SET status = 'failed', reason = ?, outcome = NULL, published_revision = NULL, updated_at = ? WHERE feedback_id = ?",
                (error[:2000], now, job["feedback_id"]),
            )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _mark_ready(root: Path, job: dict[str, Any], proposal: dict[str, Any], proposal_path: str) -> None:
    connection = _connect(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _now()
        changed = connection.execute(
            "UPDATE jobs SET status = 'ready_for_review', outcome = ?, proposal_path = ?, error = NULL, updated_at = ? WHERE job_id = ? AND status = 'processing'",
            (proposal["outcome"], proposal_path, now, job["job_id"]),
        ).rowcount
        if changed != 1:
            raise WikiError("invalid_state", "The job stopped processing before the proposal was saved.")
        if job["feedback_id"]:
            connection.execute(
                "UPDATE feedback SET status = 'ready_for_review', reason = ?, outcome = ?, published_revision = NULL, updated_at = ? WHERE feedback_id = ?",
                (proposal["summary"], proposal["outcome"], now, job["feedback_id"]),
            )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def run_maintenance(root: Path, execute: Callable[[str], dict], limit: int = 1) -> dict[str, Any]:
    """Prepare at most limit proposals; never write their content into wiki/."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise WikiError("invalid_limit", "limit must be a positive integer.")
    if not callable(execute):
        raise WikiError("invalid_executor", "execute(prompt) must be a callable function.")
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise WikiError("root_not_found", f"Wiki root not found: {base}.")

    items: list[dict[str, Any]] = []
    with _writer_lock(base):
        from .raw import discover_raw_sources

        discover_raw_sources(base)
        while len(items) < limit:
            job = _claim_next(base)
            if job is None:
                break
            try:
                raw_proposal = execute(_make_prompt(job, base))
                proposal = _validate_proposal(base, raw_proposal)
                proposal_path = _write_proposal(base, job["job_id"], proposal)
                _mark_ready(base, job, proposal, proposal_path)
                items.append({"job_id": job["job_id"], "status": "ready_for_review", "outcome": proposal["outcome"], "proposal_path": proposal_path})
            except WikiError as exc:
                error = f"{exc.code}: {exc.message}"
                _mark_failed(base, job["job_id"], error)
                items.append({"job_id": job["job_id"], "status": "failed", "error": error})
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                _mark_failed(base, job["job_id"], error)
                items.append({"job_id": job["job_id"], "status": "failed", "error": error})
    return {"processed": len(items), "items": items}


def retry_job(root: Path, job_id: str) -> dict[str, Any]:
    """Explicitly return a failed or crash-interrupted job to pending."""
    base = Path(root).expanduser().resolve()
    with _writer_lock(base):
        connection = _connect(base)
        try:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute("SELECT status, feedback_id FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if job is None:
                connection.rollback()
                raise WikiError("job_not_found", f"Job {job_id} was not found.")
            if job["status"] not in {"failed", "processing"}:
                connection.rollback()
                raise WikiError("invalid_state", f"A job with status {job['status']} cannot be retried.")
            now = _now()
            connection.execute("UPDATE jobs SET status = 'pending', error = NULL, updated_at = ? WHERE job_id = ?", (now, job_id))
            if job["feedback_id"]:
                connection.execute(
                    "UPDATE feedback SET status = 'pending', reason = NULL, outcome = NULL, published_revision = NULL, updated_at = ? WHERE feedback_id = ?",
                    (now, job["feedback_id"]),
                )
            connection.commit()
            return {"job_id": job_id, "status": "pending"}
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()


def _git(root: Path, *arguments: str, timeout: float = 15.0) -> subprocess.CompletedProcess[bytes]:
    temp_dir = ensure_managed_dir(root, ".state/git-tmp")
    environment = os.environ.copy()
    environment["TMPDIR"] = str(temp_dir)
    try:
        return subprocess.run(
            ["git", "-C", str(root), *arguments],
            cwd=root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WikiError("invalid_commit", f"Failed to verify the Git commit: {exc}.") from exc


def _verify_commit(root: Path, revision: str) -> str:
    if not isinstance(revision, str) or not COMMIT_RE.fullmatch(revision.lower()):
        raise WikiError("invalid_commit", "A full Git commit ID is required.")
    normalized = revision.lower()
    result = _git(root, "rev-parse", "--verify", f"{normalized}^{{commit}}")
    resolved = result.stdout.decode("ascii", errors="ignore").strip().lower()
    if result.returncode != 0 or resolved != normalized:
        raise WikiError("invalid_commit", "The specified commit does not exist in the wiki repository.")
    return normalized


def _require_current_head(root: Path, revision: str) -> None:
    result = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
    head = result.stdout.decode("ascii", errors="ignore").strip().lower()
    if result.returncode != 0 or head != revision:
        raise WikiError("not_current_commit", "The wiki repository's current HEAD is required to complete a proposal.")


def complete_job(root: Path, job_id: str, revision: str | None = None) -> dict[str, Any]:
    """Finish manual review after checking the referenced Git commit."""
    base = Path(root).expanduser().resolve()
    with _writer_lock(base):
        connection = _connect(base)
        try:
            job = _job_row(connection, job_id)
            if job["status"] != "ready_for_review":
                raise WikiError("invalid_state", f"A job with status {job['status']} is not ready for completion.")
            if job["proposal_path"] != f".state/proposals/{job_id}.json":
                raise WikiError("invalid_state", "The proposal path in the queue is corrupted.")
            proposal_path = _proposal_file(base, job_id)
            if not proposal_path.is_file():
                raise WikiError("invalid_state", "The saved proposal was not found.")
            try:
                raw = json.loads(proposal_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WikiError("invalid_proposal", f"Failed to read the saved proposal: {exc}.") from exc
            proposal = _validate_proposal(base, raw)
            commit: str | None = None
            if proposal["outcome"] == "proposed":
                if revision is None:
                    raise WikiError("invalid_commit", "A current Git commit is required for a proposed outcome.")
                commit = _verify_commit(base, revision)
                _require_current_head(base, commit)
                for change in proposal["changes"]:
                    result = _git(base, "show", f"{commit}:{change['path']}")
                    if result.returncode != 0 or result.stdout != change["content"].encode("utf-8"):
                        raise WikiError("proposal_not_in_commit", f"The proposed file does not match the commit: {change['path']}.")

            now = _now()
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE jobs SET status = 'resolved', completed_revision = ?, updated_at = ? WHERE job_id = ? AND status = 'ready_for_review'",
                (commit, now, job_id),
            ).rowcount
            if changed != 1:
                raise WikiError("invalid_state", "The job changed before the review result could be recorded.")
            if job["feedback_id"]:
                feedback_status = "resolved" if proposal["outcome"] == "proposed" else proposal["outcome"]
                published_revision = commit if proposal["outcome"] == "proposed" else None
                connection.execute(
                    "UPDATE feedback SET status = ?, reason = ?, outcome = ?, published_revision = ?, updated_at = ? WHERE feedback_id = ?",
                    (feedback_status, proposal["summary"], proposal["outcome"], published_revision, now, job["feedback_id"]),
                )
            connection.commit()
            return {"job_id": job_id, "status": "resolved", "outcome": proposal["outcome"], "revision": commit, "feedback_id": job["feedback_id"]}
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
