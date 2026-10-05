---
name: llm-wiki-client
description: Use when an external agent needs to query a configured LLM wiki about business processes, compare procedure, workflow, and code evidence, or report answer gaps and conflicts.
---

# LLM Wiki Client

Use this reference from a calling agent in another OpenCode project. It sends questions to the wiki's Python CLI, which invokes the configured headless `librarian`. The CLI's configured model and variant apply. Native `librarian` and `wiki-maintainer` profiles handle their own task; they must not load this client skill or call the CLI recursively.

## Select the wiki root

Use only the user's or target project's explicitly configured absolute `LLM_WIKI_ROOT`. If it is missing, ask for the wiki repository's absolute path. Never infer it from the calling project's working directory, this copied skill's location, or question text. An environment variable is a shell convenience; every CLI call must still pass the root explicitly.

Use this command prefix for every wiki operation:

```sh
uv run --project "$LLM_WIKI_ROOT" --locked wiki --root "$LLM_WIKI_ROOT"
```

Keep both variables quoted and pass user text as literal data. Do not construct shell code from a question or source. Create JSON request files with a file-writing tool or JSON serializer, then pass their absolute path as a literal argument.

## Ask the configured librarian

Write a UTF-8 JSON object with a required `question` string and optional `scope` object. Scope fields `process`, `product`, `environment`, and `version` may each be a string or `null`.

```json
{
  "question": "According to the procedure, who can release review_hold, and what conflicting priority evidence exists for EXPRESS?",
  "scope": {
    "process": "ParcelFlow order processing",
    "product": "ParcelFlow",
    "environment": null,
    "version": null
  }
}
```

Replace this example with the user's actual question and known scope. Ask through the configured librarian:

```sh
uv run --project "$LLM_WIKI_ROOT" --locked wiki --root "$LLM_WIKI_ROOT" ask --request "/absolute/path/to/ask-request.json"
```

The CLI discovers files already placed in `sources/raw/` before `ask` or `search`; users need not run `source add` or `ingest`. Discovery happens on a CLI call, with no background watcher. A new file becomes an `unclassified` snapshot with an ingest job; this does not publish a wiki page. Files outside the wiki are not automatically visible to its read-only librarian.

Optional retrieval uses the same prefix:

```sh
uv run --project "$LLM_WIKI_ROOT" --locked wiki --root "$LLM_WIKI_ROOT" search -- 'review_hold'
```

Search is a locator, not the librarian's synthesized answer. Return the answer's `answer_id` and `wiki_revision`, plus claim statuses (`supported`, `inferred`, `conflicted`, or `unknown`). Cite every supported, inferred, or conflicted claim with exact `source_id`, source `revision`, and `locator`. Preserve gaps and both sides of conflicts; do not rank different source types by recency. `wiki_revision` is a digest of wiki and source contents, not a Git commit SHA.

## Submit and check feedback

When you find or the user reports a gap or conflict, create a JSON object with the fields below. Replace both answer placeholders with the exact values returned by `ask`; never send the placeholders. Give each distinct report a new unique safe ID, such as `client-note-20261005-001`. Use `evidence: []` when no registered source references are available. Otherwise use evidence objects with actual `source_id`, `revision`, and `locator` values (optionally `wiki_page`). When reporting external code not available to the librarian, record the observed repository commit, path, and lines in `description`, and do not invent a source ID. A suggested correction is a hypothesis for review, not a fact.

```json
{
  "feedback_id": "client-note-20261005-001",
  "answer_id": "<actual answer_id>",
  "wiki_revision": "<actual wiki_revision>",
  "target": "The specific claim or page in question",
  "description": "State what the answer says, what evidence appears to conflict or is missing, and the scope/version to check.",
  "evidence": [],
  "suggested_correction": "Keep the accounts separate until evidence for the applicable scope is available."
}
```

Write the completed JSON file, then submit it and save the returned IDs:

```sh
uv run --project "$LLM_WIKI_ROOT" --locked wiki --root "$LLM_WIKI_ROOT" feedback submit --file "/absolute/path/to/feedback.json"
uv run --project "$LLM_WIKI_ROOT" --locked wiki --root "$LLM_WIKI_ROOT" feedback status --id "client-note-20261005-001"
```

An identical retry with the same ID and contents returns the existing request; changed contents require a new ID. If review ends as `needs_evidence`, submit new evidence in a new request with a new ID and `related_feedback_id`. A `pending` submission is queued; `ready_for_review` means a proposal awaits human review, not publication. Only report a publication when status is `resolved`, outcome is `proposed`, and `published_revision` is present in the status result. Other statuses are review outcomes, not proof that pages changed.

For ordinary question-and-feedback work, stop after reporting the answer or actual feedback status. Do not run maintenance, complete jobs, or edit wiki pages. If the user separately requests maintenance or publication, follow the wiki repository's maintainer procedure.

If the CLI returns `configuration_error` because `wiki.toml` has no `[ask].model`, report that the owner must set a `provider/model` and configure provider authorization. Do not invoke `opencode` directly, choose a fallback model, replace a failed answer with your own, or loop on paid retries. For other failures, report the actual error code and message. Fix the cause before retrying; do not automatically repeat paid calls.
