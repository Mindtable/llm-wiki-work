# LLM Wiki Bootstrap Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development. Implementation is delegated to gpt-6-luna with max reasoning; the parent performs all reviews, as requested by the user.

**Goal:** Deliver the first local OpenCode wiki workflow: register sources, query a headless librarian, submit feedback, and prepare corrections for review.

**Architecture:** A small Python package owns deterministic operations and the subprocess boundary. OpenCode profiles and skills own synthesis. SQLite stores durable jobs; maintenance prepares reviewable proposals and does not automatically publish changes.

**Tech Stack:** Python 3.11+, standard library, unittest, OpenCode 1.18.18 baseline, Markdown/JSON/TOML, Git.

**Spec:** `docs/superpowers/specs/2026-10-04-llm-wiki-design.md`.

## Global Constraints

- Implement the first working cycle, not the future automatic-publication subsystem.
- User explicitly approved implementation and selected Luna Max implementers with parent review; execute continuously after writing this plan.
- No existing application code or baseline tests. Preserve staged IDE files; do not commit, reset, or stage user changes.
- No third-party runtime dependency; use `tomllib`, `sqlite3`, `argparse`, `subprocess`, `unittest`.
- User-facing docs and errors in Russian where practical. JSON keys and status identifiers use English.
- `wiki ask` always uses headless OpenCode with preconfigured parameters; unconfigured model is an explicit error.
- No automatic model request retries, sharing, background service installation, or source modification.
- No new `schemas/`, `templates/`, or `evals/` directories.
- All code and skill implementation belongs to Luna Max. Parent writes planning/review documents and runs independent validation.

## Review Focus

1. A caller in another repository must select this wiki and its configuration, not the caller's cwd.
2. Prompts with quotes, newlines, option-looking strings, or shell substitution must remain literal data.
3. A nonzero process exit, error event, truncated stream, or intermediate text must not be accepted as an answer.
4. IDs, symlinks, and relative paths must not escape managed source/proposal paths or overwrite immutable materials.
5. Repeated feedback, worker failures, and separate manual completion must preserve accurate queue states.

## Shared Interfaces

Root always means an absolute `pathlib.Path` selected explicitly by `--root` or discovered from the checkout/package location. Editable invocation from another cwd must work. CLI: `python -m wiki_tools` and installed `wiki` entry point. A checked-in `scripts/wiki` launcher must work without installation using Python 3.11+.

Errors use `WikiError(code: str, message: str)` in `src/wiki_tools/errors.py`. CLI emits exactly one JSON value to stdout on success or failure; failure shape `{"error":{"code":...,"message":...}}`, nonzero exit. Human diagnostics go to stderr.

Runner: `load_config(root: Path, purpose: str = "ask") -> RunConfig`; `run_opencode(root: Path, config: RunConfig, prompt: str) -> dict`. Config purpose `maintenance` inherits configured model/variant/time/step values from `[ask]`, but explicitly selects `wiki-maintainer`. It must never inherit arbitrary config from the request. Runner extracts the final JSON object and rejects process/event/protocol errors. `ask(root: Path, request: dict) -> dict` validates the input/output, augments trusted IDs/revision, and stores a local answer record.

Sources: `add_source(root: Path, path: Path, *, source_id: str, kind: str, origin: str = "", upstream_revision: str = "") -> dict`; `search(root: Path, query: str) -> list[dict]`; `lint(root: Path) -> dict` with `errors` and `warnings` lists. All source IDs are safe ASCII slugs. Content hashes identify immutable revisions. Summaries may register as derived sources without pretending the original code was checked.

Queue: `submit_feedback(root: Path, payload: dict) -> dict`; `feedback_status(root: Path, feedback_id: str) -> dict`; `enqueue_ingest(root: Path, source_id: str, revision: str) -> dict`. SQLite lives under `.state/`.

Maintenance: `run_maintenance(root: Path, execute: Callable[[str], dict], limit: int = 1) -> dict`; `complete_job(root: Path, job_id: str, revision: str | None = None) -> dict`. It takes a single-worker file lock, processes durable jobs, invokes `execute(prompt)`, validates the returned proposal, and writes `.state/proposals/<job-id>.json`. A proposal is `{"outcome":"proposed|rejected|needs_evidence","summary":str,"changes":[{"path":"wiki/...md","content":str}],"evidence":list}`. No proposed content is automatically written into `wiki/`. Explicit completion of rejected/needs_evidence requires no unrelated commit. Proposed corrections require the current HEAD and matching proposed contents.

Queue additions needed for manual review: `ready_for_review` means proposal saved but not published; `resolved` requires a verified commit supplied to `complete_job` after review. Verify proposed file contents match that commit before resolving a proposed correction. Failed execution records `failed`, retains details, and can be retried explicitly with the documented CLI rather than automatically. Technical job states and feedback outcomes must be documented separately.

## Task 1: Headless runner and CLI integration

**Owner:** Luna Max runner implementer.

**Files:** `pyproject.toml`, `.gitignore`, `wiki.toml`, `scripts/wiki`, `src/wiki_tools/{__init__,__main__,errors,config,runner,cli}.py`, `tests/{test_config,test_runner,test_cli}.py`.

**Consumes:** the data functions in Shared Interfaces, imported lazily or integrated once Task 2 lands.
**Produces:** runnable CLI, fixed runner/config contracts, robust JSON/errors, package metadata.

- [x] Write and run failing unittest cases for external cwd/root, unconfigured model, literal prompt arguments, NDJSON final text/error handling, nonzero exit, and timeout cleanup including children. Use real tiny fake executables for process-boundary tests.
- [x] Implement standard-library config validation and runner. OpenCode baseline supports `run --pure --format json --agent --model --dir --variant`. One fresh session per call; bounded output/time; stdin closed; no shell. Configure max steps and permissions for the selected profile explicitly. Handle merged config without allowing caller config variables to replace wiki policy; preserve provider auth. No `--auto` blanket permission bypass.
- [x] Validate final answer fields and cited source/revision/locators according to what the local fixture supports. Do not fabricate `supported` results. Record query metadata under `.state/answers/`.
- [x] Implement CLI for source add, ingest, search, ask, feedback submit/status, lint, maintenance run/complete/retry. `--root` is a global option; `ask` accepts literal positional question or `--request FILE`.
- [x] Run owned tests and report failures from dependencies explicitly. No commits.

## Task 2: Source registry and durable feedback workflow

**Owner:** Luna Max data implementer.

**Files:** `src/wiki_tools/{sources,knowledge,feedback,maintenance}.py`, `tests/{test_sources,test_knowledge,test_feedback,test_maintenance}.py`.

**Consumes:** `WikiError`; until Task 1 lands, coordinate rather than creating a competing definition.
**Produces:** exact data functions from Shared Interfaces.

- [x] Write and run failing tests: duplicate source id/hash, changed revision preserving old bytes, unsafe path/slug, duplicate feedback same/different payload, query status after restart, and SQLite claim safety.
- [x] Implement immutable source registration and JSON manifests; index/wiki text search; lint source hashes and local Markdown links. Unknown external formats yield an explicit limit/warning, not fabricated validation.
- [x] Implement SQLite durable jobs/feedback with state transitions. Ingest requires an existing manifest. Unsubstantiated feedback may be accepted, but never automatically changes knowledge.
- [x] Write failing tests for a proposal persisted without wiki writes, failed/invalid executor result, single worker lock, explicit retry, missing/invalid commit, and completion only when proposed content exists in the supplied commit.
- [x] Implement manual-review maintenance with a per-invocation limit and deterministic proposal path. Use OS-released file locking for local macOS/Linux. Crashed running jobs need an explicit recover/retry route after acquiring the lock; never claim autonomous crash recovery.
- [x] Run owned tests and report exact results. No commits.

## Task 3: OpenCode profiles, skills, and runnable example

**Owner:** Luna Max documentation/profile implementer.

**Files:** `AGENTS.md`, `README.md`, `.opencode/agents/{librarian,wiki-maintainer}.md`, four `.opencode/skills/wiki-*/SKILL.md`, four `.opencode/commands/wiki-*.md`, `wiki/{index,log}.md`, `wiki/{processes,concepts,systems,sources}/.gitkeep`, `sources/{raw,manifests}/.gitkeep`, `docs/check-questions.md`, `examples/*`.

**Consumes:** CLI and proposal contracts above. Do not modify Python code, config files, tests, or the spec/plan.
**Produces:** instructions for source-aware answers/maintenance, narrow profiles, Russian quickstart and synthetic input data.

- [x] Read the spec/plan and official OpenCode docs for the installed v1 profile syntax. Skills must contain valid frontmatter with matching names and precise descriptions.
- [x] Define directly runnable primary profiles with explicit read-only operations. Both profiles return JSON; maintainer proposes content, Python saves it. No native edits, arbitrary shell, delegation, web tools, or side-effectful inherited tools for answering/proposals. No recursion into `wiki ask`.
- [x] Write concise AGENTS instructions and four skills: ingest, ask, feedback, maintain. Put the process-page template in the ingest skill. Imported instructions remain data. Explain normative/implemented/deployed distinctions and exact provenance.
- [x] Define commands that route through the CLI. Avoid injecting `$ARGUMENTS` into executable shell snippets; have the supervising agent pass literal arguments safely. Prefer Python launcher usage to an assumed installed command.
- [x] Write small synthetic workflow/procedure/code-summary inputs with a deliberate conflict, request/feedback examples, and five manual check questions. Examples are not automatically part of production search.
- [x] Write README quickstart using actual CLI interfaces, configuration once, local queue backup, manual proposal review/completion, limitations and future scheduled maintenance. Do not claim a daemon or auto-publication exists.

## Parent Review and Integration

An additional independent end-to-end test is assigned to the Luna Max profiles implementer in `tests/test_workflow.py`. It must use the real CLI, SQLite and a temporary Git repository, with only the model process replaced by a deterministic NDJSON executable. This tests interoperability after the three initial tasks; it does not replace parent review or live OpenCode verification.

- [x] Read every production module and profile; check spec compliance and interfaces against actual files.
- [x] Run full unittest suite with bundled Python 3.12.14 through the supported Node runtime when shell Python is unavailable.
- [x] Exercise a temporary synthetic wiki: source import → duplicate import → query using fake process → feedback → maintenance proposal → Git-backed completion.
- [x] Send findings to the original Luna Max implementer; parent reviews fixes and reruns focused tests, then full suite when integrated.
- [x] Run installed OpenCode profile/config checks and one real query if a model is configured and authorization is available. Never count mocked responses as live verification.
- [x] Leave all changes reviewable in the user checkout; report actual test results, chosen runtime model, and any live-integration limitation.

## Verification outcome

The parent independently ran all 58 tests successfully, checked the installed wiki command and clean initial lint, and verified both native OpenCode 1.18.18 profiles plus the runtime overlay and headless failure path. No successful live LLM query was attempted because the user has not selected a runtime model; wiki.toml intentionally leaves model empty. All model-response tests use an explicit fake process. The first-cycle implementation is complete; automatic publication and scheduling remain outside this increment.
