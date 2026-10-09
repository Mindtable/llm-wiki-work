"""Automatic registration of ordinary files dropped into sources/raw/."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Iterator

from .errors import WikiError
from .feedback import enqueue_ingest
from .sources import SOURCE_AUTHORSHIPS, SOURCE_ID_RE, add_source, safe_managed_path


_TEMP_SUFFIXES = {".tmp", ".part", ".partial", ".download", ".crdownload", ".swp", ".swo"}
_SERVICE_FILES = {".ds_store", "thumbs.db", "desktop.ini", "ehthumbs.db"}
_SNAPSHOT_NAME_RE = re.compile(r"[0-9a-f]{64}(?:\.[A-Za-z0-9]{1,12})?\Z")


def _root(root: Path) -> Path:
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise WikiError("root_not_found", f"Wiki root not found: {base}.")
    return base


def is_ignored_raw_name(name: str) -> bool:
    """Return whether a raw input name is hidden, service, or temporary."""
    lowered = name.lower()
    return (
        name.startswith(".")
        or name.startswith("~$")
        or lowered in _SERVICE_FILES
        or lowered.endswith("~")
        or Path(lowered).suffix in _TEMP_SUFFIXES
    )


def is_managed_raw_snapshot(relative: str) -> bool:
    """Recognize canonical snapshot paths even if their bytes or manifest are bad."""
    parts = PurePosixPath(relative).parts
    return (
        len(parts) == 4
        and parts[:2] == ("sources", "raw")
        and SOURCE_ID_RE.fullmatch(parts[2]) is not None
        and _SNAPSHOT_NAME_RE.fullmatch(parts[3]) is not None
    )


def _raw_files(root: Path) -> Iterator[tuple[str, Path]]:
    base = _root(root)
    try:
        raw = safe_managed_path(base, "sources/raw")
    except WikiError as exc:
        raise WikiError("raw_scan_error", f"Failed to inspect sources/raw: {exc.message}.") from exc
    if not raw.exists():
        return
    if raw.is_symlink() or not raw.is_dir():
        raise WikiError("raw_scan_error", "sources/raw must be a directory, not a symbolic link.")

    def onerror(exc: OSError) -> None:
        failing = getattr(exc, "filename", None)
        try:
            detail = Path(failing).relative_to(base).as_posix() if failing else "sources/raw"
        except ValueError:
            detail = "sources/raw"
        raise WikiError("raw_scan_error", f"Failed to scan {detail}: {exc}.") from exc

    for directory, names, files in os.walk(raw, topdown=True, followlinks=False, onerror=onerror):
        current = Path(directory)
        kept_names: list[str] = []
        for name in sorted(names):
            candidate = current / name
            if is_ignored_raw_name(name) or candidate.is_symlink():
                continue
            try:
                mode = candidate.lstat().st_mode
            except OSError as exc:
                onerror(exc)
                continue
            if stat.S_ISDIR(mode):
                kept_names.append(name)
        names[:] = kept_names

        for name in sorted(files):
            if is_ignored_raw_name(name):
                continue
            candidate = current / name
            try:
                mode = candidate.lstat().st_mode
            except OSError as exc:
                onerror(exc)
                continue
            if not stat.S_ISREG(mode):
                continue
            relative = candidate.relative_to(base).as_posix()
            if is_managed_raw_snapshot(relative):
                continue
            yield relative, candidate


def raw_drop_id(relative_path: str) -> str:
    """Derive a stable, ASCII source ID from the root-relative drop path."""
    return "drop-" + hashlib.sha256(relative_path.encode("utf-8")).hexdigest()


def raw_drop_authorship(relative_path: str) -> str:
    """Classify a raw drop only from its first directory below sources/raw/."""
    parts = PurePosixPath(relative_path).parts
    if (
        len(parts) >= 4
        and parts[:2] == ("sources", "raw")
        and parts[2] in SOURCE_AUTHORSHIPS
        and parts[2] != "unknown"
    ):
        return parts[2]
    return "unknown"


def raw_drop_files(root: Path) -> list[tuple[str, Path, str, str]]:
    """Return eligible drop paths with stable IDs and current content hashes."""
    base = _root(root)
    found: list[tuple[str, Path, str, str]] = []
    for relative, path in _raw_files(base):
        try:
            safe_managed_path(base, relative)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode):
                    continue
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    digest = hashlib.sha256()
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise WikiError("raw_scan_error", f"Failed to read {relative}: {exc}.") from exc
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise WikiError("raw_file_changed", f"File changed while scanning: {relative}; wait for the copy to finish.")
        source_id = raw_drop_id(relative)
        revision = digest.hexdigest()
        found.append((relative, path, source_id, revision))
    return found


def discover_raw_sources(root: Path) -> list[dict]:
    """Register current drops as unclassified revisions and queue each for ingest."""
    base = _root(root)
    manifests: list[dict] = []
    for relative, path, source_id, scanned_revision in raw_drop_files(base):
        try:
            manifest = add_source(
                base,
                path,
                source_id=source_id,
                kind="unclassified",
                origin=relative,
                default_authorship=raw_drop_authorship(relative),
            )
            if manifest["revision"] != scanned_revision:
                raise WikiError(
                    "raw_file_changed",
                    f"File changed after scanning: {relative}; wait for the copy to finish.",
                )
            enqueue_ingest(base, source_id, manifest["revision"])
        except WikiError as exc:
            raise WikiError(exc.code, f"Failed to process raw file {relative}: {exc.message}.") from exc
        manifests.append(manifest)
    return manifests
