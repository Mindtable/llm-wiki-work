"""Read-only selection and human-readable rendering of ready wiki proposals."""

from __future__ import annotations

import difflib
import errno
import hashlib
import json
import os
import re
import sqlite3
import stat
from pathlib import Path
from typing import Any

from .errors import WikiError
from .knowledge import _parse_frontmatter, page_project
from .maintenance import JOB_ID_RE, _job_project, _validate_proposal
from .sources import get_manifest, safe_managed_path, source_authorship, source_project


def _root_path(root: Path) -> Path:
    path = Path(root).expanduser().resolve()
    if not path.is_dir():
        raise WikiError("root_not_found", f"Wiki root not found: {path}.")
    return path


def _read_only_queue(root: Path) -> sqlite3.Connection | None:
    """Open an existing queue in read-only mode without initializing state."""
    queue_path = safe_managed_path(root, ".state/queue.sqlite3")
    if not queue_path.exists():
        return None
    if queue_path.is_symlink() or not queue_path.is_file():
        raise WikiError("unsafe_path", "Queue database must be a regular file.")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{queue_path.as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise WikiError("queue_error", f"Failed to open the review queue read-only: {exc}.") from exc


def _checked_job_id(job_id: Any) -> str:
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise WikiError("invalid_state", "job_id must be a 32-character lowercase hexadecimal ID.")
    return job_id


def _select_ready_job(
    connection: sqlite3.Connection,
    requested_job_id: str | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        connection.execute("BEGIN")
        ready_rows = connection.execute(
            "SELECT job_id FROM jobs WHERE status = 'ready_for_review' ORDER BY created_at, job_id"
        ).fetchall()
        ready_job_ids = [_checked_job_id(row["job_id"]) for row in ready_rows]

        if requested_job_id is None:
            if not ready_job_ids:
                connection.commit()
                return None, []
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (ready_job_ids[0],)).fetchone()
            if row is None:
                raise WikiError("job_not_found", "The oldest ready job disappeared from the queue.")
        else:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (requested_job_id,)).fetchone()
            if row is None:
                raise WikiError("job_not_found", f"Job {requested_job_id} was not found.")
            if row["status"] != "ready_for_review":
                raise WikiError("invalid_state", f"Job {requested_job_id} is not ready for review.")

        job = dict(row)
        connection.commit()
        return job, ready_job_ids
    except WikiError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except sqlite3.Error as exc:
        if connection.in_transaction:
            connection.rollback()
        raise WikiError("queue_error", f"Failed to read ready review jobs: {exc}.") from exc


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}.")
        result[key] = value
    return result


def _read_regular_file(path: Path, *, missing_ok: bool = False) -> bytes | None:
    """Read one regular file without following a symlink at its final component."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise WikiError("invalid_state", f"Required file was not found: {path}.")
    except OSError as exc:
        raise WikiError("review_read_error", f"Failed to inspect {path}: {exc}.") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise WikiError("unsafe_path", f"Review input must be a regular file: {path}.")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise WikiError("invalid_state", f"Required file was not found: {path}.")
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ELOOP:
            raise WikiError("unsafe_path", f"Review input cannot be a symbolic link: {path}.") from exc
        raise WikiError("review_read_error", f"Failed to open {path}: {exc}.") from exc

    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise WikiError("unsafe_path", f"Review input must be a regular file: {path}.")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    except OSError as exc:
        raise WikiError("review_read_error", f"Failed to read {path}: {exc}.") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _load_proposal(root: Path, job: dict[str, Any]) -> Any:
    job_id = _checked_job_id(job.get("job_id"))
    expected_relative = f".state/proposals/{job_id}.json"
    if job.get("proposal_path") != expected_relative:
        raise WikiError("invalid_state", "The queue proposal_path does not match the canonical job path.")
    path = safe_managed_path(root, expected_relative)
    raw = _read_regular_file(path)
    assert raw is not None
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise WikiError("invalid_proposal", f"Saved proposal is not valid JSON: {exc}.") from exc
    return value


def _unified_diff(old_lines: list[str], new_lines: list[str], *, fromfile: str, tofile: str) -> str:
    output: list[str] = []
    for line in difflib.unified_diff(old_lines, new_lines, fromfile=fromfile, tofile=tofile):
        output.append(line)
        if line and line[0] in {" ", "+", "-"} and not line.endswith(("\n", "\r")):
            output.append("\n\\ No newline at end of file\n")
    return "".join(output)


def _proposal_file_changes(root: Path, proposal: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    changes: list[dict[str, Any]] = []
    target_fingerprints: list[dict[str, Any]] = []
    for change in proposal["changes"]:
        relative = change["path"]
        if relative in {"wiki/index.md", "wiki/log.md"}:
            page_project_value, cross_project_navigation = None, True
        else:
            metadata, _, parse_error = _parse_frontmatter(change["content"])
            if parse_error or metadata is None:
                raise WikiError("invalid_proposal", f"Changed page metadata is invalid: {relative}.")
            page_project_value = page_project(metadata, error_code="invalid_proposal")
            cross_project_navigation = False
        path = safe_managed_path(root, relative)
        current_bytes = _read_regular_file(path, missing_ok=True)
        proposed_content = change["content"]
        try:
            proposed_bytes = proposed_content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise WikiError("invalid_proposal", f"Proposed content for {relative} is not valid UTF-8 text.") from exc
        try:
            current_text = current_bytes.decode("utf-8") if current_bytes is not None else ""
        except UnicodeDecodeError as exc:
            raise WikiError("invalid_state", f"Current target {relative} is not valid UTF-8 Markdown.") from exc

        if current_bytes is None:
            action = "create"
            diff = _unified_diff(
                [],
                proposed_content.splitlines(keepends=True),
                fromfile="/dev/null",
                tofile=f"b/{relative}",
            )
            current_sha256 = None
        elif current_bytes == proposed_bytes:
            action = "unchanged"
            diff = ""
            current_sha256 = hashlib.sha256(current_bytes).hexdigest()
        else:
            action = "update"
            diff = _unified_diff(
                current_text.splitlines(keepends=True),
                proposed_content.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            )
            current_sha256 = hashlib.sha256(current_bytes).hexdigest()

        proposed_sha256 = hashlib.sha256(proposed_bytes).hexdigest()
        changes.append(
            {
                "path": relative,
                "action": action,
                "diff": diff,
                "current_sha256": current_sha256,
                "proposed_sha256": proposed_sha256,
                "project": page_project_value,
                "cross_project_navigation": cross_project_navigation,
            }
        )
        target_fingerprints.append(
            {"path": relative, "exists": current_bytes is not None, "sha256": current_sha256}
        )
    return changes, target_fingerprints


def _verified_evidence(root: Path, proposal: dict[str, Any]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for citation in proposal["evidence"]:
        try:
            manifest = get_manifest(root, citation["source_id"], citation["revision"])
        except WikiError as exc:
            raise WikiError("invalid_proposal", f"Evidence {citation['source_id']}@{citation['revision']} changed during review: {exc.message}") from exc
        authorship = source_authorship(manifest)
        project = source_project(manifest)
        if citation.get("authorship") != authorship or citation.get("project") != project:
            raise WikiError("invalid_proposal", "Verified evidence metadata changed while preparing this review.")
        item = dict(citation)
        item["source_path"] = manifest["local_path"]
        evidence.append(item)
    return evidence


def _review_fingerprint(
    job: dict[str, Any],
    job_payload: dict[str, Any],
    project: str | None,
    project_bound: bool,
    proposal: dict[str, Any],
    evidence: list[dict[str, Any]],
    targets: list[dict[str, Any]],
) -> str:
    canonical = json.dumps(
        {
            "job": {
                "job_id": job["job_id"],
                "job_type": job["job_type"],
                "feedback_id": job.get("feedback_id"),
                "source_id": job.get("source_id"),
                "revision": job.get("revision"),
                "payload": job_payload,
            },
            "binding": {"project": project, "project_bound": project_bound},
            "proposal": proposal,
            "evidence": evidence,
            "targets": targets,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def review_proposal(root: Path, job_id: str | None = None) -> dict[str, Any]:
    """Select and verify one ready proposal without changing the queue or wiki."""
    base = _root_path(root)
    if job_id is not None:
        job_id = _checked_job_id(job_id)
    connection = _read_only_queue(base)
    if connection is None:
        if job_id is not None:
            raise WikiError("job_not_found", f"Job {job_id} was not found.")
        return {"status": "no_ready_proposals", "job_id": None, "ready_job_ids": []}

    try:
        job, ready_job_ids = _select_ready_job(connection, job_id)
    finally:
        connection.close()
    if job is None:
        return {"status": "no_ready_proposals", "job_id": None, "ready_job_ids": []}

    selected_id = _checked_job_id(job.get("job_id"))
    if job.get("status") != "ready_for_review":
        raise WikiError("invalid_state", f"Job {selected_id} is not ready for review.")
    if job.get("job_type") not in {"ingest", "feedback"}:
        raise WikiError("invalid_state", f"Job {selected_id} has an unsupported job_type.")

    try:
        job_payload = json.loads(job["payload"])
    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        raise WikiError("invalid_state", f"Job {selected_id} has invalid payload JSON.") from exc
    if not isinstance(job_payload, dict):
        raise WikiError("invalid_state", f"Job {selected_id} payload must be a JSON object.")
    try:
        binding = _job_project(base, job)
    except WikiError:
        raise
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise WikiError("invalid_state", f"Job {selected_id} has invalid project-binding data.") from exc

    raw_proposal = _load_proposal(base, job)
    proposal = _validate_proposal(
        base,
        raw_proposal,
        expected_project=binding.project,
        project_bound=binding.bound,
    )
    evidence = _verified_evidence(base, proposal)
    changes, targets = _proposal_file_changes(base, proposal)
    fingerprint = _review_fingerprint(job, job_payload, binding.project, binding.bound, proposal, evidence, targets)
    return {
        "status": "ready_for_review",
        "job_id": selected_id,
        "ready_job_ids": ready_job_ids,
        "job_type": job["job_type"],
        "outcome": proposal["outcome"],
        "project": binding.project,
        "project_bound": binding.bound,
        "summary": proposal["summary"],
        "proposal_path": job["proposal_path"],
        "review_fingerprint": fingerprint,
        "changes": changes,
        "evidence": evidence,
    }


def _display_text(value: Any) -> str:
    text = str(value)
    output: list[str] = []
    for char in text:
        codepoint = ord(char)
        if codepoint < 32 and char not in "\n\r\t":
            output.append(f"\\x{codepoint:02x}")
        elif 0xD800 <= codepoint <= 0xDFFF:
            output.append(f"\\u{codepoint:04x}")
        else:
            output.append(char)
    return "".join(output)


def _code_span(value: Any) -> str:
    text = _display_text(value).replace("\r", "\\r").replace("\n", "\\n")
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(1, longest + 1)
    padding = " " if text.startswith((" ", "`")) or text.endswith((" ", "`")) else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def _fenced_block(value: Any, language: str) -> str:
    text = _display_text(value)
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    if not text.endswith("\n"):
        text += "\n"
    return f"{fence}{language}\n{text}{fence}"


def render_review(report: dict[str, Any]) -> str:
    """Render one review report as inert Markdown for a human operator."""
    if not isinstance(report, dict):
        raise WikiError("invalid_review_report", "Review report must be a JSON object.")
    if report.get("status") == "no_ready_proposals":
        return "# Proposal review\n\nNo proposals are ready for review.\n"
    if report.get("status") != "ready_for_review":
        raise WikiError("invalid_review_report", "Review report does not describe a ready proposal.")
    job_id = report.get("job_id")
    summary = report.get("summary")
    changes = report.get("changes")
    evidence = report.get("evidence")
    if not isinstance(job_id, str) or not isinstance(summary, str) or not isinstance(changes, list) or not isinstance(evidence, list):
        raise WikiError("invalid_review_report", "Review report is missing required fields.")

    lines = [
        f"# Proposal {job_id}",
        "",
        f"- Job type: {_code_span(report.get('job_type', 'unknown'))}",
        f"- Outcome: **{_display_text(report.get('outcome', 'unknown'))}**",
    ]
    if report.get("project_bound"):
        project = report.get("project")
        lines.append(f"- Scope: {_code_span(project) if project is not None else 'general sources'} (bound)")
    else:
        lines.append("- Scope: unbound")
    lines.extend(
        [
            f"- Proposal file: {_code_span(report.get('proposal_path', ''))}",
            f"- Review fingerprint: {_code_span(report.get('review_fingerprint', ''))}",
            "",
            "## Rationale",
            _fenced_block(summary, "text"),
            "",
            "## File changes",
        ]
    )
    if not changes:
        lines.append("No wiki changes are proposed for this outcome.")
    else:
        for change in changes:
            if not isinstance(change, dict) or not isinstance(change.get("path"), str) or not isinstance(change.get("diff"), str):
                raise WikiError("invalid_review_report", "Review report contains a malformed file change.")
            lines.extend(
                [
                    "",
                    f"### {_code_span(change['path'])} — {_display_text(change.get('action', 'unknown'))}",
                    f"- Current SHA-256: {_code_span(change.get('current_sha256') or 'not present')}",
                    f"- Proposed SHA-256: {_code_span(change.get('proposed_sha256', ''))}",
                    f"- Project: {'cross-project navigation' if change.get('cross_project_navigation') else _code_span(change.get('project') or 'general/unassigned')}",
                ]
            )
            if change["diff"]:
                lines.extend(["", _fenced_block(change["diff"], "diff")])
            else:
                lines.extend(["", "No textual difference."])

    lines.extend(["", "## Verified evidence"])
    if not evidence:
        lines.append("No verified evidence citations are included.")
    else:
        for citation in evidence:
            if not isinstance(citation, dict):
                raise WikiError("invalid_review_report", "Review report contains malformed evidence.")
            source_id = citation.get("source_id", "unknown")
            revision = citation.get("revision", "unknown")
            lines.extend(
                [
                    "",
                    f"- Source: {_code_span(f'{source_id}@{revision}')}",
                    f"  - Locator: {_code_span(citation.get('locator', 'unknown'))}",
                    f"  - Authorship: {_code_span(citation.get('authorship', 'unknown'))}",
                    f"  - Project: {_code_span(citation.get('project') or 'unassigned')}",
                    f"  - Source file: {_code_span(citation.get('source_path', 'unknown'))}",
                ]
            )
            if citation.get("wiki_page") is not None:
                lines.append(f"  - Wiki page: {_code_span(citation['wiki_page'])}")
            for optional in ("supports", "note"):
                if citation.get(optional):
                    lines.append(f"  - {optional.title()}: {_code_span(citation[optional])}")

    lines.extend(
        [
            "",
            "## Review actions",
            "Accept, Revise, or Defer. Acceptance does not apply or commit changes automatically.",
        ]
    )
    return "\n".join(lines) + "\n"
