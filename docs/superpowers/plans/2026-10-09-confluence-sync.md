# Confluence Sync Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development. Luna Max implements; the parent agent reviews and integrates.

**Goal:** Refresh Confluence pages declared in `sources/raw/human-written/confluence/` during maintenance and prepare reviewed wiki updates.

**Architecture:** OpenCode reads Confluence through its existing MCP connection. Python parses link lists, validates responses, versions snapshots, and queues ingest. The existing maintainer synthesizes proposals against registered evidence.

**Tech Stack:** Python 3.11+ standard library, SQLite, uv, OpenCode, Atlassian MCP.

**Spec:** `docs/superpowers/specs/2026-10-09-confluence-sync.md`

## Global constraints

- Keep code, documentation, and Python messages in English.
- Preserve the original checkout's five staged IDE files.
- Only the designated directory enables remote fetching; no recursive remote crawling.
- All publication follows human proposal review; no live model calls during automated checks.
- Preserve current general/project restrictions, immutable evidence, and the configured model variant.

## Review focus

- A locally unchanged link file must still trigger a remote check during maintenance.
- Removing a link or losing MCP access must not destroy prior evidence.
- Equivalent URLs and restored content must not create ambiguous source histories.
- Global OpenCode MCP access must survive the sync launch while other wiki agents remain read-only.
- An old ready proposal must not overwrite a newer synchronized source revision.

## Task 1: OpenCode source reader

Files: `config.py`, `runner.py`, new `wiki-source-sync.md`, config/runner tests.

- [x] Add tests for `load_config(root, purpose="confluence")`, profile selection, and read-only MCP permission boundaries.
- [x] Verify installed OpenCode configuration behavior without calling a model; implement the source-reader profile and launcher configuration.
- [x] Verify focused tests and document any live-integration limitation.

## Task 2: Link discovery and snapshots

Files: new `confluence.py`, `raw.py`, confluence/raw tests.

Interfaces: `discover_confluence_links(root) -> {pages, errors}` and `sync_confluence(root, execute) -> {checked, changed, unchanged, items, errors}`. Each model response is an `ok` page object or an `error` object. Sync owns the maintenance writer lock.

- [x] Test exact-directory discovery/exclusion, plain and Markdown URLs, deduplication, invalid inputs, and safe paths.
- [x] Test unchanged checks, changed/reverted content, no-state recovery, partial failure, and superseded queue entries.
- [x] Implement validated snapshot registration and queueing with no wiki-page edits; run focused tests.

## Task 3: Maintenance integration and guidance

Files: `maintenance.py`, `cli.py`, maintenance/CLI tests, maintain skill/command, AGENTS, README, manual test guide.

- [x] Test automatic ordinary-run sync, explicit sync, skip/target/read-only routing, error exit status, and empty-directory operation without model configuration.
- [x] Supply verified inventory and affected-page dependencies within existing project boundaries; test superseded Confluence retry/completion rejection.
- [x] Update the calling-agent flow and manual scenarios; verify there is one sync pass per ordinary maintenance invocation.

## Integration

- [x] Review each task's implementation and real behavior, then run the complete test suite, wiki lint, skill validation, and whitespace checks.
- [x] Exercise the actual CLI with deterministic responses and preserve a clear distinction from live MCP verification.
Delivery follows verification: commit the intended files, fast-forward master, preserve IDE staging, push, and archive the managed worktree.

Verification on 2026-10-09: all 162 tests passed; wiki lint had no errors or warnings; both changed skills passed validation. A deterministic CLI scenario covered remote changes, deduplication, superseded proposals, and fetch failures. Native OpenCode loaded the source-reader profile and its limited read permissions. Live Atlassian MCP fetching was not run because this environment has neither a configured model nor an Atlassian MCP connection.
