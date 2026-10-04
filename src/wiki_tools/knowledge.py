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
from .sources import REVISION_RE, SOURCE_ID_RE, SOURCE_KINDS, get_manifest, safe_managed_path, source_tip


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
        raise WikiError("root_not_found", f"Корень wiki не найден: {path}")
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
            raise WikiError("unsafe_path", f"Ожидался каталог: {relative}")
        issue(relative, "unsafe_path", "Ожидался каталог")
        return
    for directory, names, files in os.walk(base, followlinks=False):
        directory_path = Path(directory)
        safe_names = []
        for name in names:
            candidate = directory_path / name
            if candidate.is_symlink():
                if issue is not None:
                    issue(_relative(root, candidate), "unsafe_path", "Символическая ссылка в управляемом дереве")
                continue
            safe_names.append(name)
        names[:] = safe_names
        for name in sorted(files):
            path = directory_path / name
            if path.is_symlink():
                if issue is not None:
                    issue(_relative(root, path), "unsafe_path", "Символическая ссылка в управляемом дереве")
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
            report["errors"].append({"code": "unsafe_path", "path": "sources/manifests", "message": "Манифесты должны находиться в каталоге"})
        return []

    records: list[dict[str, Any]] = []
    for path in sorted(manifests_dir.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            if report is not None:
                report["errors"].append({"code": "unsafe_path", "path": _relative(root, path), "message": "Манифест должен быть обычным файлом"})
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            if report is not None:
                report["errors"].append({"code": "invalid_manifest", "path": _relative(root, path), "message": str(exc)})
            continue
        if not isinstance(record, dict):
            if report is not None:
                report["errors"].append({"code": "invalid_manifest", "path": _relative(root, path), "message": "Манифест должен быть JSON-объектом"})
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

    missing = sorted(REQUIRED_PAGE_FIELDS - metadata.keys())
    if missing:
        error("missing_page_metadata", "Отсутствуют поля: " + ", ".join(missing))
    page_id = metadata.get("id")
    if not isinstance(page_id, str) or not page_id.strip() or "\n" in page_id:
        error("invalid_page_id", "id страницы должен быть непустой строкой")
    elif page_id in page_ids:
        error("duplicate_page_id", f"id страницы повторяется: {page_id}")
    else:
        page_ids.add(page_id)

    for field in ("title", "domain"):
        if not isinstance(metadata.get(field), str):
            error("invalid_page_metadata", f"Поле {field} должно быть строкой")
    page_kind = metadata.get("kind")
    if not isinstance(page_kind, str) or page_kind not in PAGE_KINDS:
        error("invalid_page_kind", "kind должен быть process, concept, system, source или source_analysis")
    review_status = metadata.get("review_status")
    if not isinstance(review_status, str) or review_status not in REVIEW_STATES:
        error("invalid_review_status", "review_status должен быть draft, reviewed или stale")
    reviewed_at = metadata.get("reviewed_at")
    if reviewed_at is not None and not isinstance(reviewed_at, str):
        error("invalid_page_metadata", "reviewed_at должен быть строкой или null")
    if metadata.get("review_status") == "reviewed" and not reviewed_at:
        error("invalid_page_metadata", "Для reviewed страницы нужен reviewed_at")

    source_refs = metadata.get("source_refs")
    if not isinstance(source_refs, list):
        error("invalid_source_refs", "source_refs должен быть списком")
    else:
        for item in source_refs:
            if not isinstance(item, dict):
                error("invalid_source_refs", "Каждая ссылка в source_refs должна быть объектом")
                continue
            source_id = item.get("source_id")
            revision = item.get("revision")
            if not isinstance(source_id, str) or not SOURCE_ID_RE.fullmatch(source_id) or not isinstance(revision, str) or not REVISION_RE.fullmatch(revision):
                error("invalid_source_refs", "Каждая ссылка должна содержать корректные source_id и revision")
            elif (source_id, revision) not in source_keys:
                error("unknown_source_ref", f"Источник не зарегистрирован: {source_id}@{revision}")

    dependencies = metadata.get("depends_on")
    if not isinstance(dependencies, list) or any(not isinstance(item, str) or not item for item in dependencies):
            error("invalid_dependencies", "depends_on должен быть списком непустых идентификаторов страниц")


def validate_page_document(root: Path, relative: str, text: str) -> dict[str, Any] | None:
    """Validate one proposed page's metadata with the same rules as lint()."""
    base = _root(root)
    if relative in {"wiki/index.md", "wiki/log.md"}:
        return None
    try:
        path = safe_managed_path(base, relative)
    except WikiError as exc:
        raise WikiError("invalid_page_metadata", f"Небезопасный путь страницы: {relative}") from exc
    if not relative.startswith("wiki/") or not relative.endswith(".md"):
        raise WikiError("invalid_page_metadata", "Страница должна быть Markdown-файлом внутри wiki/")
    metadata, _, parse_error = _parse_frontmatter(text)
    if parse_error or metadata is None:
        raise WikiError("invalid_page_metadata", parse_error or "Отсутствует frontmatter")
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
        raise WikiError("invalid_source_reference", "wiki_page должен быть непустой строкой")
    page_name, separator, raw_anchor = wiki_page.partition("#")
    page_name = unquote(page_name)
    if not page_name.startswith("wiki/") or not page_name.endswith(".md"):
        raise WikiError("invalid_source_reference", "wiki_page должен указывать на Markdown-файл в wiki/")
    try:
        page_path = safe_managed_path(root, page_name)
    except WikiError as exc:
        raise WikiError("invalid_source_reference", "wiki_page выходит за каталог wiki") from exc
    if not page_path.is_file():
        raise WikiError("invalid_source_reference", f"Страница wiki не найдена: {page_name}")
    if separator and raw_anchor:
        try:
            page_text = page_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise WikiError("invalid_source_reference", f"Не удалось прочитать страницу wiki: {exc}") from exc
        if unquote(raw_anchor) not in _markdown_anchors(page_text):
            raise WikiError("invalid_source_reference", f"Якорь страницы wiki не найден: {wiki_page}")
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
        raise WikiError("invalid_source_reference", f"Источник {source_id}@{revision} не прошёл проверку") from exc
    try:
        snapshot = safe_managed_path(root, manifest["local_path"])
    except WikiError as exc:
        raise WikiError("invalid_source_reference", "Сохранённый путь источника стал небезопасным") from exc
    if snapshot.suffix.lower() not in TEXT_SUFFIXES:
        raise WikiError("invalid_source_locator", f"Для формата {snapshot.suffix or '(без расширения)'} нельзя проверить точный locator")
    if not isinstance(locator, str):
        raise WikiError("invalid_source_locator", "locator должен быть строкой")
    locator_value = locator
    if locator.startswith("section:"):
        locator_value = "section:" + unquote(locator.partition(":")[2])
    match = SOURCE_LOCATOR_RE.fullmatch(locator_value)
    if not match:
        raise WikiError("invalid_source_locator", f"Неподдерживаемый locator: {locator}")
    try:
        source_text = snapshot.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise WikiError("invalid_source_locator", f"Не удалось прочитать текст источника: {exc}") from exc
    if match.group(1):
        first = int(match.group(1))
        last = int(match.group(2) or first)
        if first < 1 or last < first or last > len(source_text.splitlines()):
            raise WikiError("invalid_source_locator", f"Цитата {locator} выходит за строки источника")
    else:
        if snapshot.suffix.lower() != ".md" or match.group(3) not in _markdown_anchors(source_text):
            raise WikiError("invalid_source_locator", f"Раздел {locator} не найден в Markdown-источнике")
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
            report["errors"].append({"code": "unsafe_markdown_link", "path": rel, "target": target, "message": "Локальная цель выходит за корень wiki"})
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
            report["errors"].append({"code": "broken_markdown_link", "path": rel, "target": target, "message": "Локальная цель ссылки не найдена"})
            continue
        if anchor and target_path.suffix.lower() == ".md":
            try:
                target_text = target_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                report["errors"].append({"code": "unreadable_markdown_link", "path": rel, "target": target, "message": "Не удалось прочитать Markdown-цель"})
                continue
            if anchor not in _markdown_anchors(target_text):
                report["errors"].append({"code": "broken_markdown_anchor", "path": rel, "target": target, "message": "Якорь в локальной Markdown-ссылке не найден"})


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
            report["errors"].append({"code": "invalid_manifest", "path": "sources/manifests", "message": "Некорректный source_id или revision"})
            continue
        manifest_by_key[(source_id, revision)] = record
        source_ids_by_revision.setdefault(source_id, []).append(record)

    for path in _walk_files(base, "sources/manifests", {".json"}, issue=record_path_issue):
        rel = _relative(base, path)
        match = MANIFEST_NAME_RE.fullmatch(path.name)
        if not match:
            report["errors"].append({"code": "invalid_manifest_name", "path": rel, "message": "Имя должно быть <source_id>--<sha256>.json"})
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue  # _source_records already recorded the parse error.
        if not isinstance(record, dict):
            continue
        source_id, revision = match.group("source"), match.group("revision")
        if record.get("source_id") != source_id or record.get("revision") != revision:
            report["errors"].append({"code": "manifest_name_mismatch", "path": rel, "message": "Имя манифеста не совпадает с его source_id/revision"})
        missing = {"source_id", "revision", "kind", "origin", "upstream_revision", "sha256", "captured_at", "local_path", "scope", "supersedes", "derived_from"} - record.keys()
        if missing:
            report["errors"].append({"code": "invalid_manifest", "path": rel, "message": "Отсутствуют поля: " + ", ".join(sorted(missing))})
        if record.get("sha256") != revision:
            report["errors"].append({"code": "manifest_hash_mismatch", "path": rel, "message": "sha256 должен совпадать с revision"})
        if not isinstance(record.get("kind"), str) or record.get("kind") not in SOURCE_KINDS:
            report["errors"].append({"code": "invalid_source_kind", "path": rel, "message": "Неизвестный kind источника"})
        local_path = record.get("local_path")
        expected_prefix = f"sources/raw/{source_id}/"
        if not isinstance(local_path, str) or not local_path.startswith(expected_prefix):
            report["errors"].append({"code": "unsafe_source_path", "path": rel, "message": "local_path должен находиться в каталоге источника"})
            continue
        try:
            snapshot = safe_managed_path(base, local_path)
        except WikiError as exc:
            report["errors"].append({"code": "unsafe_source_path", "path": rel, "message": exc.message})
            continue
        if not snapshot.is_file():
            report["errors"].append({"code": "missing_source_snapshot", "path": local_path, "message": "Снимок источника не найден"})
        else:
            try:
                actual = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            except OSError as exc:
                report["errors"].append({"code": "source_read_error", "path": local_path, "message": str(exc)})
            else:
                if actual != revision:
                    report["errors"].append({"code": "source_hash_mismatch", "path": local_path, "message": "Содержимое снимка не совпадает с sha256"})
        if snapshot.suffix.lower() not in TEXT_SUFFIXES:
            report["warnings"].append({"code": "unsupported_source_format", "path": local_path, "message": "Хеш проверен, но формат не входит в поддерживаемый текстовый набор"})
        if record.get("kind") == "code_summary":
            report["warnings"].append({"code": "derived_summary_not_source_code", "path": local_path, "message": "Зарегистрировано производное саммари; эта запись не подтверждает проверку исходного кода"})
        if record.get("kind") == "code_summary":
            report["warnings"].append({"code": "derived_summary_not_source_code", "path": local_path, "message": "Зарегистрировано производное саммари; эта запись не подтверждает проверку исходного кода"})

    for source_id, records in source_ids_by_revision.items():
        revisions = {str(record.get("revision")) for record in records}
        for record in records:
            previous = record.get("supersedes")
            if previous is not None and (not isinstance(previous, str) or previous not in revisions):
                report["errors"].append({"code": "broken_source_history", "path": "sources/manifests", "message": f"{source_id} supersedes отсутствующую ревизию {previous}"})
            derived_from = record.get("derived_from", [])
            if not isinstance(derived_from, list):
                report["errors"].append({"code": "invalid_derived_from", "path": "sources/manifests", "message": f"{source_id} derived_from должен быть списком"})
            else:
                for reference in derived_from:
                    if not isinstance(reference, dict):
                        report["errors"].append({"code": "unknown_derived_source", "path": "sources/manifests", "message": f"{source_id} содержит неизвестную derived_from ссылку"})
                        continue
                    ref_id, ref_revision = reference.get("source_id"), reference.get("revision")
                    if not isinstance(ref_id, str) or not isinstance(ref_revision, str) or (ref_id, ref_revision) not in manifest_by_key:
                        report["errors"].append({"code": "unknown_derived_source", "path": "sources/manifests", "message": f"{source_id} содержит неизвестную derived_from ссылку"})
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
                    "message": "Содержимое raw-файла не совпадает с текущей зарегистрированной ревизией",
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
                report["errors"].append({"code": "unknown_page_dependency", "path": _relative(base, page), "message": f"Зависимая страница не найдена: {dependency}"})

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
        raise WikiError("invalid_query", "Поисковый запрос должен содержать текст")
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


def search(root: Path, query: str) -> list[dict[str, Any]]:
    """Search Markdown pages and the current registered text source revisions."""
    base = _root(root)
    if not isinstance(query, str) or not query.strip():
        raise WikiError("invalid_query", "Поисковый запрос должен содержать текст")
    from .raw import discover_raw_sources

    discover_raw_sources(base)
    results: list[dict[str, Any]] = []

    for path in _walk_files(base, "wiki", {".md"}):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        match = _searchable_match(text, query)
        if match is None:
            continue
        score, snippet, locator = match
        results.append({
            "type": "wiki_page",
            "id": _frontmatter_id(text, path.stem),
            "kind": "wiki_page",
            "path": _relative(base, path),
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
        try:
            manifest = get_manifest(base, source_id, current_revision)
            local_path = manifest["local_path"]
            snapshot = safe_managed_path(base, local_path)
            if snapshot.suffix.lower() not in TEXT_SUFFIXES:
                continue
            text = snapshot.read_text(encoding="utf-8")
        except (WikiError, OSError, UnicodeDecodeError, KeyError):
            continue
        match = _searchable_match(text, query)
        if match is None:
            continue
        score, snippet, locator = match
        results.append({
            "type": "source",
            "id": source_id,
            "kind": manifest.get("kind"),
            "path": local_path,
            "revision": current_revision,
            "locator": locator,
            "snippet": snippet,
            "score": score,
        })

    results.sort(key=lambda item: (-item["score"], item["path"], item["id"]))
    return results
