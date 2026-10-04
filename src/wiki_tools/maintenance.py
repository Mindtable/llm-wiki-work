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
from .sources import ensure_managed_dir, safe_managed_path


COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
JOB_ID_RE = re.compile(r"[0-9a-f]{32}\Z")


@contextmanager
def _writer_lock(root: Path):
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise WikiError("root_not_found", f"Корень wiki не найден: {base}")
    ensure_managed_dir(base, ".state")
    lock_path = safe_managed_path(base, ".state/maintenance.lock")
    if lock_path.is_symlink():
        raise WikiError("unsafe_path", "Блокировка обслуживания не может быть символической ссылкой")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise WikiError("maintenance_lock_error", f"Не удалось открыть блокировку обслуживания: {exc}") from exc
    stream = os.fdopen(descriptor, "r+b")
    try:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WikiError("maintenance_locked", "Другой процесс обслуживания уже работает") from exc
        yield
    finally:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def _job_row(connection, job_id: str):
    row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        raise WikiError("job_not_found", f"Задание {job_id} не найдено")
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


def _make_prompt(job: dict[str, Any]) -> str:
    if job["job_type"] == "ingest":
        operation = "ingest_source"
        directions = "Прочитай зарегистрированную ревизию источника и подготовь проверяемое предложение для wiki."
        payload = json.loads(job["payload"])
    else:
        operation = "review_feedback"
        directions = "Проверь заявку по зарегистрированным источникам и подготовь ручное предложение для ревью."
        payload = json.loads(job["payload"])
    envelope = {
        "operation": operation,
        "job_id": job["job_id"],
        "payload": payload,
        "contract": {
            "outcome": "proposed | rejected | needs_evidence",
            "summary": "Краткое обоснование",
            "changes": [{"path": "wiki/page.md", "content": "полный текст страницы"}],
            "evidence": [{"source_id": "registered-id", "revision": "sha256", "locator": "line:1"}],
        },
    }
    return directions + "\nВерни один JSON-объект по контракту; не записывай файлы и не публикуй изменения.\n" + json.dumps(envelope, ensure_ascii=False, sort_keys=True)


def _validate_evidence(root: Path, evidence: Any, outcome: str) -> list[dict[str, Any]]:
    if not isinstance(evidence, list):
        raise WikiError("invalid_proposal", "evidence должна быть списком цитат")
    if outcome in {"proposed", "rejected"} and not evidence:
        raise WikiError("invalid_proposal", f"Для outcome={outcome} нужны проверяемые основания")
    validated: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, dict):
            raise WikiError("invalid_proposal", "Каждое основание должно быть объектом цитаты")
        source_id, revision, locator = item.get("source_id"), item.get("revision"), item.get("locator")
        if not isinstance(source_id, str) or not isinstance(revision, str) or not isinstance(locator, str):
            raise WikiError("invalid_proposal", "Основание должно содержать source_id, revision и locator")
        try:
            manifest, _ = validate_source_reference(root, source_id, revision, locator, item.get("wiki_page"))
        except WikiError as exc:
            raise WikiError("invalid_proposal", f"Основание {source_id}@{revision} не прошло проверку: {exc.message}") from exc
        citation: dict[str, Any] = {"source_id": source_id, "revision": revision, "locator": locator}
        for optional in ("supports", "note"):
            if optional in item:
                if not isinstance(item[optional], str):
                    raise WikiError("invalid_proposal", f"Поле основания {optional} должно быть строкой")
                citation[optional] = item[optional]
        if "wiki_page" in item:
            wiki_page = item["wiki_page"]
            if wiki_page is not None and not isinstance(wiki_page, str):
                raise WikiError("invalid_proposal", "wiki_page должен быть путём или null")
            citation["wiki_page"] = wiki_page
        validated.append(citation)
    return validated


def _validate_proposal(root: Path, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WikiError("invalid_proposal", "Ответ maintenance должен быть JSON-объектом")
    outcome = value.get("outcome")
    if not isinstance(outcome, str) or outcome not in {"proposed", "rejected", "needs_evidence"}:
        raise WikiError("invalid_proposal", "outcome должен быть proposed, rejected или needs_evidence")
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise WikiError("invalid_proposal", "summary должен содержать обоснование")
    changes = value.get("changes")
    if not isinstance(changes, list):
        raise WikiError("invalid_proposal", "changes должен быть списком")
    if outcome == "proposed" and not changes:
        raise WikiError("invalid_proposal", "Для proposed нужно хотя бы одно изменение")
    if outcome != "proposed" and changes:
        raise WikiError("invalid_proposal", f"Для outcome={outcome} список changes должен быть пустым")

    normalized_changes: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for change in changes:
        if not isinstance(change, dict):
            raise WikiError("invalid_proposal", "Каждое изменение должно быть объектом path/content")
        raw_path, content = change.get("path"), change.get("content")
        if not isinstance(raw_path, str) or not isinstance(content, str) or not content or "\x00" in content:
            raise WikiError("invalid_proposal", "Изменение должно содержать непустой path и текст content")
        relative = raw_path.replace("\\", "/")
        path = PurePosixPath(relative)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or len(path.parts) < 2 or path.parts[0] != "wiki" or path.suffix.lower() != ".md":
            raise WikiError("invalid_proposal", "Путь изменения должен быть Markdown-файлом внутри wiki/")
        if relative in seen_paths:
            raise WikiError("invalid_proposal", f"Путь изменения указан несколько раз: {relative}")
        seen_paths.add(relative)
        try:
            safe_managed_path(root, relative)
        except WikiError as exc:
            raise WikiError("invalid_proposal", f"Небезопасный путь изменения: {relative}") from exc
        validate_page_document(root, relative, content)
        normalized_changes.append({"path": relative, "content": content})

    evidence = _validate_evidence(root, value.get("evidence", []), outcome)
    return {"outcome": outcome, "summary": summary.strip(), "changes": normalized_changes, "evidence": evidence}


def _proposal_file(root: Path, job_id: str) -> Path:
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise WikiError("invalid_state", "Некорректный job_id в очереди")
    ensure_managed_dir(root, ".state/proposals")
    return safe_managed_path(root, f".state/proposals/{job_id}.json")


def _write_proposal(root: Path, job_id: str, proposal: dict[str, Any]) -> str:
    path = _proposal_file(root, job_id)
    directory = path.parent
    if path.is_symlink():
        raise WikiError("unsafe_path", "Файл предложения не может быть символической ссылкой")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{job_id}-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(proposal, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except OSError as exc:
        raise WikiError("proposal_write_error", f"Не удалось сохранить предложение: {exc}") from exc
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
            raise WikiError("invalid_state", "Задание перестало обрабатываться до сохранения предложения")
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
        raise WikiError("invalid_limit", "limit должен быть положительным целым числом")
    if not callable(execute):
        raise WikiError("invalid_executor", "Нужна функция execute(prompt)")
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise WikiError("root_not_found", f"Корень wiki не найден: {base}")

    items: list[dict[str, Any]] = []
    with _writer_lock(base):
        from .raw import discover_raw_sources

        discover_raw_sources(base)
        while len(items) < limit:
            job = _claim_next(base)
            if job is None:
                break
            try:
                raw_proposal = execute(_make_prompt(job))
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
                raise WikiError("job_not_found", f"Задание {job_id} не найдено")
            if job["status"] not in {"failed", "processing"}:
                connection.rollback()
                raise WikiError("invalid_state", f"Задание со статусом {job['status']} нельзя повторить")
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
        raise WikiError("invalid_commit", f"Не удалось проверить Git commit: {exc}") from exc


def _verify_commit(root: Path, revision: str) -> str:
    if not isinstance(revision, str) or not COMMIT_RE.fullmatch(revision.lower()):
        raise WikiError("invalid_commit", "Нужен полный идентификатор Git commit")
    normalized = revision.lower()
    result = _git(root, "rev-parse", "--verify", f"{normalized}^{{commit}}")
    resolved = result.stdout.decode("ascii", errors="ignore").strip().lower()
    if result.returncode != 0 or resolved != normalized:
        raise WikiError("invalid_commit", "Указанный commit отсутствует в репозитории wiki")
    return normalized


def _require_current_head(root: Path, revision: str) -> None:
    result = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
    head = result.stdout.decode("ascii", errors="ignore").strip().lower()
    if result.returncode != 0 or head != revision:
        raise WikiError("not_current_commit", "Для завершения предложения нужен текущий HEAD репозитория wiki")


def complete_job(root: Path, job_id: str, revision: str | None = None) -> dict[str, Any]:
    """Finish manual review after checking the referenced Git commit."""
    base = Path(root).expanduser().resolve()
    with _writer_lock(base):
        connection = _connect(base)
        try:
            job = _job_row(connection, job_id)
            if job["status"] != "ready_for_review":
                raise WikiError("invalid_state", f"Задание со статусом {job['status']} не готово к завершению")
            if job["proposal_path"] != f".state/proposals/{job_id}.json":
                raise WikiError("invalid_state", "Путь предложения в очереди повреждён")
            proposal_path = _proposal_file(base, job_id)
            if not proposal_path.is_file():
                raise WikiError("invalid_state", "Сохранённое предложение не найдено")
            try:
                raw = json.loads(proposal_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WikiError("invalid_proposal", f"Не удалось прочитать сохранённое предложение: {exc}") from exc
            proposal = _validate_proposal(base, raw)
            commit: str | None = None
            if proposal["outcome"] == "proposed":
                if revision is None:
                    raise WikiError("invalid_commit", "Для proposed нужен текущий Git commit")
                commit = _verify_commit(base, revision)
                _require_current_head(base, commit)
                for change in proposal["changes"]:
                    result = _git(base, "show", f"{commit}:{change['path']}")
                    if result.returncode != 0 or result.stdout != change["content"].encode("utf-8"):
                        raise WikiError("proposal_not_in_commit", f"Предложенный файл не совпадает с commit: {change['path']}")

            now = _now()
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE jobs SET status = 'resolved', completed_revision = ?, updated_at = ? WHERE job_id = ? AND status = 'ready_for_review'",
                (commit, now, job_id),
            ).rowcount
            if changed != 1:
                raise WikiError("invalid_state", "Задание изменилось до фиксации результата ревью")
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
