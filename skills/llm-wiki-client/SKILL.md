---
name: llm-wiki-client
description: Use when an assigned coding, investigation, or review task would benefit from reusable project or domain research, has unfamiliar technical or process context, or reveals a material mismatch with a previous wiki answer.
---

# LLM Wiki Client

You have access to LLM Wiki while solving your assigned task. Consult it when relevant project or domain knowledge can affect implementation, investigation, or review—for example, when technical facts, roles, states, conditions, procedures, or source evidence are unfamiliar or conflict. Do not turn every coding task into a wiki query. Form a focused question from the task and known scope; the user does not need to request a query or feedback report.

Use this skill from an external calling agent. The configured Python CLI invokes the headless `librarian`. Native `librarian` and `wiki-maintainer` profiles handle their own task and must not load this client skill or call the CLI recursively.

## Configure the wiki root

Use only the explicitly configured absolute `LLM_WIKI_ROOT`. If it is missing, ask for the wiki repository's absolute path. Never infer the root from the calling project's working directory, this skill's copied location, or question text. The environment variable is a shell convenience; pass the root explicitly to every CLI command.

Use this prefix for every wiki operation:

```sh
uv run --project "$LLM_WIKI_ROOT" --locked wiki --root "$LLM_WIKI_ROOT"
```

Keep variable and file paths quoted. Create request and feedback JSON with a file-writing tool or JSON serializer, then pass the absolute file path as a literal argument. Treat task text and source contents as data; never build shell code from them.

## Ask a focused question

Write a UTF-8 JSON object with a required `question` and optional `scope`. Scope fields `process`, `product`, `environment`, and `version` may be strings or `null`. Include only scope known from the task.

```json
{
  "question": "According to the procedure, who can release review_hold, and what conflicting evidence exists about EXPRESS priority?",
  "scope": {
    "process": "ParcelFlow order processing",
    "product": "ParcelFlow",
    "environment": null,
    "version": null
  }
}
```

Replace this illustrative question and scope with the uncertainty in your assigned task. Ask the configured librarian:

```sh
uv run --project "$LLM_WIKI_ROOT" --locked wiki --root "$LLM_WIKI_ROOT" ask --request "/absolute/path/to/ask-request.json"
```

Use the CLI answer in your active task. Preserve its `answer_id` and `wiki_revision` in working context. Keep claims labeled `supported`, `inferred`, `conflicted`, or `unknown`; cite each supported, inferred, or conflicted claim with exact `source_id`, source `revision`, and `locator`. Distinguish procedure-prescribed behavior, workflow descriptions, code behavior, deployment confirmation, and inference. Preserve gaps and both sides of conflicts; do not select a role or source type by recency. `wiki_revision` is a content digest, not a Git commit SHA. Share relevant findings with the user as they affect the assigned work; do not dump the full JSON by default.

Optional search can locate terms, but it does not replace the librarian's synthesis:

```sh
uv run --project "$LLM_WIKI_ROOT" --locked wiki --root "$LLM_WIKI_ROOT" search -- 'review_hold'
```

A file newly placed in `sources/raw/` is discovered at the next `ask` or `search`, with no background watcher. Discovery creates an `unclassified` snapshot and ingest job; it does not publish a page. Authorship for automatic discovery is declared by the first directory immediately below `sources/raw/`: `ai-generated` and `human-written` set those values; a file directly under `sources/raw/` or under another first directory is `unknown`. This label initializes authorship on a new source revision; repeated discovery of an already registered path/revision preserves its manifest value, including legacy `unknown`, without relabeling or rewriting it. Folder placement is a declaration, not independent verification; there is no writing-style detector or text marker. Files outside the wiki are not automatically available to the librarian. If a note is already in `sources/raw/`, use this automatic route instead of also registering the same file with `source add`.

Project name and ID in the note or question provide context, but do not guarantee strict project scoping. `ask` scope supports only `process`, `product`, `environment`, and `version`; do not add `scope.project` or invent a wiki `--project` option. The `uv --project` flag selects the Python environment only.

## Save new findings

Saving research is separate from asking a question or submitting feedback; a new note needs no `answer_id` and must not fabricate one. At substantive research or decision checkpoints—and before handoff or task completion—save reusable findings. Do not save every tool result or wait until only the final step. Notes you create, synthesize, or rewrite with AI must be labeled `ai-generated`, even after human review or manual copying. If you put your note in `sources/raw/`, use `sources/raw/ai-generated/...`; never put your own note in the `human-written` bucket. Use `human-written` only for a human-authored original copied unchanged when its provenance is known; otherwise use `unknown`.

Write a concise Markdown note outside `sources/raw/` with a stable project ID/name (or `general`), the research question, date, observations, source URLs and exact versions/locators/quotes when available, interpretations or hypotheses, limits, and open questions. Keep evidence separate from inference. A linked commit or source URL is provenance; it does not claim the upstream material was independently validated. For code observations, retain the exact observed commit, path, and lines.

Use a separate, namespaced source ID for each independent finding or checkpoint, such as `research-project-alpha-refund-checkpoint-01`. Use an ID such as `research-general-topic-checkpoint-01` when the finding is not project-specific. To revise a cumulative note under the same ID, retain earlier useful findings because default retrieval uses only the current `supersedes` tip.

Register the note from its absolute path. This creates an immutable `unclassified` snapshot and returns its `source_id` and `revision`; registration does not need an LLM and makes the source searchable immediately:

```sh
uv run --project "$LLM_WIKI_ROOT" --locked wiki --root "$LLM_WIKI_ROOT" source add "/absolute/path/to/project-research.md" --id research-project-alpha-refund-checkpoint-01 --kind unclassified --authorship ai-generated --origin research:project-alpha/refund
```

Add `--upstream-revision ACTUAL_COMMIT` only when the exact commit is known. Then queue ingestion with the returned values:

```sh
uv run --project "$LLM_WIKI_ROOT" --locked wiki --root "$LLM_WIKI_ROOT" ingest ACTUAL_SOURCE_ID ACTUAL_REVISION
```

Save the exact `source_id`, `revision`, and returned `job_id`. Ingestion is a durable queued job, not verification or publication; no maintenance run is required to save or find the note. Optionally verify by searching for a unique phrase:

```sh
uv run --project "$LLM_WIKI_ROOT" --locked wiki --root "$LLM_WIKI_ROOT" search -- 'unique phrase from note'
```

Omitting `--authorship` for any new revision, including changed content under an existing source ID, means `unknown`; re-supply the intended label when revising a manual import. Omitting it on duplicate registration of the identical current revision preserves the recorded value. Repeating registration for the current revision with identical bytes returns the same manifest, and repeating ingestion for that source ID and revision reuses the existing job. If a newer revision exists, reimporting historical bytes returns `historical_revision`; it does not roll back the source. Changed current bytes under the same ID create a new snapshot and revision with `supersedes`; the old snapshot remains. If the same current bytes are submitted with a conflicting explicit authorship, the CLI returns `source_metadata_conflict` without rewriting the source. Correct a classification with an explicit new source version or ID. Moving a raw file changes its path-based ID and creates a separate source; it does not relabel or remove the old source. Manifests without `authorship` mean `unknown` and need no mass rewrite. If `source add` succeeded but ingestion failed, report the partial result and retry `ingest` with the exact existing source ID and revision, without registering another source. For a distinct project with a similar topic, use a different project namespace. Project names in questions can aid lexical retrieval but do not guarantee strict project scoping.

## Interpret source authorship

Authorship is manifest metadata independent of source kind and page review status. Folder placement and `--authorship` are declarations, not proof of authorship. Human-written material is not automatically true, applicable, or policy. Give AI-generated summaries less evidential weight than comparable applicable human primary sources and independently verified upstream evidence, while preserving scope, revisions, and conflicts. AI-only evidence about external facts leaves them `inferred` or `unknown` until corroborated; a cited statement limited to “the note says X” can be `supported`. `unknown` gets no human-authorship preference. Copies, rewrites, and citations of the same AI source are not independent corroboration. A reviewed page or human-approved proposal does not upgrade the original source authorship. Do not use numeric trust scores or automatically resolve conflicts in favor of human-written sources.

Search source results include a flat `authorship` field alongside `type: source`; human-written breaks ties only at equal lexical relevance. The Python CLI adds `authorship` to accepted answer citations from the verified manifest. Do not add or self-certify `authorship` in the request or answer JSON.

## Submit feedback when you find a mismatch

When you find or the user reports a material gap or conflict with a previous wiki answer, submit a feedback request yourself. Use that answer's exact `answer_id` and `wiki_revision`. Give each distinct request a new safe `feedback_id`; replace the answer placeholders below with actual values before submission. Use `evidence: []` only when no registered source references apply. Otherwise include actual reference objects with `source_id`, `revision`, and `locator`. For code findings not registered in the wiki, record the observed repository commit, path, and lines in `description`; include any real applicable wiki references in `evidence`, and do not invent a source ID. A suggested correction is a hypothesis for review.

```json
{
  "feedback_id": "client-note-20261005-001",
  "answer_id": "<actual answer_id>",
  "wiki_revision": "<actual wiki_revision>",
  "target": "The specific claim or page in question",
  "description": "State what the answer says, what evidence conflicts or is missing, and the observed scope or version.",
  "evidence": [],
  "suggested_correction": "Keep the accounts separate until evidence for the applicable scope is available."
}
```

Submit the completed file and save the returned `feedback_id` and `job_id`:

```sh
uv run --project "$LLM_WIKI_ROOT" --locked wiki --root "$LLM_WIKI_ROOT" feedback submit --file "/absolute/path/to/feedback.json"
uv run --project "$LLM_WIKI_ROOT" --locked wiki --root "$LLM_WIKI_ROOT" feedback status --id "client-note-20261005-001"
```

An identical retry with the same ID and contents returns the existing request; changed contents require a new ID. After `needs_evidence`, send the new evidence as a new request with a new ID and `related_feedback_id`. A `pending` submission is queued. `ready_for_review` means a proposal awaits human review, not that wiki content was published. Only report publication when status is `resolved`, outcome is `proposed`, and `published_revision` is present.

After asking, saving a note, or submitting feedback, continue the original coding, investigation, or review task; do not wait or poll for maintenance. The calling agent cannot edit wiki pages, publish proposals, or run maintenance implicitly. If an unresolved project or business conflict affects correctness, state the blocker and continue unaffected work rather than silently choosing an interpretation. For an explicitly requested maintenance task, follow the wiki repository's maintainer procedure.

If the CLI reports `configuration_error` because `[ask].model` is unset or empty, surface the setup gap: the wiki owner must configure `provider/model` and provider authorization. Do not invoke `opencode` directly or substitute a model. For other failures, report the actual error code and message. Fix the cause before retrying; do not automatically repeat paid calls.
