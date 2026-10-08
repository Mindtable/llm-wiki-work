# Manual Test Scenarios

This file is the single source of detailed manual test scenarios. Check observable behavior and supporting evidence, not the model's exact wording. A live OpenCode call is not verified until it has actually been run.

## Preparation

Use a separate temporary copy of the repository with a clean `.state/` queue. Do not run these checks against the working wiki or delete files from its `sources/raw/`: user input files and created snapshots serve different purposes. A copy with Git history is required for the scenario involving a Git commit.

You need macOS, Linux, or WSL2, `uv`, and OpenCode 1.18.18. `uv sync --locked` selects and, if needed, installs the Python version specified by the project. Open the copy in OpenCode, configure provider authorization with `opencode auth login`, and set the chosen `provider/model` in the `[ask]` section of `wiki.toml`. Do not add authorization secrets to repository files. Set up the Python environment and run the initial structural check from the copy's root:

```sh
uv sync --locked
uv run --locked wiki --root . lint
```

On a clean copy, lint should return an empty error list. The live scenarios below require a configured OpenCode model; they check citations, revisions, and statuses, not exact wording or model quality beyond these contracts.

## A File Placed in `sources/raw/`

This is the primary test for automatic discovery. The user copies a file through a file manager or editor. They do not run `wiki source add` or `wiki ingest` themselves: an ordinary request to the calling agent follows the existing Python CLI route, which discovers new input files before calling the profile. `/wiki-ask` can be used as an explicit shortcut. Testing starts only after the file copy has finished; there is no background directory watcher.

1. In the root of the temporary copy, use a file manager or editor to create a unique file with ordinary UTF-8 text, for example `sources/raw/manual-drop-2026-10-04.md`, containing this synthetic sentence:

   ```markdown
   # Ordinary Add Test

   In the training record for `manual-drop-key-742`, the storage location is listed as the "purple box".
   ```

   Use a new unique key for each run. Do not place the file inside a managed source's ID directory. Until the file is discovered, `wiki lint` should report an `unregistered_raw_source` warning; lint is read-only and does not import the file.

2. Send the calling agent an ordinary message, such as: “Ask the librarian: what storage location is listed for `manual-drop-key-742`? Include an exact quote and explain what follows from this record.” Alternatively, call `/wiki-ask` with the same question. Automatically discovered material receives manifest kind `unclassified` because no kind has been declared. This does not prevent its contents from being described, but its presence in the directory does not establish independent review, approval, or applicability as policy.

   Expect an answer quoting the source, with a `source_id` of the form `drop-<sha256 UTF-8 relative POSIX path>`, a SHA-256 `revision`, and a `line:<n>` locator. If the librarian infers an apparent kind from the text, it must identify that as an inference and preserve the manifest kind `unclassified`. If there is no evidence for a claim that a policy is approved or in force, leave it `unknown` and explain the gap.

3. Immediately after the answer, before any separate `wiki search`, check the manifest and queue entry. This confirms that discovery happened during the ordinary ask and that a search did not compensate for a missed step.

   The manifest at `sources/manifests/<source_id>--<revision>.json` should point in `origin` to the original path `sources/raw/manual-drop-2026-10-04.md`, point in `local_path` to the hash-named snapshot under `sources/raw/<source_id>/`, and have a `sha256` matching `revision`. Compare the snapshot bytes with the original file. The original remains in place and editable; the snapshot is immutable. The manifest has `kind: unclassified`. Discovery creates one ingest job but does not publish a wiki page.

   To check the queue, replace the two Python strings with the values from the manifest and run this read-only query:

   ```sh
   uv run --locked python - <<'PY'
   import sqlite3

   source_id = "PASTE_SOURCE_ID"
   revision = "PASTE_REVISION"
   with sqlite3.connect("file:.state/queue.sqlite3?mode=ro", uri=True) as db:
       rows = db.execute(
           "SELECT job_id, status FROM jobs "
           "WHERE job_type = 'ingest' AND source_id = ? AND revision = ?",
           (source_id, revision),
       ).fetchall()
   print(rows)
   PY
   ```

   Expect one ingest row.

4. Only after the check above, run a separate search for the unique key:

   ```sh
   uv run --locked wiki --root . search manual-drop-key-742
   ```

   The result should include `type: source`, `kind: unclassified`, the ID, revision, and locator.

5. Repeat the ordinary question and search. The ID and revision should remain the same; repeat discovery should not create a second job.

## Editing and Rediscovery

Open and edit the original file created by hand in the first scenario. Do not edit the hash-named snapshot; it is immutable evidence of the earlier version.

1. Replace "purple box" with a different unique value, then send the same question to the calling agent as an ordinary message.
2. The answer and `wiki search` should show the same `source_id` and a new `revision`. In the new manifest, `supersedes` points to the previous revision. The previous snapshot and manifest remain in place; one ingest job is created for the new ID/revision pair.
3. By default, use the current tip of that `source_id`'s `supersedes` chain unless the question asks for a historical version. If a wiki page or its summary cites an older revision of the same source, identify it as stale evidence. Do not infer that a newer procedure takes precedence over a workflow or code summary with a different ID: compare source types and scope using the evidence.
4. Confirm that the answer's `wiki_revision` changed after the new revision appeared. This is a SHA-256 digest of wiki and source contents, not a Git commit SHA. If the history for this source ID already contains a newer revision, trying to return to past content must fail explicitly with `historical_revision` and include the original file path; the old copy on disk does not become current.

## Paths and Discovery Exclusions

1. Use a file manager to create an ordinary nested folder and a file whose names include spaces and Cyrillic characters, for example `sources/raw/Полевые заметки/Смена 2.md`. The Cyrillic names are intentional test data for Unicode path handling, not untranslated documentation. Add a unique synthetic sentence and ask the calling agent about it. Expect one valid manifest and a citation with a locator; the origin path, ID, and revision should preserve all characters.
2. Repeat the search and question after the hash-named snapshot has been created automatically. A managed snapshot must not be rediscovered recursively as a new user file: there should be no second `source_id`, manifest, or ingest job for the snapshot's own path.
3. Check exclusions: the scanner skips hidden and service names, `.tmp` and `.part` temporary suffixes, and symbolic links. Wait for copying to finish: if a file changes while being scanned, the CLI returns `raw_file_changed` with the path and asks you to retry after the copy completes. The canonical path of a hash snapshot is always excluded from discovery, even if the snapshot is damaged or its manifest is missing. A regular file in an unsupported format still receives a snapshot, manifest, and ingest job, but is unavailable for text search and citation; lint reports `unsupported_source_format`. PDFs, archives, and similar materials are not automatically converted to text. Run lint again after discovering supported inputs: the `unregistered_raw_source` warning should disappear for processed inputs. Lint itself remains read-only.

## Check Questions on Demonstration Materials

Files in `examples/` remain excluded from search until explicitly registered. Use a separate clean copy for this scenario. Register the three input files with their stated kinds; for each command, take the actual revision from the JSON response and pass it to a separate `ingest` command:

```sh
uv run --locked wiki --root . source add examples/order-workflow.md \
  --id demo-order-workflow --kind workflow --origin synthetic:demo-order-workflow
uv run --locked wiki --root . source add examples/order-procedure.md \
  --id demo-order-procedure --kind procedure --origin synthetic:demo-order-procedure
uv run --locked wiki --root . source add examples/order-code-summary.md \
  --id demo-order-code-summary --kind code_summary \
  --origin synthetic:demo-order-code-summary \
  --upstream-revision 8f12a7c0d3e4f5a6b7c8091a2b3c4d5e6f708192
```

After registration, queue each exact revision:

```sh
uv run --locked wiki --root . ingest demo-order-workflow REVISION
uv run --locked wiki --root . ingest demo-order-procedure REVISION
uv run --locked wiki --root . ingest demo-order-code-summary REVISION
```

Then ask the calling agent the questions below. Before asking about publication, first complete and review the proposal cycle described in the next section.

1. **Direct fact.** According to the procedure, who can release `review_hold`? Expected evidence: `demo-order-procedure`, `section:review-hold-release` (or exact lines from that section); the answer is the shift manager after confirming the data.
2. **Synthesis across materials.** What happens if the workflow receives `screening_code=EXPRESS` and the check finds missing data? Expected evidence: `demo-order-workflow` and `demo-order-procedure`. The answer connects the urgent priority with checking and the hold until confirmation; each claim has its own citation.
3. **Procedure/code discrepancy.** What priority is specified for `EXPRESS`? Expected evidence: `demo-order-procedure`, `section:priority-rule`, and `demo-order-code-summary`, `section:priority-behavior`; the workflow also says `urgent`. The answer preserves the conflict and states that the summary does not confirm that the source code was checked.
4. **Gap.** What is the average order-check time on holidays? Expect `unknown`, with no invented number and an explanation of the gap.
5. **Repeat after publication.** After manually reviewing and publishing a wiki change, repeat question 3. The answer uses the current `wiki_revision` and specific evidence. If the evidence still cannot resolve the conflict, keep the conflict visible. For a source with the same ID, use the current tip of its `supersedes` chain; do not pick a winner among different source types using a “newest wins” rule.

Check statuses and evidence, not exact wording. No synthetic example confirms real ParcelFlow behavior. Treat code as checked only when the relevant source commit and source files are available; they are not included in this example.

## Manual Proposal Review and Feedback

This is a separate live scenario; keep the model enabled. Start with the actual answer to control question 3 and copy its `answer_id` and `wiki_revision` into a copy of `examples/feedback.json`. For another run, use a new unique `feedback_id` and save the completed JSON outside `sources/`.

Submit the feedback through `/wiki-feedback` or the CLI:

```sh
uv run --locked wiki --root . feedback submit --file /path/to/feedback.json
```

Save the actual `feedback_id` and `job_id` from the response. A slash command may return only the feedback ID; in that case, get the `job_id` by querying `feedback status` with that `feedback_id`.

The previous section queued three ingest jobs. Before the feedback scenario, either process and manually close all three or monitor the queue: after each `/wiki-maintain run --limit 1`, check the feedback with `uv run --locked wiki --root . feedback status --id FEEDBACK_ID` and repeat the bounded run until the feedback reaches `ready_for_review`. Do not assume that one `--limit 1` command will select feedback specifically. If the status is `failed`, stop, inspect the error, fix its cause, and only then explicitly run `uv run --locked wiki --root . maintenance retry --id JOB_ID`; do not automatically repeat maintenance until it succeeds. `ready_for_review` means a JSON proposal has been saved under `.state/proposals/` and is waiting for a person. Check its evidence, gaps, conflicting revisions, complete pages, metadata, and links.

- For `outcome: proposed`, first record the agreed full content in the proposal, then put that same content and the required entry in `wiki/log.md` into the repository. Include the checked source snapshots and manifests in the Git commit with the agreed wiki pages. After the commit, pass its full SHA to `wiki maintenance complete --id JOB_ID --revision COMMIT_SHA`. The CLI checks that this content exists in the specified current commit.
- For `outcome: rejected` or `needs_evidence`, check the rationale and run `wiki maintenance complete --id JOB_ID` without a Git revision. Do not publish an unsupported correction. Submit new evidence in a separate feedback item.

A job's `resolved` status and a feedback outcome are different statuses. Neither `ready_for_review` nor successful submission means publication. `wiki_revision` is a hash of wiki and source contents; the Git SHA used to complete a proposal is a separate identifier.

## What Counts as a Passed Check

Structural results, snapshot and hash, manifest, path-based ID, current revision in the chain, `wiki search` result, and number of SQLite records can be checked deterministically. A citation and answer contents, the headless profile's proposal, and its decision can only be checked live with a configured OpenCode model. Record the date, OpenCode version, question, source ID/revision, statuses, and exact locators. Mark live steps that were not performed as not run, not as passed.

## External Research Checkpoints Without a Prior Answer

This scenario checks that an external calling agent can save reusable research without first asking the librarian. Use a temporary repository copy and a separate agent project with the client skill installed. Set `LLM_WIKI_ROOT` to the temporary wiki's absolute path. The source registration, ingestion, and search checks below do not require a configured model.

1. Create a Markdown research note outside `sources/raw/`, for example `/absolute/path/to/notes/project-alpha-refund.md`. Include a stable project ID/name, a research question, date, observations with source URLs and exact versions/locators or short quotes when available, and separate sections for interpretation, uncertainty, and open questions. Record a code commit/path/lines as observed provenance; do not imply that linking a commit proves an independent source check.
2. Register the note with a project-namespaced ID and origin:

   ```sh
   uv run --project "$LLM_WIKI_ROOT" --locked wiki --root "$LLM_WIKI_ROOT" source add "/absolute/path/to/notes/project-alpha-refund.md" --id research-project-alpha-refund-checkpoint-01 --kind unclassified --origin research:project-alpha/refund
   ```

   Capture the returned `source_id` and `revision`. If the exact upstream commit is known, it may be supplied with `--upstream-revision ACTUAL_COMMIT`; do not guess it.

3. Queue that exact revision and save the returned `job_id`:

   ```sh
   uv run --project "$LLM_WIKI_ROOT" --locked wiki --root "$LLM_WIKI_ROOT" ingest ACTUAL_SOURCE_ID ACTUAL_REVISION
   ```

   Inspect the manifest and snapshot: the kind is `unclassified`, the origin identifies the project/topic, and the snapshot bytes and SHA-256 match the saved note. Check `.state/queue.sqlite3` read-only for exactly one ingest row matching the returned source ID and revision. Search for a unique phrase from the note and confirm the result carries the source ID, revision, and locator.

4. Restart the external agent and repeat registration with the identical note bytes, ID, and origin, then repeat ingestion. The manifest/revision and job ID should be unchanged, with one matching queue row. Edit the note under the same ID, preserving earlier useful observations, and register it again. Queue the newly returned revision:

   ```sh
   uv run --project "$LLM_WIKI_ROOT" --locked wiki --root "$LLM_WIKI_ROOT" ingest ACTUAL_SOURCE_ID NEW_REVISION
   ```

   Confirm the new revision's `supersedes` points to the previous revision, the prior snapshot remains on disk, exactly one job exists for the new source ID/revision pair, and search retrieves the new current revision. For a second project researching the same topic, use a distinct project namespace and source ID.

5. In an optional live model check, include the project ID/name in the question text. The supported `ask` scope has only `process`, `product`, `environment`, and `version`; there is no `scope.project` or wiki project filter. The `uv --project` flag selects the Python environment only. This retrieval check requires configured OpenCode, a model, and provider authorization; mark it not run until it has actually been exercised. If the note was instead placed in `sources/raw/`, allow the next `ask` or `search` to discover it and do not also run `source add` on the same file.
