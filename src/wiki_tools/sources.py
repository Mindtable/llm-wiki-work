"""Immutable source snapshots and their JSON manifests."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import WikiError


SOURCE_KINDS = {"workflow", "procedure", "confluence_export", "code_summary", "code_excerpt", "unclassified"}
SOURCE_ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
REVISION_RE = re.compile(r"[0-9a-f]{64}\Z")
SAFE_SUFFIX_RE = re.compile(r"\.[A-Za-z0-9]{1,12}\Z")


def _root_path(root: Path) -> Path:
    path = Path(root).expanduser().resolve()
    if not path.is_dir():
        raise WikiError("root_not_found", f"Wiki root not found: {path}.")
    return path


def safe_managed_path(root: Path, relative: str | Path) -> Path:
    """Return a path below root, rejecting traversal and symlink components."""
    base = _root_path(root)
    rel = PurePosixPath(str(relative).replace("\\", "/"))
    if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
        raise WikiError("unsafe_path", "Path must remain inside the Wiki root.")

    candidate = base
    for part in rel.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise WikiError("unsafe_path", f"Symbolic links are not allowed in managed paths: {rel}.")
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(base):
            raise WikiError("unsafe_path", f"Path escapes the Wiki root: {rel}.")
    return candidate


def ensure_managed_dir(root: Path, relative: str | Path) -> Path:
    """Create a managed directory one checked component at a time."""
    base = _root_path(root)
    rel = PurePosixPath(str(relative).replace("\\", "/"))
    if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
        raise WikiError("unsafe_path", "Directory path must remain inside the Wiki root.")
    current = base
    for part in rel.parts:
        current = safe_managed_path(base, current.relative_to(base) / part)
        if current.exists():
            if not current.is_dir():
                raise WikiError("unsafe_path", f"Expected a directory: {current.relative_to(base)}.")
        else:
            try:
                current.mkdir()
            except FileExistsError:
                # Another importer may have created this checked directory.
                current = safe_managed_path(base, current.relative_to(base))
                if not current.is_dir():
                    raise WikiError("unsafe_path", f"Expected a directory: {current.relative_to(base)}.")
    return current


def _read_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise WikiError("unsafe_path", "Manifest must be a regular file.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WikiError("invalid_manifest", f"Failed to read manifest {path.name}: {exc}.") from exc
    if not isinstance(value, dict):
        raise WikiError("invalid_manifest", f"Manifest {path.name} must be a JSON object.")
    return value


def manifest_path(root: Path, source_id: str, revision: str) -> Path:
    if not SOURCE_ID_RE.fullmatch(source_id) or not REVISION_RE.fullmatch(revision):
        raise WikiError("unsafe_source_id", "Invalid source_id or revision.")
    return safe_managed_path(root, f"sources/manifests/{source_id}--{revision}.json")


def get_manifest(root: Path, source_id: str, revision: str) -> dict[str, Any]:
    """Load a registered source revision without trusting its stored path."""
    path = manifest_path(root, source_id, revision)
    manifest = _read_manifest(path)
    if manifest is None:
        raise WikiError("source_not_found", f"Source {source_id}@{revision} is not registered.")
    if manifest.get("source_id") != source_id or manifest.get("revision") != revision:
        raise WikiError("invalid_manifest", f"Manifest ID does not match its filename: {path.name}.")
    if manifest.get("sha256") != revision:
        raise WikiError("invalid_manifest", f"sha256 does not match revision in manifest {path.name}.")
    if not isinstance(manifest.get("kind"), str) or manifest.get("kind") not in SOURCE_KINDS:
        raise WikiError("invalid_manifest", f"Invalid kind in manifest {path.name}.")
    if not isinstance(manifest.get("origin"), str) or not isinstance(manifest.get("upstream_revision"), str):
        raise WikiError("invalid_manifest", f"Invalid origin/upstream_revision in manifest {path.name}.")
    local_path = manifest.get("local_path")
    expected_prefix = f"sources/raw/{source_id}/"
    if not isinstance(local_path, str) or not local_path.startswith(expected_prefix):
        raise WikiError("invalid_manifest", f"Invalid local_path in manifest {path.name}.")
    if len(PurePosixPath(local_path).parts) != 4:
        raise WikiError("invalid_manifest", f"local_path does not point to a source snapshot in {path.name}.")
    try:
        snapshot = safe_managed_path(root, local_path)
    except WikiError as exc:
        raise WikiError("invalid_manifest", f"Invalid local_path in manifest {path.name}.") from exc
    if not re.fullmatch(re.escape(revision) + r"(?:\.[A-Za-z0-9]{1,12})?", snapshot.name):
        raise WikiError("invalid_manifest", f"local_path does not point to revision {revision}.")
    if not snapshot.is_file() or hashlib.sha256(snapshot.read_bytes()).hexdigest() != revision:
        raise WikiError("source_hash_mismatch", f"Source snapshot {source_id}@{revision} is missing or corrupted.")
    return manifest


@contextmanager
def _source_lock(root: Path, source_id: str):
    """Serialize source history updates across local processes."""
    import fcntl

    lock_dir = ensure_managed_dir(root, ".state/source-locks")
    lock_path = safe_managed_path(root, f".state/source-locks/{source_id}.lock")
    if lock_path.is_symlink():
        raise WikiError("unsafe_path", "Source lock must not be a symbolic link.")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise WikiError("source_lock_error", f"Failed to open source lock: {exc}.") from exc
    with os.fdopen(descriptor, "r+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _all_source_manifests(root: Path, source_id: str) -> list[dict[str, Any]]:
    base = safe_managed_path(root, "sources/manifests")
    if not base.exists():
        return []
    if not base.is_dir():
        raise WikiError("unsafe_path", "sources/manifests must be a directory.")
    records: list[dict[str, Any]] = []
    for path in sorted(base.glob(f"{source_id}--*.json")):
        safe_managed_path(root, path.relative_to(root))
        record = _read_manifest(path)
        if record is not None:
            if record.get("source_id") != source_id or not isinstance(record.get("revision"), str) or not REVISION_RE.fullmatch(record["revision"]):
                raise WikiError("invalid_manifest", f"Invalid manifest: {path.name}.")
            records.append(record)
    return records


def source_tip(manifests: list[dict[str, Any]]) -> str | None:
    """Return the unique revision not superseded by a later manifest."""
    if not manifests:
        return None
    revisions = {str(record["revision"]) for record in manifests}
    predecessors = {
        str(record["supersedes"])
        for record in manifests
        if isinstance(record.get("supersedes"), str) and record["supersedes"] in revisions
    }
    tips = sorted(revisions - predecessors)
    if len(tips) != 1:
        raise WikiError("source_history_conflict", "Source history has multiple current revisions.")
    return tips[0]


def add_source(
    root: Path,
    path: Path,
    *,
    source_id: str,
    kind: str,
    origin: str = "",
    upstream_revision: str = "",
) -> dict[str, Any]:
    """Register a new immutable source snapshot or reuse an identical one."""
    if not isinstance(source_id, str) or not SOURCE_ID_RE.fullmatch(source_id):
        raise WikiError("unsafe_source_id", "source_id must be a lowercase ASCII slug.")
    if not isinstance(kind, str) or kind not in SOURCE_KINDS:
        raise WikiError("invalid_source_kind", f"Unknown source kind: {kind}.")
    source_path = Path(path).expanduser()
    if not source_path.exists() or not source_path.is_file():
        raise WikiError("source_not_found", f"Source file not found: {source_path}.")
    try:
        content = source_path.read_bytes()
    except OSError as exc:
        raise WikiError("source_read_error", f"Failed to read source: {exc}.") from exc

    root_path = _root_path(root)
    with _source_lock(root_path, source_id):
        return _add_source_locked(root_path, content, source_path, source_id, kind, origin, upstream_revision)


def _add_source_locked(
    root_path: Path,
    content: bytes,
    source_path: Path,
    source_id: str,
    kind: str,
    origin: str,
    upstream_revision: str,
) -> dict[str, Any]:
    revision = hashlib.sha256(content).hexdigest()
    ensure_managed_dir(root_path, f"sources/raw/{source_id}")
    manifest_dir = ensure_managed_dir(root_path, "sources/manifests")

    suffix = source_path.suffix
    if not SAFE_SUFFIX_RE.fullmatch(suffix):
        suffix = ""
    raw_relative = PurePosixPath("sources/raw") / source_id / f"{revision}{suffix.lower()}"
    raw_path = safe_managed_path(root_path, raw_relative)
    stored_manifest_path = safe_managed_path(root_path, f"sources/manifests/{source_id}--{revision}.json")

    old_manifest = _read_manifest(stored_manifest_path)
    if old_manifest is not None:
        validated = get_manifest(root_path, source_id, revision)
        current_revision = source_tip(_all_source_manifests(root_path, source_id))
        if current_revision != revision:
            raise WikiError(
                "historical_revision",
                f"These bytes are already registered as an older revision; the current revision is {current_revision}.",
            )
        return validated

    if raw_path.exists():
        if raw_path.is_symlink() or not raw_path.is_file() or hashlib.sha256(raw_path.read_bytes()).hexdigest() != revision:
            raise WikiError("source_hash_mismatch", f"Source snapshot path is already in use: {raw_relative}.")
    else:
        try:
            with raw_path.open("xb") as stream:
                stream.write(content)
        except FileExistsError:
            if not raw_path.is_file() or hashlib.sha256(raw_path.read_bytes()).hexdigest() != revision:
                raise WikiError("source_hash_mismatch", f"Source snapshot path is already in use: {raw_relative}.")
        except OSError as exc:
            raise WikiError("source_write_error", f"Failed to save source snapshot: {exc}.") from exc

    existing = _all_source_manifests(root_path, source_id)
    previous = source_tip(existing)
    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    manifest = {
        "source_id": source_id,
        "revision": revision,
        "kind": kind,
        "origin": str(origin),
        "upstream_revision": str(upstream_revision),
        "sha256": revision,
        "captured_at": captured_at,
        "local_path": raw_relative.as_posix(),
        "scope": {},
        "supersedes": previous,
        "derived_from": [],
    }

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{source_id}-", suffix=".tmp", dir=manifest_dir)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_name, stored_manifest_path, follow_symlinks=False)
        except FileExistsError:
            concurrent = get_manifest(root_path, source_id, revision)
            return concurrent
    except OSError as exc:
        raise WikiError("source_write_error", f"Failed to save manifest: {exc}.") from exc
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
    return manifest
