# Local LLM Wiki

A local knowledge base for business processes, research, and project notes. The Python CLI registers immutable source snapshots, searches Markdown, stores feedback in SQLite, and launches a separate headless OpenCode profile to answer questions or prepare proposals. Answers should show their evidence, gaps, and conflicts. The wiki is updated after a person reviews the proposal and creates a Git commit.

The first version does not install a daemon or publish changes automatically. Unprocessed feedback and the queue live in `.state/queue.sqlite3`; this file is not tracked in Git and is part of the local data.

## Requirements and setup

- macOS or Linux; on Windows, use WSL2. The maintenance lock and child-process handling use POSIX mechanisms.
- `uv` installed. See the [official uv installation guide](https://docs.astral.sh/uv/getting-started/installation/).
- The tested version is OpenCode 1.18.18. Install and configure it separately from the Python project; see [official installation](https://opencode.ai/docs/) and [CLI commands](https://opencode.ai/docs/cli/). To answer questions, configure provider authorization with `opencode auth login`. Profile and command configuration was checked against OpenCode v1; compatibility with newer configuration formats has not been confirmed. `uv sync` does not install OpenCode or configure model access.

From the root of the cloned repository, install the environment and package. `.python-version` selects Python 3.12; if that version is not installed, uv will install it. `uv.lock` pins the resolved Python dependencies, and `--locked` prevents commands from changing the lockfile:

```sh
uv sync --locked
uv run --locked wiki --root . lint
```

The project supports Python 3.11 and later. The Python CLI currently requires no third-party runtime packages; uv also installs the project and its `wiki` command into the local `.venv/`.

From another working directory, pass `--project` to `uv` to use this repository's environment and `--root` to select the wiki itself. This `uv --project` option is different from `--project atlas` on the `wiki ask`, `wiki search`, or `wiki source add` subcommand, which selects knowledge scope:

```sh
uv run --project /path/to/llm-wiki-work --locked wiki --root /path/to/llm-wiki-work lint
```

Configure the model once in the `[ask]` section of `wiki.toml`. Use a `provider/model` ID shown by `opencode models`:

```toml
[ask]
model = "provider/model"
agent = "librarian"
timeout_seconds = 120
max_steps = 8
```

Replace `provider/model` with the model chosen for this wiki. There is no default model; an empty `model` intentionally results in `configuration_error`. You may add `variant` if the provider supports it. Authorization keys are stored through OpenCode's standard mechanism; do not put secrets in repository files.

## Use the client skill from another OpenCode project

The portable [LLM Wiki client skill](skills/llm-wiki-client/SKILL.md) lets an external calling agent consult this wiki and submit feedback autonomously when project or domain knowledge matters to its task; the user need not separately request a query or feedback report. From this repository's root, copy it into the target project's local OpenCode skills directory:

```sh
mkdir -p "/path/to/agent-project/.opencode/skills/llm-wiki-client"
cp -n "skills/llm-wiki-client/SKILL.md" "/path/to/agent-project/.opencode/skills/llm-wiki-client/SKILL.md"
export LLM_WIKI_ROOT="/absolute/path/to/llm-wiki-work"
export LLM_WIKI_PROJECT="atlas" # optional trusted project ID for this agent context
```

Replace the paths with their actual absolute paths; run the exports in the target agent's launch environment or set them in local, untracked project instructions. `LLM_WIKI_PROJECT` is optional and must be a trusted lowercase slug; the CLI does not read it automatically, and the client skill passes it to wiki commands. Keep machine-specific paths out of committed files. OpenCode loads the skill from `.opencode/skills/llm-wiki-client/SKILL.md`; see the [OpenCode skills guide](https://opencode.ai/docs/skills/).

For a global agent instruction, use the short [prompt fragment](prompts/llm-wiki.md). The client skill can save reusable project research notes as `unclassified` source snapshots; notes created or rewritten by the agent are marked `ai-generated`. Those notes are searchable after registration, while `ingest` is queued work and does not publish wiki pages. A trusted project ID can be established by the task or `LLM_WIKI_PROJECT`; the CLI itself does not read the environment variable. Project IDs are lowercase ASCII slugs.

## Ordinary use

To add material without a separate import command, copy a regular file with a file manager or editor into `sources/raw/` or an ordinary subdirectory. Wait for the copy to finish, then ask the calling agent a question; it will call the configured Python CLI route, which discovers the file before `wiki ask`. The `/wiki-ask` shortcut is also available. For each discovered file, Python leaves the original in place, creates an immutable hash snapshot and manifest, assigns kind `unclassified`, and records authorship declared by the first directory below `sources/raw/`. A first directory named `ai-generated` or `human-written` declares that authorship; files directly in `sources/raw/` or under another first directory are `unknown`. A `projects/<slug>` folder immediately below `sources/raw/` or an authorship folder declares project scope; the recognized forms are `sources/raw/ai-generated/projects/<slug>/...`, `sources/raw/human-written/projects/<slug>/...`, and `sources/raw/projects/<slug>/...`. The last form has `unknown` authorship. Other raw paths are general; do not infer project from names or text. Folder labels initialize scope/authorship on a new revision; repeated discovery preserves the existing manifest value, including legacy `null` project, without rewriting it. Folder placement is a declaration, not independent verification. Users do not need to run `wiki source add` or `wiki ingest` for such a file.

Discovery runs during ordinary `wiki search`, `wiki ask`, or `wiki maintenance run` commands, not when the file is copied; there is no continuously running watcher. Lint remains read-only and warns about files that have not yet been registered. Do not edit hash-named snapshots; to add a revision to the same source, change the original file at its existing relative path and re-supply the intended `--authorship` label. A new revision with the same `source_id` is linked to the previous one through `supersedes`, and questions use the tip of that chain by default. Moving a raw file changes its path-based ID and creates a separate source; it does not relabel or remove the old source. Kind `unclassified` does not establish a normative rule or publish a wiki page.

For a separate import with a chosen source ID, kind, authorship, and project, or for files outside `sources/raw/`, explicitly invoke `/wiki-ingest` or use the corresponding `source add` and `ingest` commands. An explicit `/wiki-ingest` guides one newly queued job through proposal review; direct `wiki ingest` and routine research capture remain queue-only. `authorship` is independent of source kind and page review status; `--authorship` accepts `human-written`, `ai-generated`, or `unknown`. Omitted authorship on any new revision means `unknown`; re-supply it when revising a manual import. Omission on duplicate registration of the identical current revision preserves the value. `--project` accepts a lowercase ASCII slug and stores `scope.project`. A new source without it is general; changed content under the same source ID inherits the prior project when omitted, while an explicit flag changes the project on that revision. Omission on an identical current duplicate preserves the project. A conflicting explicit authorship or project label for identical bytes returns `source_metadata_conflict` without rewriting. There is no flag to silently clear project scope; use a separate source ID for general material. Old manifests without authorship mean `unknown`, and missing project means general; they are not mass-rewritten. Files in `examples/` are synthetic and excluded from search until explicitly registered. The complete guide to automatic discovery, project scope, authorship checks, five control questions, and the manual review cycle is in [`docs/manual-testing.md`](docs/manual-testing.md).

## Project-scoped capture and retrieval

Use `--project` on `wiki source add`, `wiki search`, or `wiki ask` to route sources and questions. For example, an agent in project `atlas` can save a brainstorm or Jira story draft as a note with provenance such as its author, date, Jira key/URL, decisions, and proposed acceptance criteria. Keep ideas and draft criteria labeled as drafts; the CLI does not create or update Jira issues.

```sh
uv run --locked wiki --root . source add notes/atlas/refund-brainstorm.md \
  --id atlas-refund-brainstorm-01 --kind unclassified \
  --authorship ai-generated --project atlas --origin research:atlas/refund
uv run --locked wiki --root . ingest atlas-refund-brainstorm-01 REVISION
uv run --locked wiki --root . ask --project atlas \
  'Summarize the refund brainstorm and draft Jira story; distinguish ideas from approved decisions.'
```

`wiki search --project atlas -- QUERY` and `wiki ask --project atlas "QUESTION"` include `atlas` sources/pages and general ones, with the project group first. Use `--` when the question begins with an option-like token. Without a project, they include all projects and prioritize general material. Scope group order comes before textual relevance; the existing score and human-authorship tie-break apply within each group. This ranking guides retrieval and does not determine truth or resolve conflicts. If an `atlas` inventory has no relevant Atlas evidence, general sources can provide shared background but not the project's recorded ideas, decisions, or stories. Unscoped answers should qualify project-specific findings. A request file may set `scope.project`; if both it and the CLI flag are provided, they must match. `uv --project` selects the Python environment; `--project atlas` on the individual `wiki ask`, `wiki search`, or `wiki source add` subcommand selects knowledge scope. Project scoping is not an access-control boundary; this first version still serves one audience with access to all registered material. Omit project scope for intentionally general or cross-project questions.

Use `ai-generated` for notes an agent creates, synthesizes, or rewrites, even after a person reviews or manually copies them. Use `human-written` only for a human-authored original copied unchanged when its provenance is known; otherwise use `unknown`. Neither folder placement nor a CLI label verifies authorship; there is no writing-style detector or text marker. Do not guess `human-written` or a source kind such as `procedure` from style. Unknown authorship gets no human preference. Human-written sources do not automatically establish truth, policy, or applicability. Give AI-generated evidence less weight than comparable applicable human primary sources and independently verified upstream evidence, while preserving scope, revisions, and conflicts; do not assign numeric trust scores or automatically resolve conflicts in favor of human-written sources. An AI note can support an attributed claim about what the note says, but AI-only evidence for an external fact leaves that fact `inferred` or `unknown` until corroborated. Copies, rewrites, and citations of the same AI material are not independent corroboration. Reviewing a page or approving a proposal does not upgrade the original source authorship.

`wiki search` source results include a flat `authorship` field alongside `type: source`; human-written sources break ties only when lexical relevance is equal. Python adds verified `project` and `authorship` metadata to accepted answer citations and proposal evidence.

To answer a question, configure the model in `[ask]` in `wiki.toml`. Run `uv run --locked wiki --root . lint`, then send the calling agent an ordinary question or use `/wiki-ask`. The CLI validates the answer and records `answer_id` and `wiki_revision`, a SHA-256 digest of the wiki contents and registered sources. This digest is not a Git commit SHA.

For feedback, pass the actual `answer_id` and `wiki_revision` through `/wiki-feedback` or `wiki feedback submit`. `/wiki-maintain` with no action first reads saved proposals; if one is ready, the calling agent presents it and waits before doing more work. If none is ready, it prepares at most one pending job. Use `/wiki-maintain review [--id ID]` for read-only review. The calling agent uses the default JSON report internally for status, `ready_job_ids`, and `review_fingerprint`, then renders a readable proposal; `--format markdown` is an optional direct view and may omit machine fields. `ready_for_review` means a proposal is waiting for a human decision, not published. Proposals are shown one at a time with a focused summary, project/files, readable diff, exact evidence, and doubts. Reply `accept`, `revise`, or `defer`; do not open or edit proposal JSON.

For `revise`, the calling agent updates the proposal and presents a new diff; the previous decision does not apply. For `defer`, it leaves the proposal and queue unchanged. Before applying an `accept`, the calling agent refreshes the default JSON review and compares `review_fingerprint` internally; if it changed, it presents the new review and waits for a fresh decision. Before applying the contents, it inspects staged and unstaged diffs on affected paths, discloses edits that would be overwritten or included in a commit, and gets agreement before proceeding. For `outcome: proposed`, the caller applies exactly the accepted contents and reconciles `wiki/index.md` and `wiki/log.md` as needed. After applying, it rechecks affected-path diffs before staging, then stages only approved paths and necessary snapshots/manifests. Immediately before committing, it inspects the staged diff again and preserves unrelated staging. If current `HEAD` already contains the exact agreed contents, no empty commit is needed; pass that full SHA to completion. Otherwise complete with the new commit's full SHA:

```sh
uv run --locked wiki --root . maintenance complete --id JOB_ID --revision COMMIT_SHA
```

The CLI checks that the agreed content is present in the specified current commit. For accepted `rejected` or `needs_evidence` outcomes, no Git revision is needed: complete the reviewed job with `maintenance complete --id JOB_ID`. Deferring or revising is not the semantic `rejected` outcome. Do not publish an unsupported correction or push unless requested. The `.state/queue.sqlite3` queue is local and is not included in Git; back it up using SQLite's backup API.

## Verification

Run the standard-library test suite from the repository root:

```sh
uv run --locked python -m unittest discover -s tests
```

## Backing up the queue

Git does not store the SQLite queue. For a consistent backup, use SQLite's backup API instead of copying an open database file. This example temporarily saves a copy in `.state/backups/`, which is excluded from Git; after creating the copy, move it to the chosen backup location:

```sh
uv run --locked python - <<'PY'
import sqlite3
from pathlib import Path

backup_path = Path(".state/backups/queue-backup.sqlite3")
backup_path.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(".state/queue.sqlite3") as source:
    with sqlite3.connect(backup_path) as backup:
        source.backup(backup)
PY
```

After copying it, store the backup separately from the repository in a protected local directory. Check access permissions: records may contain questions and business context.

## First-version limits

Registration preserves the original file and its SHA-256, but does not prove that its contents are reliable. Search and lint work without an LLM; automatic conversion of arbitrary workflows and Confluence exports is not promised. A code summary is not equivalent to checking the source code. Answers apply to one audience whose members have the same access to all registered material.

Maintenance is started manually and bounded by `--limit`. It creates a proposal, but does not provide a daemon, scheduler, automatic publication, or recovery from a publication failure. Scheduling, a network API, vector search, and multiple audiences are deferred.
