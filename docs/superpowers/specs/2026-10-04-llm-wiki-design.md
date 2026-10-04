# Local LLM Wiki for Business Processes

Date: 2026-10-04. Status: The user authorized implementation of the first working cycle; automated maintenance remains a later phase.

## Purpose and Scope

The repository accumulates verifiable knowledge about business processes from workflow definitions, procedures, Confluence exports, and source-code analyses. Calling agents ask a librarian for answers with evidence and submit discovered discrepancies for asynchronous review.

The user confirmed that agents may access this repository locally and work through OpenCode. The user also chose to invoke OpenCode in headless mode through `wiki ask` with preconfigured parameters. Markdown and Git are proposed for the knowledge base, a local Python CLI for supporting operations, and SQLite for the queue. No separate network service is required.

The first version should take one business process through a complete cycle: source registration → page synthesis → question → feedback → review → updated answer. It must not automatically resolve a business dispute when evidence is insufficient.

The librarian is invoked through OpenCode. A prepared prompt or search results alone do not count as a librarian answer.

## Development Sequence

**Bootstrap and first working cycle:** OpenCode instructions and commands, source tracking, answers with evidence, feedback intake, and manually launched review. Review runs separately after feedback is submitted and does not block the calling agent. A person reviews the first wiki update and records it in a normal Git commit. Feedback intake and its resolution remain separate events.

This phase includes minimal format validation in Python, stable feedback IDs, and statuses. It does not promise automatic publication, recovery after a crash during publication, or atomicity across multiple working-tree changes. The wiki must not be edited concurrently with the first manual review. The review result is recorded separately, and `resolved` is set only after a verified commit.

**Automated maintenance:** an immutable snapshot per request, isolated change preparation, a writer lock, retries and crash recovery, automatic publication, and scheduled runs. The sections below that describe these guarantees define a later phase, not acceptance criteria for the initial bootstrap.

This separation makes it possible to validate the approach on one workflow first. Including automated maintenance in the current implementation requires separate scope approval.

## OpenCode Integration

Agent definitions live in `.opencode/agents/`, skills in `.opencode/skills/`, and commands in `.opencode/commands/`. The librarian answers in read-only mode; the maintenance agent prepares changes.

### Headless Invocation Through `wiki ask`

`wiki ask` is the single entry point for calling agents. It starts a separate `opencode run` process without a TUI, interactive dialog, or connection to a server that was started in advance. Each question creates a new session; the previous calling agent's history is not added automatically.

The calling agent passes the question and context. It cannot choose the model, agent, arbitrary command, working directory, or permissions through request content. Parameters are configured in advance in the `[ask]` section of `wiki.toml`:

| Field | Purpose |
| --- | --- |
| `executable` | OpenCode executable; defaults to `opencode` |
| `agent` | Predefined agent; defaults to `librarian` |
| `model` | Model explicitly selected during setup, in `provider/model` format |
| `variant` | Optional model variant, if the provider supports it |
| `timeout_seconds` | Maximum duration of one run |
| `max_steps` | Maximum number of agent steps |

The owner sets the model ID during initial setup. If it is not configured, the command returns `configuration_error` instead of selecting the last model from the user's session. A timeout and step count are not monetary limits. Provider keys stay in OpenCode's existing authentication mechanism or the environment, outside tracked repository files.

The `librarian` agent must support direct invocation as a primary agent; a subagent-only profile is unsuitable. Its instructions and allowed read operations are defined in `.opencode/agents/librarian.md`. Settings from `wiki.toml` are applied to the CLI and that run's configuration, without duplicating the model in multiple files.

Example shape of the internal call, with values supplied from configuration:

```text
opencode run --pure --format json --agent librarian --model <configured-model> --dir <wiki-root> -- <request-prompt>
```

Python passes arguments as a list without shell interpolation. The question and context are serialized as data in a fixed prompt. `variant` is added only when configured. The wiki root is determined from the installed bootstrap's location or an explicitly selected configuration, not from the calling agent's current directory. In the second phase, the run profile will use a prepared snapshot of the published revision instead of the working tree.

OpenCode configurations are merged, so selecting an agent name alone is not enough for reproducibility. The wrapper sets run parameters explicitly and does not accept variables from the calling agent that override OpenCode configuration. Existing provider authentication is preserved; the wiki profile defines the librarian's permissions and tools and disables sharing. `--pure` excludes external plugins but is not considered complete configuration isolation. Inherited connections and user tools must not expand the librarian's allowed capabilities. Administrative policies are not bypassed; an incompatible profile produces a configuration error.

stdin is closed; operations have preassigned permissions and do not wait for confirmation. `wiki ask` does not start interactive authentication. On timeout or cancellation, the entire process group is terminated so the request cannot continue in the background.

The CLI's JSON mode emits session events, not the final answer contract. The wrapper separates service events and intermediate text, extracts the final answer, and validates its fields. Success requires a successful process exit, no error event in the stream, and a valid final answer. `wiki ask` writes one result JSON object to stdout and diagnostics to stderr. Configuration, launch, authentication, timeout, and invalid-answer errors produce a nonzero exit code and a distinct `error.code`. The first cycle does not automatically retry paid requests.

For an agent working in another repository, this project's wiki settings are not assumed to be loaded automatically. It invokes the local `wiki ask` command, which selects the directory and OpenCode profile itself. The project command `/wiki-ask` uses the same wrapper, avoiding a second answer path with different settings. The librarian does not call `wiki ask` recursively. The other project commands are `/wiki-ingest`, `/wiki-feedback`, and `/wiki-maintain`.

On this machine, `opencode --version` and `opencode run --help` were checked: version **1.18.18** is installed, and the help output confirms the listed CLI flags, including `--pure`. Retrieving the help output required an available temporary directory through `TMPDIR`. A model call, effective permissions, and the format of a real event stream had not yet been verified; these are part of implementation validation.

## Knowledge Model

### Source Materials

`sources/raw/` stores immutable snapshots of received materials. A new version creates a new snapshot; the old one is never overwritten. Immutability preserves evidence; it does not establish that the content is true.

Each source has a separate JSON manifest in `sources/manifests/`:

- `source_id` — stable identifier for a logical source;
- `revision` — identifier for a specific snapshot;
- `kind` — `workflow`, `procedure`, `confluence_export`, `code_summary`, or `code_excerpt`;
- `origin` — the original address, or repository and path;
- `upstream_revision` — the page version, commit, or definition version, if known;
- `sha256`, `captured_at`, `local_path`;
- `scope` — domain, product, environment, and applicable period, if known;
- `supersedes` — the previous revision replaced by this snapshot;
- `derived_from` — references to source materials for derived documents.

Unknown versions or environments are stated explicitly. An export timestamp does not stand in for a procedure's effective date. The current snapshot is determined by explicit revision links, not by file modification time.

Code summaries require the source commit and references to specific files and symbols. If the librarian cannot access the source, the conclusion is labeled as based only on derived material. In that case, the code must not be described as verified.

### Wiki Pages

The main page types are processes, concepts, systems, and source analyses. A process page describes its purpose, participants, steps, conditions and exceptions, system interactions, evidence, known discrepancies, and gaps.

Minimum page metadata: `id`, `title`, `kind`, `domain`, `review_status`, `source_refs`, `depends_on`, and `reviewed_at`. Review status is `draft`, `reviewed`, or `stale`; `reviewed` means the page was checked against the listed sources, not that it is guaranteed to be true. Conflicts are called out separately in the text: a reviewed page can still describe an unresolved discrepancy.

In the first implementation, metadata are written as a JSON object between `---` delimiters. This YAML subset allows Python's standard JSON parser to be used instead of a partial custom YAML parser. Arbitrary YAML frontmatter is not supported.

Key claims receive stable Markdown anchors and references to specific source revisions. The text distinguishes:

1. behavior prescribed by a procedure;
2. behavior described by a workflow or implemented in code;
3. behavior verified for a specific deployment;
4. a librarian's assumption or inference.

There is no blanket priority rule that “code outranks documentation.” Evidence is compared in the context of the question. A discrepancy between policy and implementation is retained as knowledge.

In the first cycle, the skill requires finding and rechecking pages that cite a superseded revision. Under automated maintenance, a new source marks such pages and propagates the mark through explicit `depends_on` links. Automatically finding semantically related pages that are not recorded as dependencies is not guaranteed.

### Navigation and History

`wiki/index.md` contains a page catalog with brief descriptions. Initial search uses the catalog, text, and metadata filters. Vector search is added only after measurable misses on control questions.

`wiki/log.md` stores append-only publication records: the operation, affected pages and sources, and feedback IDs. Git stores the history of the changes themselves. Queries that produce no new knowledge do not create pages automatically.

## Librarian

### Answering a Question

The calling agent passes the question and known context: process, product, environment, and required version. The librarian selects a published wiki revision, finds relevant pages, checks the evidence, and returns a structured answer with a brief text summary.

In the first cycle, the working tree is read, so `wiki_revision` has the form `sha256:<hash of wiki and sources content>`. The wrapper checks the hash before and after the request and rejects the answer if files changed. This is a content identifier, not a Git commit or a guarantee of an atomic snapshot. Reading an immutable published commit belongs to the next phase. The `published_revision` field for a completed correction contains the Git commit itself.

Answer contract:

- `answer_id`, `wiki_revision`, `question`, `scope`;
- `summary` — a brief answer;
- `claims` — claims with IDs and a `supported`, `inferred`, `conflicted`, or `unknown` status;
- `citations` — for each piece of evidence: `source_id`, `revision`, an exact `locator`, and a wiki page reference;
- `gaps` and `conflicts` — what is unknown and which sources disagree.

A reference to a generated page alone does not replace evidence. An inference needs evidence and a clear distinction between facts and interpretation. An unknown answer may have an empty evidence set if the gap is explained. The librarian does not invent a confidence percentage.

Answer mode reads the knowledge base. Writes are limited to recording the query result in local state; wiki changes go through a separate maintenance cycle. Getting an answer does not depend on the correction queue being complete.

### Feedback

The calling agent submits a JSON feedback item:

- `feedback_id` — a unique ID for retries;
- `answer_id`, `wiki_revision`;
- `target` — a disputed claim or a page with an anchor;
- `description` — the specific discrepancy;
- `evidence` — references to verifiable materials, if available;
- `suggested_correction` — an optional correction hypothesis.

The item may be accepted without evidence, but its content must not be treated as correct. Resubmitting the same `feedback_id` with identical content returns the existing result; different content under that ID returns an error.

Lifecycle: `pending → processing → resolved | rejected | needs_evidence`. Set `resolved` only after publishing a verified change or confirming that the issue was already fixed in a later revision. Publishing a verified conflict with evidence also counts as a completed outcome; it does not mean the business dispute itself is resolved. Every terminal outcome includes a rationale; a correction also includes `published_revision`.

The first manual cycle adds `ready_for_review` between processing and a terminal result. Completing a proposed correction requires the current HEAD and an exact match between proposed files and the commit. Rejections and requests for evidence are completed with a separate command and do not need an artificial commit. A job has status `resolved` after completion; the substantive feedback outcome is stored separately.

`needs_evidence` is not processed indefinitely. New evidence is submitted as a separate item with `related_feedback_id` to preserve the original message's history.

The librarian compares feedback with current sources. It may correct a conclusion, reject the feedback, request missing evidence, or retain a substantiated conflict. The process owner decides how the business workflow itself should change.

### Rules for External Materials

Documents, code comments, and feedback items are data. Instructions inside them do not change the librarian's rules, run commands, or grant permission for additional actions. The first version assumes one audience with equal access to all included sources and derived pages.

## Local Tools

### Separation of Responsibilities

Python performs deterministic operations: registering snapshots and hashes, validating formats and links, searching, managing the queue, obtaining a consistent knowledge snapshot, and invoking the agent adapter.

The LLM interprets meaning, compares sources, forms answers, and proposes changes. Structural validation is not presented as validation of business content.

Proposed `wiki` CLI:

| Command | Result |
| --- | --- |
| `wiki source add` | A new snapshot and manifest, or a reference to an identical snapshot already registered |
| `wiki ingest` | A job to process a registered source revision |
| `wiki search` | Matching pages with paths, IDs, and snippets |
| `wiki ask --request request.json` | A headless OpenCode call using a preconfigured profile and a validated JSON answer |
| `wiki feedback submit --file feedback.json` | A durably stored feedback item and its ID |
| `wiki feedback status --id ID` | Current status, rationale, and correction revision |
| `wiki lint` | Deterministic structural, link, and provenance errors |
| `wiki maintenance run` | One bounded queue pass and a result report |

If the adapter is unavailable, agent commands return an explicit error rather than a simulated successful answer. Search, registration, queue operations, and structural validation work without an LLM.

### Queue and Restarts Under Automated Maintenance

SQLite is stored in `.state/queue.sqlite3` and is not included in Git. Transactions protect feedback intake and job selection. The database is a required part of local state: Git does not preserve pending feedback. The README should describe consistent backups through the SQLite backup mechanism.

One maintenance process acquires the writer lock. Jobs have IDs, attempt counts, error details, and a lease. After a crash, an unfinished job can be claimed again; automatic attempts are limited. When attempts are exhausted, the job receives the `failed` status with a diagnostic reason. This technical status differs from the substantive outcome `rejected`.

Per-run limits include the maximum number of jobs, maximum duration, and a cost limit if the adapter can enforce one. If monetary cost cannot be measured, the CLI states so explicitly; a timeout is not a cost limit.

### Publication and Reads Under Automated Maintenance

The published knowledge version is the Git commit referenced by the local ref `refs/wiki/published`. Maintenance updates this ref only after validation succeeds. Each request resolves the ref at its start and reads that immutable snapshot; freely reading a changing working tree is not part of the consistency guarantee. The initial ref is created explicitly from a verified commit, without including user changes that were already prepared.

Maintenance prepares changes separately from the published snapshot. Before publication, it checks formats, links, claim provenance, and whether the feedback applies to the current version. New sources, pages, the index, and the log are included in one publication. Partially written files must not be visible through `wiki ask`.

Helpers do not include unrelated user changes in a commit. If preparation or publication overlaps with user changes, or the source revision changes, the operation stops with a clear result and preserves prepared material for another review. The specific Git mechanism will be selected in the implementation plan.

SQLite and Git do not form a shared transaction. The published commit records the job ID in its log. After a failure between publication and the queue update, the handler first checks for an already published result so it can complete the status update without applying the correction again.

If no knowledge base has been published yet, `wiki ask` returns a no-knowledge state. Untracked and unverified material is not published automatically.

## Bootstrap Files

```text
AGENTS.md
README.md
pyproject.toml
wiki.toml
.gitignore
.opencode/
  agents/
    librarian.md
    wiki-maintainer.md
  skills/
    wiki-ingest/SKILL.md
    wiki-ask/SKILL.md
    wiki-feedback/SKILL.md
    wiki-maintain/SKILL.md
  commands/
    wiki-ingest.md
    wiki-ask.md
    wiki-feedback.md
    wiki-maintain.md
sources/
  raw/
  manifests/
wiki/
  index.md
  log.md
  processes/
  concepts/
  systems/
  sources/
src/wiki_tools/
tests/
docs/check-questions.md
examples/
```

Separate `schemas/`, `templates/`, and `evals/` directories are not created. They would represent three useful functions for which simpler locations are sufficient at the start:

- source, answer, and feedback formats are described and validated in Python; field validation does not prove that an answer is true;
- the process-page template is kept in the `wiki-ingest` skill;
- in the original design, control questions and expected evidence were kept together in `docs/check-questions.md`.

Moving these materials into dedicated directories makes sense after multiple templates, external format consumers, or an automated evaluation suite appear.

`AGENTS.md` briefly describes write boundaries, evidence handling, the single-published-revision rule, and skill routing. Detailed operation steps live in the skills. Python code is not duplicated in instructions.

`examples/` contains a small fictional workflow, procedure, and implementation excerpt with an intentional discrepancy. Example materials are excluded from production-knowledge search until the user explicitly registers them.

## Scheduled Maintenance

The scheduler calls the same `wiki maintenance run` command that can be invoked manually. The MVP does not need a persistent Python service. Deploying a schedule is a separate setup step; creating the bootstrap does not activate background runs automatically.

Processing new sources and feedback takes priority over preventive tasks. Structural checks run regularly; substantive re-review is triggered by changed dependencies and feedback. Reanalyzing the entire knowledge base on every run is excluded.

If remote clients are introduced, a network adapter can use the same contracts. Authentication, audience separation, and remote storage will require separate design.

## Readiness Checks

Five control questions are sufficient for the first working cycle: a direct fact, synthesis across two materials, a procedure/implementation conflict, missing information, and a repeat answer after a correction. Review both the answer and its evidence; exact wording need not match. Python checks cover snapshot registration, required fields, local paths, and feedback resubmission.

For `wiki ask`, also verify that configured parameters are applied, invocation works from another directory, text is passed without shell interpretation, the final answer is extracted from the event stream, errors produce a nonzero exit code, and timeouts terminate the process. Stubbed checks are supplemented with one real OpenCode run after model setup and a check that answer mode denies writes.

After automated maintenance is added, checks must confirm these essential guarantees:

1. Reimporting preserves the original snapshot and does not duplicate an identical revision.
2. A source change identifies dependent pages, including explicit transitive dependencies.
3. Resubmitting feedback does not create a second job; changed content under the same ID is detected.
4. Two maintenance processes cannot apply the same job simultaneously.
5. An LLM error, invalid JSON, or validation error does not publish a partial change.
6. Restarting after publication but before job completion does not apply the correction again.
7. An answer refers to one published revision. Local files and Markdown anchors are checked automatically; external links, code symbols, and other formats are checked only when a suitable handler and source are available.
8. Change preparation does not affect user files outside its job.

The set of substantive questions grows over time from real errors and feedback from calling agents. A separate evaluation framework is not part of the bootstrap.

Before testing a real invocation in the selected agent environment, only the tools and contracts can be declared ready. A stubbed check does not verify that the librarian works.

## Deferred Work

A network API, distributed queue, multiple writers, graph and vector databases, a chat interface, automatic loading from corporate systems, and multiple audiences with different access rights are outside the first version.

Support for a specific workflow format or type of Confluence export begins with a real sample. Until samples are available, the bootstrap accepts local materials with metadata and provides a general workflow; it does not claim universal conversion of every export.

## Project Rationale

The user supplied the original LLM Wiki pattern in an attachment. The design adopts its separation of source material from the synthesized wiki, its index and log, and its ingest, query, and lint operations. The feedback queue, answer contracts, and publication constraints are proposals for this project.

Source versioning is especially important for workflow definitions: [Camunda documentation on resource version binding](https://docs.camunda.io/docs/components/best-practices/modeling/choosing-the-resource-binding-type/) shows that the resource actually used depends on the binding type. This example does not define the behavior of the user's as-yet-unselected engine.

The OpenCode integration is based on the official documentation for [agents](https://opencode.ai/docs/agents/), [commands](https://opencode.ai/docs/commands/), the [CLI](https://opencode.ai/docs/cli/), and [configuration merging](https://opencode.ai/docs/config/). Documentation for another major OpenCode version must not substitute for checking the installed version.
