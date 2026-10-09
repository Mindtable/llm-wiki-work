"""Durable feedback and ingest jobs backed by the local SQLite queue."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import WikiError
from .sources import SOURCE_ID_RE, ensure_managed_dir, get_manifest, safe_managed_path, source_authorship


FEEDBACK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
JOB_STATUSES = {"pending", "processing", "ready_for_review", "failed", "resolved"}
FEEDBACK_STATUSES = {"pending", "processing", "ready_for_review", "resolved", "rejected", "needs_evidence", "failed"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise WikiError("invalid_payload", f"Payload must be JSON-serializable: {exc}.") from exc


def _queue_path(root: Path) -> Path:
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise WikiError("root_not_found", f"Wiki root not found: {base}.")
    ensure_managed_dir(base, ".state")
    path = safe_managed_path(base, ".state/queue.sqlite3")
    if path.is_symlink():
        raise WikiError("unsafe_path", "Queue database must not be a symbolic link.")
    if path.exists() and not path.is_file():
        raise WikiError("unsafe_path", "Queue file must be a regular file.")
    return path


def _connect(root: Path) -> sqlite3.Connection:
    path = _queue_path(root)
    try:
        connection = sqlite3.connect(str(path), timeout=15.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                job_type TEXT NOT NULL CHECK (job_type IN ('feedback', 'ingest')),
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                feedback_id TEXT,
                source_id TEXT,
                revision TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                outcome TEXT,
                proposal_path TEXT,
                completed_revision TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_feedback ON jobs(feedback_id) WHERE feedback_id IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_ingest_revision
                ON jobs(job_type, source_id, revision) WHERE job_type = 'ingest';
            CREATE INDEX IF NOT EXISTS jobs_pending_order ON jobs(status, created_at, job_id);
            CREATE TABLE IF NOT EXISTS feedback (
                feedback_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                job_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                reason TEXT,
                outcome TEXT,
                published_revision TEXT,
                updated_at TEXT NOT NULL
            );
            """
        )
        return connection
    except sqlite3.Error as exc:
        try:
            connection.close()
        except (UnboundLocalError, AttributeError):
            pass
        raise WikiError("queue_error", f"Failed to open local queue: {exc}.") from exc


def _normalize_feedback(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise WikiError("invalid_feedback", "Feedback request must be a JSON object.")
    normalized = dict(payload)
    feedback_id = normalized.get("feedback_id")
    if not isinstance(feedback_id, str) or not FEEDBACK_ID_RE.fullmatch(feedback_id):
        raise WikiError("invalid_feedback", "feedback_id must be a safe, non-empty identifier.")
    for field in ("answer_id", "wiki_revision", "description"):
        value = normalized.get(field)
        if not isinstance(value, str) or not value.strip():
            raise WikiError("invalid_feedback", f"Field {field} must contain text.")
    target = normalized.get("target")
    if not ((isinstance(target, str) and target.strip()) or (isinstance(target, dict) and target)):
        raise WikiError("invalid_feedback", "target must identify a disputed claim or page.")
    evidence = normalized.setdefault("evidence", [])
    if not isinstance(evidence, list):
        raise WikiError("invalid_feedback", "evidence must be a list.")
    if "suggested_correction" in normalized and normalized["suggested_correction"] is not None and not isinstance(normalized["suggested_correction"], str):
        raise WikiError("invalid_feedback", "suggested_correction must be a string or null.")
    if "related_feedback_id" in normalized and normalized["related_feedback_id"] is not None:
        related = normalized["related_feedback_id"]
        if not isinstance(related, str) or not FEEDBACK_ID_RE.fullmatch(related):
            raise WikiError("invalid_feedback", "related_feedback_id has an invalid format.")
    _canonical(normalized)
    return normalized


def submit_feedback(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist feedback and its processing job; identical retries are idempotent."""
    normalized = _normalize_feedback(payload)
    feedback_id = normalized["feedback_id"]
    encoded = _canonical(normalized)
    connection = _connect(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT payload, job_id, status FROM feedback WHERE feedback_id = ?", (feedback_id,)).fetchone()
        if existing is not None:
            if existing["payload"] != encoded:
                connection.rollback()
                raise WikiError("idempotency_conflict", "This feedback_id has already been used with different content.")
            connection.commit()
            return {"feedback_id": feedback_id, "job_id": existing["job_id"], "status": existing["status"], "duplicate": True}

        job_id = uuid.uuid4().hex
        now = _now()
        connection.execute(
            "INSERT INTO jobs (job_id, job_type, status, payload, feedback_id, created_at, updated_at) VALUES (?, 'feedback', 'pending', ?, ?, ?, ?)",
            (job_id, encoded, feedback_id, now, now),
        )
        connection.execute(
            "INSERT INTO feedback (feedback_id, payload, job_id, status, updated_at) VALUES (?, ?, ?, 'pending', ?)",
            (feedback_id, encoded, job_id, now),
        )
        connection.commit()
        return {"feedback_id": feedback_id, "job_id": job_id, "status": "pending", "duplicate": False}
    except sqlite3.Error as exc:
        if connection.in_transaction:
            connection.rollback()
        raise WikiError("queue_error", f"Failed to save feedback request: {exc}.") from exc
    finally:
        connection.close()


def feedback_status(root: Path, feedback_id: str) -> dict[str, Any]:
    if not isinstance(feedback_id, str) or not FEEDBACK_ID_RE.fullmatch(feedback_id):
        raise WikiError("feedback_not_found", "Invalid feedback_id.")
    connection = _connect(root)
    try:
        row = connection.execute(
            "SELECT f.feedback_id, f.status, f.reason, f.outcome, f.published_revision, f.job_id, j.attempts, j.error "
            "FROM feedback f JOIN jobs j ON j.job_id = f.job_id WHERE f.feedback_id = ?",
            (feedback_id,),
        ).fetchone()
        if row is None:
            raise WikiError("feedback_not_found", f"Feedback request {feedback_id} was not found.")
        return {key: row[key] for key in row.keys()}
    finally:
        connection.close()


def enqueue_ingest(root: Path, source_id: str, revision: str) -> dict[str, Any]:
    """Queue one ingest job for an existing, hash-verified source revision."""
    if not isinstance(source_id, str) or not SOURCE_ID_RE.fullmatch(source_id):
        raise WikiError("unsafe_source_id", "Invalid source_id.")
    manifest = get_manifest(root, source_id, revision)
    payload = {
        "source_id": source_id,
        "revision": revision,
        "kind": manifest.get("kind"),
        "authorship": source_authorship(manifest),
    }
    encoded = _canonical(payload)
    connection = _connect(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT job_id, status FROM jobs WHERE job_type = 'ingest' AND source_id = ? AND revision = ?",
            (source_id, revision),
        ).fetchone()
        if existing is not None:
            connection.commit()
            return {"job_id": existing["job_id"], "status": existing["status"], "source_id": source_id, "revision": revision, "duplicate": True}
        job_id = uuid.uuid4().hex
        now = _now()
        connection.execute(
            "INSERT INTO jobs (job_id, job_type, status, payload, source_id, revision, created_at, updated_at) VALUES (?, 'ingest', 'pending', ?, ?, ?, ?, ?)",
            (job_id, encoded, source_id, revision, now, now),
        )
        connection.commit()
        return {"job_id": job_id, "status": "pending", "source_id": source_id, "revision": revision, "duplicate": False}
    except sqlite3.Error as exc:
        if connection.in_transaction:
            connection.rollback()
        raise WikiError("queue_error", f"Failed to queue source: {exc}.") from exc
    finally:
        connection.close()
