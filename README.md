# Local LLM Wiki

A local knowledge base for business processes. The Python CLI registers immutable source snapshots, searches Markdown, stores feedback in SQLite, and launches a separate headless OpenCode profile to answer questions or prepare proposals. Answers should show their evidence, gaps, and conflicts. The wiki is updated after a person reviews the proposal and creates a Git commit.

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

From another working directory, pass `--project` to use this repository's environment and `--root` to select the wiki itself:

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
```

Replace both paths with the actual absolute paths; run the export in the target agent's launch environment or set `LLM_WIKI_ROOT` in local, untracked project instructions. Keep machine-specific paths out of committed files. OpenCode loads the skill from `.opencode/skills/llm-wiki-client/SKILL.md`; see the [OpenCode skills guide](https://opencode.ai/docs/skills/).

For a global agent instruction, use the short [prompt fragment](prompts/llm-wiki.md). The client skill can save reusable research notes as `unclassified` source snapshots; notes created or rewritten by the agent are marked `ai-generated`. Those notes are searchable after registration, while `ingest` is queued work and does not publish wiki pages. Project labels in notes and questions provide context, not strict filtering.

## Ordinary use

To add material without a separate import command, copy a regular file with a file manager or editor into `sources/raw/` or an ordinary subdirectory. Wait for the copy to finish, then ask the calling agent a question; it will call the configured Python CLI route, which discovers the file before `wiki ask`. The `/wiki-ask` shortcut is also available. For each discovered file, Python leaves the original in place, creates an immutable hash snapshot and manifest, assigns kind `unclassified`, and records authorship declared by the first directory below `sources/raw/`. A first directory named `ai-generated` or `human-written` declares that authorship; files directly in `sources/raw/` or under another first directory are `unknown`. This folder label initializes authorship only for a new source revision; repeated discovery preserves the existing revision's manifest value, including legacy `unknown`, without relabeling or rewriting it. Folder placement is a declaration, not independent verification. Users do not need to run `wiki source add` or `wiki ingest` for such a file.

Discovery runs during ordinary `wiki search`, `wiki ask`, or `wiki maintenance run` commands, not when the file is copied; there is no continuously running watcher. Lint remains read-only and warns about files that have not yet been registered. Do not edit hash-named snapshots; to add a revision to the same source, change the original file at its existing relative path and re-supply the intended `--authorship` label. A new revision with the same `source_id` is linked to the previous one through `supersedes`, and questions use the tip of that chain by default. Moving a raw file changes its path-based ID and creates a separate source; it does not relabel or remove the old source. Kind `unclassified` does not establish a normative rule or publish a wiki page.

For a separate import with a chosen source ID, kind, and authorship, or for files outside `sources/raw/`, use `/wiki-ingest` or the corresponding `source add` and `ingest` commands. `authorship` is independent of source kind and page review status; `--authorship` accepts `human-written`, `ai-generated`, or `unknown`. Omitting it for any new revision means `unknown`, including changed content under an existing source ID; re-supply the intended label when revising a manual import. Omission on duplicate registration of the identical current revision preserves the existing value. If the same current bytes are submitted with a conflicting explicit label, the CLI returns `source_metadata_conflict` without rewriting the source. Old manifests without the field mean `unknown` and are not mass-rewritten. Correct a classification with a new source version or source ID. If the same file has already been placed in `raw`, its automatically created record with a path-based ID remains separate; explicit import creates a separate source. Moving a file does not relabel or remove the old source. Files in `examples/` are synthetic and excluded from search until explicitly registered. The complete guide to automatic discovery, authorship checks, five control questions, and the manual review cycle is in [`docs/manual-testing.md`](docs/manual-testing.md).

Use `ai-generated` for notes an agent creates, synthesizes, or rewrites, even after a person reviews or manually copies them. Use `human-written` only for a human-authored original copied unchanged when its provenance is known; otherwise use `unknown`. Neither folder placement nor a CLI label verifies authorship; there is no writing-style detector or text marker. Do not guess `human-written` or a source kind such as `procedure` from style. Unknown authorship gets no human preference. Human-written sources do not automatically establish truth, policy, or applicability. Give AI-generated evidence less weight than comparable applicable human primary sources and independently verified upstream evidence, while preserving scope, revisions, and conflicts; do not assign numeric trust scores or automatically resolve conflicts in favor of human-written sources. An AI note can support an attributed claim about what the note says, but AI-only evidence for an external fact leaves that fact `inferred` or `unknown` until corroborated. Copies, rewrites, and citations of the same AI material are not independent corroboration. Reviewing a page or approving a proposal does not upgrade the original source authorship.

`wiki search` source results include a flat `authorship` field alongside `type: source`; human-written sources break ties only when lexical relevance is equal. Accepted answer citations and proposal evidence include `authorship` added by Python from the verified manifest.

To answer a question, configure the model in `[ask]` in `wiki.toml`. Run `uv run --locked wiki --root . lint`, then send the calling agent an ordinary question or use `/wiki-ask`. The CLI validates the answer and records `answer_id` and `wiki_revision`, a SHA-256 digest of the wiki contents and registered sources. This digest is not a Git commit SHA.

For feedback, pass the actual `answer_id` and `wiki_revision` through `/wiki-feedback` or `wiki feedback submit`, then run bounded manual processing with `/wiki-maintain run --limit N`. `ready_for_review` means a proposal has been saved under `.state/proposals/` and is waiting for a person. Check the evidence and full contents. For `outcome: proposed`, reconcile the pages, `wiki/index.md`, and the entry in `wiki/log.md`, create an ordinary Git commit with the snapshots and manifests, and complete the job with the full commit SHA:

```sh
uv run --locked wiki --root . maintenance complete --id JOB_ID --revision COMMIT_SHA
```

The CLI checks that the agreed content is present in the specified commit. For `rejected` or `needs_evidence`, no Git revision is needed: complete the reviewed job with `maintenance complete --id JOB_ID`. Do not publish an unsupported correction. The `.state/queue.sqlite3` queue is local and is not included in Git; back it up using SQLite's backup API.

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
