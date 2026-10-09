"""Deterministic local search and structural checks for the wiki."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit
from typing import Any

from .errors import WikiError
from .raw import raw_drop_files
from .sources import (
    REVISION_RE,
    SOURCE_ID_RE,
    SOURCE_KINDS,
    get_manifest,
    manifest_path,
    project_matches,
    safe_managed_path,
    source_authorship,
    source_project,
    source_tip,
    validate_project,
)


TEXT_SUFFIXES = {".md", ".txt", ".text", ".rst", ".json", ".yaml", ".yml", ".toml", ".py", ".xml", ".bpmn", ".dmn", ".csv", ".tsv", ".html", ".htm", ".log"}
PAGE_KINDS = {"process", "concept", "system", "source", "source_analysis"}
REVIEW_STATES = {"draft", "reviewed", "stale"}
REQUIRED_PAGE_FIELDS = {"id", "title", "kind", "domain", "review_status", "source_refs", "depends_on", "reviewed_at"}
MANIFEST_NAME_RE = re.compile(r"(?P<source>[a-z0-9]+(?:-[a-z0-9]+)*)--(?P<revision>[0-9a-f]{64})\.json\Z")
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
SOURCE_LOCATOR_RE = re.compile(r"(?:line:(\d+)(?:-(\d+))?|section:([\w][\w_-]*))\Z", flags=re.UNICODE)


def _root(root: Path) -> Path:
    path = Path(root).expanduser().resolve()
    if not path.is_dir():
        raise WikiError("root_not_found", f"Wiki root not found: {path}.")
    return path


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _walk_files(
    root: Path,
    relative: str,
    suffixes: set[str] | None = None,
    *,
    issue: Any = None,
):
    """Yield regular files without following symbolic links."""
    try:
        base = safe_managed_path(root, relative)
    except WikiError as exc:
        if issue is None:
            raise
        issue(relative, "unsafe_path", exc.message)
        return
    if not base.exists():
        return
    if not base.is_dir():
        if issue is None:
            raise WikiError("unsafe_path", f"Expected a directory: {relative}.")
        issue(relative, "unsafe_path", "Expected a directory.")
        return
    for directory, names, files in os.walk(base, followlinks=False):
        directory_path = Path(directory)
        safe_names = []
        for name in names:
            candidate = directory_path / name
            if candidate.is_symlink():
                if issue is not None:
                    issue(_relative(root, candidate), "unsafe_path", "A symbolic link was found in the managed tree.")
                continue
            safe_names.append(name)
        names[:] = safe_names
        for name in sorted(files):
            path = directory_path / name
            if path.is_symlink():
                if issue is not None:
                    issue(_relative(root, path), "unsafe_path", "A symbolic link was found in the managed tree.")
                continue
            if not path.is_file():
                continue
            if suffixes is None or path.suffix.lower() in suffixes:
                yield path


def _parse_frontmatter(text: str) -> tuple[dict[str, Any] | None, str, str | None]:
    """Parse the documented JSON-in-YAML-subset metadata block."""
    lines = text.splitlines()
    if not lines or lines[0].lstrip("\ufeff") != "---":
        return None, text, "missing frontmatter delimiter"
    try:
        closing = lines.index("---", 1)
    except ValueError:
        return None, text, "frontmatter has no closing delimiter"
    raw = "\n".join(lines[1:closing])
    try:
        metadata = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, "\n".join(lines[closing + 1 :]), f"frontmatter must contain one JSON object: {exc.msg}"
    if not isinstance(metadata, dict):
        return None, "\n".join(lines[closing + 1 :]), "frontmatter must be a JSON object"
    return metadata, "\n".join(lines[closing + 1 :]), None


def _source_records(root: Path, report: dict[str, list[dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
    manifests_dir = safe_managed_path(root, "sources/manifests")
    if not manifests_dir.exists():
        return []
    if not manifests_dir.is_dir():
        if report is not None:
            report["errors"].append({"code": "unsafe_path", "path": "sources/manifests", "message": "Manifests must be inside a directory."})
        return []

    records: list[dict[str, Any]] = []
    for path in sorted(manifests_dir.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            if report is not None:
                report["errors"].append({"code": "unsafe_path", "path": _relative(root, path), "message": "Manifest must be a regular file."})
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            if report is not None:
                report["errors"].append({"code": "invalid_manifest", "path": _relative(root, path), "message": str(exc)})
            continue
        if not isinstance(record, dict):
            if report is not None:
                report["errors"].append({"code": "invalid_manifest", "path": _relative(root, path), "message": "Manifest must be a JSON object."})
            continue
        records.append(record)
    return records


def _validate_page_metadata(
    root: Path,
    path: Path,
    metadata: dict[str, Any],
    source_keys: set[tuple[str, str]],
    page_ids: set[str],
    report: dict[str, list[dict[str, Any]]],
) -> None:
    rel = _relative(root, path)

    def error(code: str, message: str) -> None:
        report["errors"].append({"code": code, "path": rel, "message": message})

    try:
        scoped_project = page_project(metadata)
    except WikiError as exc:
        error(exc.code, exc.message)
        scoped_project = None
        scope_valid = False
    else:
        scope_valid = True

    missing = sorted(REQUIRED_PAGE_FIELDS - metadata.keys())
    if missing:
        error("missing_page_metadata", "Missing fields: " + ", ".join(missing))
    page_id = metadata.get("id")
    if not isinstance(page_id, str) or not page_id.strip() or "\n" in page_id:
        error("invalid_page_id", "Page id must be a non-empty string.")
    elif page_id in page_ids:
        error("duplicate_page_id", f"Duplicate page id: {page_id}.")
    else:
        page_ids.add(page_id)

    for field in ("title", "domain"):
        if not isinstance(metadata.get(field), str):
            error("invalid_page_metadata", f"Field {field} must be a string.")
    page_kind = metadata.get("kind")
    if not isinstance(page_kind, str) or page_kind not in PAGE_KINDS:
        error("invalid_page_kind", "kind must be process, concept, system, source, or source_analysis.")
    review_status = metadata.get("review_status")
    if not isinstance(review_status, str) or review_status not in REVIEW_STATES:
        error("invalid_review_status", "review_status must be draft, reviewed, or stale.")
    reviewed_at = metadata.get("reviewed_at")
    if reviewed_at is not None and not isinstance(reviewed_at, str):
        error("invalid_page_metadata", "reviewed_at must be a string or null.")
    if metadata.get("review_status") == "reviewed" and not reviewed_at:
        error("invalid_page_metadata", "reviewed_at is required for a reviewed page.")

    source_refs = metadata.get("source_refs")
    if not isinstance(source_refs, list):
        error("invalid_source_refs", "source_refs must be a list.")
    else:
        for item in source_refs:
            if not isinstance(item, dict):
                error("invalid_source_refs", "Each source_refs entry must be an object.")
                continue
            source_id = item.get("source_id")
            revision = item.get("revision")
            if not isinstance(source_id, str) or not SOURCE_ID_RE.fullmatch(source_id) or not isinstance(revision, str) or not REVISION_RE.fullmatch(revision):
                error("invalid_source_refs", "Each source reference must contain valid source_id and revision values.")
            elif (source_id, revision) not in source_keys:
                error("unknown_source_ref", f"Source is not registered: {source_id}@{revision}.")
            elif scope_valid:
                try:
                    manifest = get_manifest(root, source_id, revision)
                    referenced_project = source_project(manifest)
                except WikiError as exc:
                    error("invalid_source_refs", f"Source reference {source_id}@{revision} failed validation: {exc.message}")
                    continue
                if scoped_project is None and referenced_project is not None:
                    error("invalid_source_refs", "A general page cannot reference a project-scoped source.")
                elif scoped_project is not None and referenced_project not in {None, scoped_project}:
                    error("invalid_source_refs", "Page project scope must match each scoped source reference.")

    dependencies = metadata.get("depends_on")
    if not isinstance(dependencies, list) or any(not isinstance(item, str) or not item for item in dependencies):
            error("invalid_dependencies", "depends_on must be a list of non-empty page IDs.")


def validate_page_document(root: Path, relative: str, text: str) -> dict[str, Any] | None:
    """Validate one proposed page's metadata with the same rules as lint()."""
    base = _root(root)
    if relative in {"wiki/index.md", "wiki/log.md"}:
        return None
    try:
        path = safe_managed_path(base, relative)
    except WikiError as exc:
        raise WikiError("invalid_page_metadata", f"Unsafe page path: {relative}.") from exc
    if not relative.startswith("wiki/") or not relative.endswith(".md"):
        raise WikiError("invalid_page_metadata", "Page must be a Markdown file inside wiki/.")
    metadata, _, parse_error = _parse_frontmatter(text)
    if parse_error or metadata is None:
        raise WikiError("invalid_page_metadata", parse_error or "Frontmatter is missing.")
    source_keys = {
        (record.get("source_id"), record.get("revision"))
        for record in _source_records(base)
        if isinstance(record.get("source_id"), str) and isinstance(record.get("revision"), str)
    }
    report: dict[str, list[dict[str, Any]]] = {"errors": [], "warnings": []}
    _validate_page_metadata(base, path, metadata, source_keys, set(), report)
    if report["errors"]:
        errors = "; ".join(f"{item['code']}: {item['message']}" for item in report["errors"])
        raise WikiError("invalid_page_metadata", errors)
    return metadata


def page_project(metadata: dict[str, Any], *, error_code: str = "invalid_page_metadata") -> str | None:
    """Return a page's optional project label, rejecting malformed declarations."""
    if not isinstance(metadata, dict):
        raise WikiError(error_code, "Page metadata must be a JSON object.")
    if "scope" not in metadata:
        return None
    scope = metadata["scope"]
    if not isinstance(scope, dict):
        raise WikiError(error_code, "scope must be an object.")
    return validate_project(scope.get("project"), error_code=error_code)


def _page_source_refs_match_project(root: Path, metadata: dict[str, Any], project: str | None) -> bool:
    references = metadata.get("source_refs")
    if not isinstance(references, list):
        return False
    for reference in references:
        if not isinstance(reference, dict):
            return False
        source_id, revision = reference.get("source_id"), reference.get("revision")
        if not isinstance(source_id, str) or not isinstance(revision, str):
            return False
        try:
            manifest = get_manifest(root, source_id, revision)
            referenced_project = source_project(manifest)
        except WikiError:
            return False
        if project is None:
            if referenced_project is not None:
                return False
        elif referenced_project not in {None, project}:
            return False
    return True


def wiki_page_in_project(root: Path, wiki_page: str, project: str | None) -> bool:
    """Check whether a page and all its source references fit the requested scope."""
    requested_project = validate_project(project)
    return _wiki_page_in_scope(root, wiki_page, requested_project, bound=requested_project is not None)


def _wiki_page_in_scope(root: Path, wiki_page: str, project: str | None, *, bound: bool) -> bool:
    """Check a citation page against a bound project, including a bound general scope."""
    base = _root(root)
    requested_project = validate_project(project)
    page_path = validate_wiki_page_reference(base, wiki_page)
    relative = _relative(base, page_path)
    if bound and relative in {"wiki/index.md", "wiki/log.md"}:
        return False
    try:
        text = page_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise WikiError("invalid_source_reference", f"Failed to read wiki page: {exc}.") from exc
    metadata, _, parse_error = _parse_frontmatter(text)
    if parse_error and parse_error != "missing frontmatter delimiter":
        return False
    if metadata is None:
        return True
    page_scope = page_project(metadata)
    if not bound:
        return _page_source_refs_match_project(base, metadata, page_scope)
    if not project_matches(page_scope, requested_project):
        return False
    if requested_project is None and page_scope is not None:
        return False
    return _page_source_refs_match_project(base, metadata, page_scope)


def _markdown_slug(value: str) -> str:
    value = re.sub(r"<[^>]*>", "", value).strip().lower()
    value = re.sub(r"[`*_~]", "", value)
    value = re.sub(r"[^\w\- ]", "", value, flags=re.UNICODE)
    return re.sub(r"\s+", "-", value)


def _markdown_anchors(text: str) -> set[str]:
    anchors: set[str] = set()
    slug_counts: dict[str, int] = {}
    code_stripped = re.sub(r"```.*?```|~~~.*?~~~", "", text, flags=re.DOTALL)
    for line in code_stripped.splitlines():
        match = HEADING_RE.match(line)
        if match:
            slug = _markdown_slug(match.group(1))
            count = slug_counts.get(slug, 0)
            anchors.add(slug if count == 0 else f"{slug}-{count}")
            slug_counts[slug] = count + 1
        for explicit in re.findall(r"<a\s+(?:id|name)=[\"']([^\"']+)[\"']", line, flags=re.IGNORECASE):
            anchors.add(unquote(explicit))
        for explicit in re.findall(r"\{#([^}]+)\}", line):
            anchors.add(explicit)
    return anchors


def validate_wiki_page_reference(root: Path, wiki_page: str) -> Path:
    """Validate an optional root-relative wiki page citation and anchor."""
    if not isinstance(wiki_page, str) or not wiki_page:
        raise WikiError("invalid_source_reference", "wiki_page must be a non-empty string.")
    page_name, separator, raw_anchor = wiki_page.partition("#")
    page_name = unquote(page_name)
    if not page_name.startswith("wiki/") or not page_name.endswith(".md"):
        raise WikiError("invalid_source_reference", "wiki_page must point to a Markdown file in wiki/.")
    try:
        page_path = safe_managed_path(root, page_name)
    except WikiError as exc:
        raise WikiError("invalid_source_reference", "wiki_page escapes the wiki/ directory.") from exc
    if not page_path.is_file():
        raise WikiError("invalid_source_reference", f"Wiki page not found: {page_name}.")
    if separator and raw_anchor:
        try:
            page_text = page_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise WikiError("invalid_source_reference", f"Failed to read wiki page: {exc}.") from exc
        if unquote(raw_anchor) not in _markdown_anchors(page_text):
            raise WikiError("invalid_source_reference", f"Wiki page anchor not found: {wiki_page}.")
    return page_path


def validate_source_reference(
    root: Path,
    source_id: str,
    revision: str,
    locator: str,
    wiki_page: str | None = None,
) -> tuple[dict[str, Any], Path]:
    """Verify a source manifest, its immutable bytes, and a concrete locator."""
    try:
        manifest = get_manifest(root, source_id, revision)
    except WikiError as exc:
        raise WikiError("invalid_source_reference", f"Source snapshot {source_id}@{revision} failed validation.") from exc
    try:
        snapshot = safe_managed_path(root, manifest["local_path"])
    except WikiError as exc:
        raise WikiError("invalid_source_reference", "The saved source path is no longer safe.") from exc
    if snapshot.suffix.lower() not in TEXT_SUFFIXES:
        raise WikiError("invalid_source_locator", f"An exact locator cannot be verified for format {snapshot.suffix or '(no extension)'}.")
    if not isinstance(locator, str):
        raise WikiError("invalid_source_locator", "locator must be a string.")
    locator_value = locator
    if locator.startswith("section:"):
        locator_value = "section:" + unquote(locator.partition(":")[2])
    match = SOURCE_LOCATOR_RE.fullmatch(locator_value)
    if not match:
        raise WikiError("invalid_source_locator", f"Unsupported locator: {locator}.")
    try:
        source_text = snapshot.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise WikiError("invalid_source_locator", f"Failed to read source text: {exc}.") from exc
    if match.group(1):
        first = int(match.group(1))
        last = int(match.group(2) or first)
        if first < 1 or last < first or last > len(source_text.splitlines()):
            raise WikiError("invalid_source_locator", f"Locator {locator} is outside the source line range.")
    else:
        if snapshot.suffix.lower() != ".md" or match.group(3) not in _markdown_anchors(source_text):
            raise WikiError("invalid_source_locator", f"Section {locator} was not found in the Markdown source.")
    if wiki_page is not None:
        validate_wiki_page_reference(root, wiki_page)
    return manifest, snapshot


def _check_markdown_links(root: Path, path: Path, text: str, report: dict[str, list[dict[str, Any]]]) -> None:
    rel = _relative(root, path)
    code_stripped = re.sub(r"```.*?```|~~~.*?~~~", "", text, flags=re.DOTALL)
    for match in LINK_RE.finditer(code_stripped):
        raw_target = match.group(1).strip()
        if raw_target.startswith("<") and ">" in raw_target:
            target = raw_target[1 : raw_target.index(">")]
        else:
            target = raw_target.split(None, 1)[0] if raw_target else ""
        if not target:
            continue
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc:
            continue
        local_path = unquote(parsed.path)
        anchor = unquote(parsed.fragment)
        if local_path.startswith("/"):
            relative = posixpath.normpath(local_path.lstrip("/"))
        elif local_path:
            relative = posixpath.normpath((path.relative_to(root).parent / local_path).as_posix())
        else:
            relative = rel
        if relative in {"..", "."} or relative.startswith("../") or relative.startswith("/"):
            report["errors"].append({"code": "unsafe_markdown_link", "path": rel, "target": target, "message": "Local target escapes the wiki root."})
            continue
        try:
            target_path = safe_managed_path(root, relative)
        except WikiError as exc:
            report["errors"].append({"code": "unsafe_markdown_link", "path": rel, "target": target, "message": exc.message})
            continue
        if target_path.is_dir():
            relative = posixpath.join(relative, "index.md")
            try:
                target_path = safe_managed_path(root, relative)
            except WikiError as exc:
                report["errors"].append({"code": "unsafe_markdown_link", "path": rel, "target": target, "message": exc.message})
                continue
        if not target_path.is_file():
            report["errors"].append({"code": "broken_markdown_link", "path": rel, "target": target, "message": "Local link target was not found."})
            continue
        if anchor and target_path.suffix.lower() == ".md":
            try:
                target_text = target_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                report["errors"].append({"code": "unreadable_markdown_link", "path": rel, "target": target, "message": "Failed to read Markdown link target."})
                continue
            if anchor not in _markdown_anchors(target_text):
                report["errors"].append({"code": "broken_markdown_anchor", "path": rel, "target": target, "message": "Anchor in local Markdown link was not found."})


def lint(root: Path) -> dict[str, list[dict[str, Any]]]:
    """Check source hashes, page metadata, provenance references, and local links."""
    base = _root(root)
    report: dict[str, list[dict[str, Any]]] = {"errors": [], "warnings": []}

    def record_path_issue(path: str, code: str, message: str) -> None:
        report["errors"].append({"code": code, "path": path, "message": message})

    try:
        manifests = _source_records(base, report)
    except WikiError as exc:
        manifests = []
        record_path_issue("sources/manifests", exc.code, exc.message)
    manifest_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    source_ids_by_revision: dict[str, list[dict[str, Any]]] = {}
    for record in manifests:
        source_id = record.get("source_id")
        revision = record.get("revision")
        if not isinstance(source_id, str) or not SOURCE_ID_RE.fullmatch(source_id) or not isinstance(revision, str) or not REVISION_RE.fullmatch(revision):
            report["errors"].append({"code": "invalid_manifest", "path": "sources/manifests", "message": "Invalid source_id or revision."})
            continue
        manifest_by_key[(source_id, revision)] = record
        source_ids_by_revision.setdefault(source_id, []).append(record)

    for path in _walk_files(base, "sources/manifests", {".json"}, issue=record_path_issue):
        rel = _relative(base, path)
        match = MANIFEST_NAME_RE.fullmatch(path.name)
        if not match:
            report["errors"].append({"code": "invalid_manifest_name", "path": rel, "message": "Filename must be <source_id>--<sha256>.json."})
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue  # _source_records already recorded the parse error.
        if not isinstance(record, dict):
            continue
        source_id, revision = match.group("source"), match.group("revision")
        if record.get("source_id") != source_id or record.get("revision") != revision:
            report["errors"].append({"code": "manifest_name_mismatch", "path": rel, "message": "Manifest filename does not match its source_id/revision."})
        try:
            source_authorship(record)
        except WikiError as exc:
            report["errors"].append({"code": exc.code, "path": rel, "message": exc.message})
        try:
            source_project(record)
        except WikiError as exc:
            report["errors"].append({"code": exc.code, "path": rel, "message": exc.message})
        missing = {"source_id", "revision", "kind", "origin", "upstream_revision", "sha256", "captured_at", "local_path", "scope", "supersedes", "derived_from"} - record.keys()
        if missing:
            report["errors"].append({"code": "invalid_manifest", "path": rel, "message": "Missing fields: " + ", ".join(sorted(missing))})
        if record.get("sha256") != revision:
            report["errors"].append({"code": "manifest_hash_mismatch", "path": rel, "message": "sha256 must match revision."})
        if not isinstance(record.get("kind"), str) or record.get("kind") not in SOURCE_KINDS:
            report["errors"].append({"code": "invalid_source_kind", "path": rel, "message": "Unknown source kind."})
        local_path = record.get("local_path")
        expected_prefix = f"sources/raw/{source_id}/"
        if not isinstance(local_path, str) or not local_path.startswith(expected_prefix):
            report["errors"].append({"code": "unsafe_source_path", "path": rel, "message": "local_path must be inside the source directory."})
            continue
        try:
            snapshot = safe_managed_path(base, local_path)
        except WikiError as exc:
            report["errors"].append({"code": "unsafe_source_path", "path": rel, "message": exc.message})
            continue
        if not snapshot.is_file():
            report["errors"].append({"code": "missing_source_snapshot", "path": local_path, "message": "Source snapshot was not found."})
        else:
            try:
                actual = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            except OSError as exc:
                report["errors"].append({"code": "source_read_error", "path": local_path, "message": str(exc)})
            else:
                if actual != revision:
                    report["errors"].append({"code": "source_hash_mismatch", "path": local_path, "message": "Source snapshot content does not match sha256."})
        if snapshot.suffix.lower() not in TEXT_SUFFIXES:
            report["warnings"].append({"code": "unsupported_source_format", "path": local_path, "message": "Hash verified, but the format is not in the supported text set."})
        if record.get("kind") == "code_summary":
            report["warnings"].append({"code": "derived_summary_not_source_code", "path": local_path, "message": "A derived summary was registered; this record does not confirm that the source code was checked."})
        if record.get("kind") == "code_summary":
            report["warnings"].append({"code": "derived_summary_not_source_code", "path": local_path, "message": "A derived summary was registered; this record does not confirm that the source code was checked."})

    for source_id, records in source_ids_by_revision.items():
        revisions = {str(record.get("revision")) for record in records}
        for record in records:
            previous = record.get("supersedes")
            if previous is not None and (not isinstance(previous, str) or previous not in revisions):
                report["errors"].append({"code": "broken_source_history", "path": "sources/manifests", "message": f"Source {source_id} supersedes missing revision {previous}."})
            derived_from = record.get("derived_from", [])
            if not isinstance(derived_from, list):
                report["errors"].append({"code": "invalid_derived_from", "path": "sources/manifests", "message": f"{source_id} derived_from must be a list."})
            else:
                for reference in derived_from:
                    if not isinstance(reference, dict):
                        report["errors"].append({"code": "unknown_derived_source", "path": "sources/manifests", "message": f"Source {source_id} contains an unknown derived_from reference."})
                        continue
                    ref_id, ref_revision = reference.get("source_id"), reference.get("revision")
                    if not isinstance(ref_id, str) or not isinstance(ref_revision, str) or (ref_id, ref_revision) not in manifest_by_key:
                        report["errors"].append({"code": "unknown_derived_source", "path": "sources/manifests", "message": f"Source {source_id} contains an unknown derived_from reference."})
        try:
            source_tip(records)
        except WikiError as exc:
            report["errors"].append({"code": exc.code, "path": "sources/manifests", "message": f"{source_id}: {exc.message}"})

    current_source_revisions: dict[str, str] = {}
    for source_id, records in source_ids_by_revision.items():
        try:
            current_source_revisions[source_id] = source_tip(records)
        except WikiError:
            pass
    try:
        for relative, _path, source_id, revision in raw_drop_files(base):
            if current_source_revisions.get(source_id) != revision:
                report["warnings"].append({
                    "code": "unregistered_raw_source",
                    "path": relative,
                    "message": "Raw file contents do not match the currently registered revision.",
                })
    except WikiError as exc:
        report["errors"].append({"code": exc.code, "path": "sources/raw", "message": exc.message})

    pages = list(_walk_files(base, "wiki", {".md"}, issue=record_path_issue))
    page_records: list[tuple[Path, dict[str, Any], str]] = []
    page_ids: set[str] = set()
    source_keys = set(manifest_by_key)
    for page in pages:
        try:
            text = page.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report["errors"].append({"code": "unreadable_markdown", "path": _relative(base, page), "message": str(exc)})
            continue
        metadata, body, metadata_error = _parse_frontmatter(text)
        if _relative(base, page) not in {"wiki/index.md", "wiki/log.md"}:
            if metadata_error:
                report["errors"].append({"code": "invalid_frontmatter", "path": _relative(base, page), "message": metadata_error})
            elif metadata is not None:
                _validate_page_metadata(base, page, metadata, source_keys, page_ids, report)
                page_records.append((page, metadata, body))
        _check_markdown_links(base, page, text, report)

    all_ids = {record.get("id") for _, record, _ in page_records if isinstance(record.get("id"), str)}
    for page, metadata, _ in page_records:
        dependencies = metadata.get("depends_on", [])
        for dependency in dependencies if isinstance(dependencies, list) else []:
            if isinstance(dependency, str) and dependency not in all_ids:
                report["errors"].append({"code": "unknown_page_dependency", "path": _relative(base, page), "message": f"Dependent page was not found: {dependency}."})

    report["errors"] = list({(item.get("path"), item.get("code"), item.get("message")): item for item in report["errors"]}.values())
    report["warnings"] = list({(item.get("path"), item.get("code"), item.get("message")): item for item in report["warnings"]}.values())
    report["errors"].sort(key=lambda item: (item.get("path", ""), item.get("code", ""), item.get("message", "")))
    report["warnings"].sort(key=lambda item: (item.get("path", ""), item.get("code", ""), item.get("message", "")))
    return report


def _frontmatter_id(text: str, fallback: str) -> str:
    metadata, _, _ = _parse_frontmatter(text)
    page_id = metadata.get("id") if metadata else None
    return page_id if isinstance(page_id, str) and page_id else fallback


def _searchable_match(text: str, query: str) -> tuple[int, str, str] | None:
    tokens = [token.casefold() for token in re.findall(r"[\w'-]+", query, flags=re.UNICODE) if token]
    if not tokens:
        raise WikiError("invalid_query", "Search query must contain text.")
    folded = text.casefold()
    if not all(token in folded for token in tokens):
        return None
    lines = text.splitlines() or [text]
    matching = [index for index, line in enumerate(lines, 1) if any(token in line.casefold() for token in tokens)]
    if matching:
        first, last = min(matching), max(matching)
        start = max(1, first - 1)
        end = min(len(lines), max(first, min(last, first + 3)) + 1)
        snippet = " ".join(line.strip() for line in lines[start - 1 : end] if line.strip())[:280]
        locator = f"line:{first}" if first == last else f"line:{first}-{last}"
    else:
        first = next((index for index, line in enumerate(lines, 1) if any(token in line.casefold() for token in tokens)), 1)
        snippet = lines[first - 1].strip()[:280]
        locator = f"line:{first}"
    score = sum(folded.count(token) for token in tokens) + (4 if " ".join(tokens) in folded else 0)
    return score, snippet, locator


def search(root: Path, query: str, *, project: str | None = None) -> list[dict[str, Any]]:
    """Search Markdown pages and the current registered text source revisions."""
    base = _root(root)
    if not isinstance(query, str) or not query.strip():
        raise WikiError("invalid_query", "Search query must contain text.")
    requested_project = validate_project(project)
    from .raw import discover_raw_sources

    discover_raw_sources(base)
    results: list[dict[str, Any]] = []

    for path in _walk_files(base, "wiki", {".md"}):
        relative = _relative(base, path)
        if requested_project is not None and relative in {"wiki/index.md", "wiki/log.md"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        metadata, _, parse_error = _parse_frontmatter(text)
        if parse_error:
            if parse_error != "missing frontmatter delimiter":
                continue
            page_scope = None
        else:
            page_scope = page_project(metadata) if metadata is not None else None
        if not project_matches(page_scope, requested_project):
            continue
        if metadata is not None and not _page_source_refs_match_project(base, metadata, page_scope):
            continue
        match = _searchable_match(text, query)
        if match is None:
            continue
        score, snippet, locator = match
        results.append({
            "type": "wiki_page",
            "id": _frontmatter_id(text, path.stem),
            "kind": "wiki_page",
            "project": page_scope,
            "path": relative,
            "revision": None,
            "locator": locator,
            "snippet": snippet,
            "score": score,
        })

    by_source: dict[str, list[dict[str, Any]]] = {}
    for record in _source_records(base):
        source_id, revision = record.get("source_id"), record.get("revision")
        if isinstance(source_id, str) and isinstance(revision, str):
            by_source.setdefault(source_id, []).append(record)
    for source_id, records in by_source.items():
        try:
            current_revision = source_tip(records)
        except WikiError:
            continue
        record = next((item for item in records if item.get("revision") == current_revision), None)
        if record is None:
            continue
        record_scope = source_project(record)
        if not project_matches(record_scope, requested_project):
            continue
        try:
            manifest = get_manifest(base, source_id, current_revision)
            source_scope = source_project(manifest)
            local_path = manifest["local_path"]
            snapshot = safe_managed_path(base, local_path)
            if snapshot.suffix.lower() not in TEXT_SUFFIXES:
                continue
            text = snapshot.read_text(encoding="utf-8")
        except WikiError as exc:
            if requested_project is not None and exc.code == "invalid_manifest":
                raise
            continue
        except (OSError, UnicodeDecodeError, KeyError):
            continue
        match = _searchable_match(text, query)
        if match is None:
            continue
        score, snippet, locator = match
        results.append({
            "type": "source",
            "id": source_id,
            "kind": manifest.get("kind"),
            "authorship": source_authorship(manifest),
            "project": source_scope,
            "path": local_path,
            "revision": current_revision,
            "locator": locator,
            "snippet": snippet,
            "score": score,
        })

    def scope_rank(item: dict[str, Any]) -> int:
        candidate = item.get("project")
        if requested_project is None:
            return 0 if candidate is None else 1
        return 0 if candidate == requested_project else 1

    results.sort(
        key=lambda item: (
            scope_rank(item),
            -item["score"],
            0 if item["type"] == "source" and item.get("authorship") == "human-written" else 1,
            item["path"],
            item["id"],
        )
    )
    return results


def project_inventory(root: Path, project: str | None, *, general_only: bool = False) -> dict[str, Any]:
    """Build a verified inventory, optionally prioritizing one project and shared material."""
    base = _root(root)
    requested_project = validate_project(project)

    by_source: dict[str, list[dict[str, Any]]] = {}
    for record in _source_records(base):
        source_id, revision = record.get("source_id"), record.get("revision")
        if isinstance(source_id, str) and isinstance(revision, str):
            by_source.setdefault(source_id, []).append(record)

    sources: list[dict[str, Any]] = []
    for source_id, records in by_source.items():
        try:
            current_revision = source_tip(records)
        except WikiError:
            continue
        for record in records:
            revision = record.get("revision")
            if not isinstance(revision, str):
                continue
            record_scope = source_project(record)
            if general_only and record_scope is not None:
                continue
            if not project_matches(record_scope, requested_project):
                continue
            manifest = get_manifest(base, source_id, revision)
            source_scope = source_project(manifest)
            manifest_file = manifest_path(base, source_id, revision)
            sources.append(
                {
                    "source_id": source_id,
                    "revision": revision,
                    "path": manifest["local_path"],
                    "manifest_path": _relative(base, manifest_file),
                    "project": source_scope,
                    "authorship": source_authorship(manifest),
                    "current": revision == current_revision,
                }
            )

    wiki_pages: list[dict[str, Any]] = []
    for path in _walk_files(base, "wiki", {".md"}):
        relative = _relative(base, path)
        if requested_project is not None and relative in {"wiki/index.md", "wiki/log.md"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        metadata, _, parse_error = _parse_frontmatter(text)
        if parse_error and parse_error != "missing frontmatter delimiter":
            continue
        page_scope = page_project(metadata) if metadata is not None else None
        if general_only and page_scope is not None:
            continue
        if not project_matches(page_scope, requested_project):
            continue
        if metadata is not None and not _page_source_refs_match_project(base, metadata, page_scope):
            continue
        wiki_pages.append(
            {
                "path": relative,
                "id": _frontmatter_id(text, path.stem),
                "project": page_scope,
            }
        )

    def scope_rank(candidate: str | None) -> int:
        return 0 if candidate == requested_project else 1

    sources.sort(key=lambda item: (scope_rank(item["project"]), 0 if item["current"] else 1, item["path"], item["source_id"], item["revision"]))
    wiki_pages.sort(key=lambda item: (scope_rank(item["project"]), item["path"], item["id"]))
    return {"project": requested_project, "sources": sources, "wiki_pages": wiki_pages}
