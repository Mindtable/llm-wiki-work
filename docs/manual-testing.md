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

   Expect an answer quoting the source, with a `source_id` of the form `drop-<sha256 UTF-8 relative POSIX path>`, a SHA-256 `revision`, a `line:<n>` locator, and `citation.authorship: unknown`. If the librarian infers an apparent kind from the text, it must identify that as an inference and preserve the manifest kind `unclassified`. If there is no evidence for a claim that a policy is approved or in force, leave it `unknown` and explain the gap.

3. Immediately after the answer, before any separate `wiki search`, check the manifest and queue entry. This confirms that discovery happened during the ordinary ask and that a search did not compensate for a missed step.

   The manifest at `sources/manifests/<source_id>--<revision>.json` should point in `origin` to the original path `sources/raw/manual-drop-2026-10-04.md`, point in `local_path` to the hash-named snapshot under `sources/raw/<source_id>/`, and have a `sha256` matching `revision`. Compare the snapshot bytes with the original file. The original remains in place and editable; the snapshot is immutable. The manifest has `kind: unclassified` and `authorship: unknown` because the file is directly under `sources/raw/`. Discovery creates one ingest job but does not publish a wiki page.

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

   The result should include `type: source`, `kind: unclassified`, an `authorship: unknown` field, the ID, revision, and locator.

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

## Agent-led Proposal Review

Use a separate Git clone with a temporary `.state/` queue. This scenario checks the calling agent's review flow; the model-generated proposal step requires configured OpenCode, but reading a saved proposal does not call a model.

1. Arrange one `ready_for_review` proposal and one unrelated `pending` job. Invoke `/wiki-maintain` with no action. It should read the default JSON report internally for machine fields and present only the oldest ready proposal in a readable form. Check that the report preserves `status`, `job_id`, `ready_job_ids`, job type/outcome/project, summary, changes, evidence, `proposal_path`, and `review_fingerprint`, with the project, changed files, readable diff, and doubts clearly shown. The fingerprint and raw JSON stay internal; do not ask the user to open or edit a JSON file. It must not start another model call while a ready proposal exists. If no proposal is ready, it may prepare at most one pending job and then review it.
2. Call `/wiki-maintain review --id JOB_ID` again. It must only read the existing proposal, make no model call or queue change, and the calling agent should find the same `review_fingerprint` internally if nothing changed. The direct CLI forms `wiki maintenance review --id JOB_ID` and `wiki maintenance review --id JOB_ID --format markdown` should also be read-only; the default JSON report carries machine fields, while Markdown is an optional human view. Verify the report came from the SQLite `ready_for_review` row, not a guessed `.state/proposals/` file. In a separate empty-queue copy, run `wiki maintenance review`; expect `no_ready_proposals` and no model invocation or queue changes.
3. Leave an unrelated pending job, then explicitly invoke `/wiki-ingest` for a new source. Check the source snapshot, returned revision, and job ID. If the job is pending, the agent must prepare only it with `wiki maintenance run --id JOB_ID`, then review that ID. If the job already has a ready proposal, it must reuse it without another model call. The unrelated pending job must remain untouched; the agent must not duplicate-import the source. Low-level `wiki ingest` and routine research capture/raw discovery remain queue-only.
4. Choose `defer` for one ready proposal. Confirm no wiki page, proposal, queue status, Git staging, commit, or completion status changes, and that no persistent deferred/rejected status is invented. During this review session the agent skips the deferred proposal and can use `ready_job_ids` to select the next one. Then choose `revise` for another proposal: provide feedback in ordinary text, confirm the calling agent updates only the saved proposal, re-runs review, presents the new diff, and waits for a fresh decision. Wiki pages, index/log, Git staging, commits, and completion status remain unchanged until a new acceptance. The previous decision does not approve the revision.
5. In the temporary clone, change a proposal after it was shown but before acceptance, then refresh `wiki maintenance review --id JOB_ID`. The calling agent must detect the changed internal `review_fingerprint`, show the updated diff and explanation to the user, and wait for a new decision before applying anything. Do not show the fingerprint itself.
6. For an accepted `outcome: proposed`, verify the agent refreshes the same-ID review immediately before applying. Seed staged and unstaged changes in an affected path and in unrelated paths in the temporary clone. Before applying, the agent must inspect affected-path diffs, disclose edits that would be overwritten or included in a commit, and get agreement; it must not overwrite or include extra changes silently. It should then apply the exact agreed contents, reconcile `wiki/index.md`/`wiki/log.md` if needed, re-present any material reconciliation change, and run lint. Verify it rechecks staged and unstaged affected-path diffs before staging, then inspects the staged diff again immediately before committing. It must commit only explicitly approved paths and necessary snapshots/manifests, and preserve unrelated staging and work. If current `HEAD` already contains the exact approved contents, it should not create an empty commit and should complete with that full current `HEAD` SHA; otherwise, after the commit, it completes with the new full commit SHA. There is no automatic push.
7. For accepted `rejected` or `needs_evidence` rationales, verify the agent calls `wiki maintenance complete --id JOB_ID` without a Git revision. A user deferring or revising a correction proposal is not the same as accepting semantic outcome `rejected`.
8. Run explicit `/wiki-maintain run --limit 2` with multiple pending jobs. It must process only the bounded set and review ready results one at a time, using `ready_job_ids` to continue after each human decision. It must not drain additional jobs, dump the saved JSON, or automatically retry failed jobs. Mark model steps not performed as not run.

## Feedback Proposal Review

This is a separate live scenario; keep the model enabled. Start with the actual answer to control question 3 and copy its `answer_id` and `wiki_revision` into a copy of `examples/feedback.json`. For another run, use a new unique `feedback_id` and save the completed JSON outside `sources/`.

Submit the feedback through `/wiki-feedback` or the CLI:

```sh
uv run --locked wiki --root . feedback submit --file /path/to/feedback.json
```

Save the actual `feedback_id` and `job_id` from the response. A slash command may return only the feedback ID; in that case, get the `job_id` with `feedback status --id FEEDBACK_ID`. Process a bounded set with `/wiki-maintain run --limit 1`, checking status until this feedback reaches `ready_for_review`; do not assume the limit selects feedback specifically. The agent then presents this proposal with the review flow above. Check the evidence, gaps, conflicting revisions, complete pages, metadata, links, and project scope in the readable diff.

For an accepted `outcome: proposed`, the calling agent applies only the agreed content, commits it with necessary snapshots/manifests, then completes the job with the full SHA. If current `HEAD` already contains the approved bytes, no empty commit is needed and the current full SHA is used. The CLI verifies the content in the specified commit. For accepted `rejected` or `needs_evidence`, it completes by job ID without a Git revision. New evidence goes in a separate feedback item. `ready_for_review` and a resolved job are not proof of publication; `wiki_revision` is a content digest, not a Git SHA.

## What Counts as a Passed Check

Structural results, snapshot and hash, manifest, path-based ID, current revision in the chain, `wiki search` result, and number of SQLite records can be checked deterministically. A citation and answer contents, the headless profile's proposal, and its decision can only be checked live with a configured OpenCode model. Record the date, OpenCode version, question, source ID/revision, statuses, and exact locators. Mark live steps that were not performed as not run, not as passed.

## External Research Checkpoints Without a Prior Answer

This scenario checks that an external calling agent can save reusable research without first asking the librarian. Use a temporary repository copy and a separate agent project with the client skill installed. Set `LLM_WIKI_ROOT` to the temporary wiki's absolute path. The source registration, ingestion, and search checks below do not require a configured model.

1. Create a Markdown research note outside `sources/raw/`, for example `/absolute/path/to/notes/project-alpha-refund.md`. Include a stable project ID/name, a research question, date, observations with source URLs and exact versions/locators or short quotes when available, and separate sections for interpretation, uncertainty, and open questions. Record a code commit/path/lines as observed provenance; do not imply that linking a commit proves an independent source check.
2. Register the note with a project-namespaced ID and origin:

   ```sh
   uv run --project "$LLM_WIKI_ROOT" --locked wiki --root "$LLM_WIKI_ROOT" source add "/absolute/path/to/notes/project-alpha-refund.md" --id research-project-alpha-refund-checkpoint-01 --kind unclassified --authorship ai-generated --project project-alpha --origin research:project-alpha/refund
   ```

   Capture the returned `source_id` and `revision`. If the exact upstream commit is known, it may be supplied with `--upstream-revision ACTUAL_COMMIT`; do not guess it.

3. Queue that exact revision and save the returned `job_id`:

   ```sh
   uv run --project "$LLM_WIKI_ROOT" --locked wiki --root "$LLM_WIKI_ROOT" ingest ACTUAL_SOURCE_ID ACTUAL_REVISION
   ```

   Inspect the manifest and snapshot: the kind is `unclassified`, authorship is `ai-generated`, `scope.project` is `project-alpha`, the origin identifies the project/topic, and the snapshot bytes and SHA-256 match the saved note. Check `.state/queue.sqlite3` read-only for exactly one ingest row matching the returned source ID and revision. Search for a unique phrase from the note and confirm the result carries the source ID, revision, locator, and a flat `authorship` field.

4. Restart the external agent and repeat registration with the identical note bytes, ID, origin, `--authorship ai-generated`, and `--project project-alpha`, then repeat ingestion. The manifest/revision and job ID should be unchanged, with one matching queue row. Repeat registration without `--authorship` and `--project`; it should preserve both recorded values. Edit the note under the same ID, preserving earlier useful observations, and register it again with `--authorship ai-generated --project project-alpha`. Queue the newly returned revision:

   ```sh
   uv run --project "$LLM_WIKI_ROOT" --locked wiki --root "$LLM_WIKI_ROOT" ingest ACTUAL_SOURCE_ID NEW_REVISION
   ```

   Confirm the new revision's `supersedes` points to the previous revision, the prior snapshot remains on disk, exactly one job exists for the new source ID/revision pair, and search retrieves the new current revision. For a second project researching the same topic, use a distinct project namespace and source ID.

5. In an optional live model check, ask with `--project project-alpha` or provide the same ID as `scope.project` in the request. Expect only project-alpha and general sources/pages, with project-alpha first, and `scope.project` pinned to `project-alpha` in the answer. The `uv --project` flag still selects only the Python environment. Mark this check not run until it has actually been exercised. If the note was instead placed in `sources/raw/ai-generated/projects/project-alpha/`, allow the next `ask` or `search` to discover it and do not also run `source add` on the same file.

## Source Authorship Labels

Use a disposable repository copy. These checks verify declared provenance, not a detector of who wrote the text.

1. Create two test fixtures with identical UTF-8 contents and a unique search phrase, one under `sources/raw/ai-generated/test-bucket/` and one under `sources/raw/human-written/test-bucket/`. Their folder labels are test inputs, not claims about real-world authorship. Create two more copies directly under `sources/raw/` and under `sources/raw/project-alpha/`. Trigger ordinary `wiki search` for the phrase. Check the manifests and search results: the first pair must have `authorship: ai-generated` and `authorship: human-written`, while the other paths must be `unknown`. All four sources may remain `kind: unclassified`; kind and authorship are independent. Folder placement is a declaration, not verification.
2. Confirm each source search result exposes a flat `authorship` field alongside `type: source`. Because the first pair has identical text, their lexical relevance is tied and the human-written result should appear first. For a query where relevance differs, a more relevant result must still rank above a less relevant human-written result. `unknown` must not receive the human-written tie preference.
3. Use the research-note registration from the previous section, which passes `--authorship ai-generated`. Create one additional explicit source without the option and confirm that a new source defaults to `authorship: unknown`. Register a separate manual source as `human-written`, change its bytes under the same source ID, and register that changed revision without `--authorship`; the new revision should be `unknown` and its `supersedes` should reference the earlier one. Re-register the agent-authored source with the same current bytes and source ID but explicit `--authorship human-written`. Expect `source_metadata_conflict`; verify the existing manifest and snapshot remain unchanged.

   In a separate temporary copy, place a legacy imported note at `sources/raw/ai-generated/legacy-note.md` with a manifest created before authorship existed. Trigger discovery repeatedly: the existing revision must remain `unknown`, without `source_metadata_conflict` or a manifest rewrite, despite the folder label. Change the note bytes at that path and discover again; the new revision should receive `ai-generated` from the folder. Lint and search should continue to interpret the legacy revision as `unknown`.
4. Move a discovered file from the `ai-generated` raw bucket to the `human-written` bucket and run discovery again. The new path should produce a different path-based source ID with `human-written`; the old manifest and snapshot remain `ai-generated`. The move must not relabel or remove the old source. Correcting a label requires an explicit new source version or source ID.
5. Create a test knowledge page citing the AI-generated source and manually review it. Confirm the source manifest and search/citation metadata still say `ai-generated`; a reviewed page or human approval does not upgrade the source. Do not add an authorship field to page metadata or model-produced citation JSON; Python supplies it from the manifest.
6. Optional live trust checks require configured OpenCode and a model. Ask about an external fact present only in an AI-generated note: the fact should remain `inferred` or `unknown`, while the limited statement “the note reports X” may be `supported` with a citation. Add an applicable human primary source that conflicts with an AI summary: the answer should give the primary evidence greater weight but preserve the conflict rather than automatically selecting a winner based only on authorship. Copies or rewrites of the same AI material must not count as independent corroboration. Mark these live checks not run until actually exercised.

## Project Scope Routing

Use a separate temporary repository copy. Project IDs are lowercase ASCII slugs. Project scope controls retrieval grouping, not access to the shared repository.

1. Register three UTF-8 notes with the same search topic: one with `--project atlas`, one with `--project beacon`, and a general source without `--project`. Mark agent-written notes with `--authorship ai-generated`; pass `--kind unclassified` for these notes. Include distinct facts in each note, and make the general note a stronger textual match for the chosen query than the Atlas note. Check each manifest's `scope.project` (`atlas`, `beacon`, or `null`) and verify the general note is shared, not copied into each project.

   ```sh
   uv run --locked wiki --root . source add /tmp/atlas-note.md \
     --id atlas-refund-01 --kind unclassified --authorship ai-generated --project atlas
   uv run --locked wiki --root . source add /tmp/beacon-note.md \
     --id beacon-refund-01 --kind unclassified --authorship ai-generated --project beacon
   uv run --locked wiki --root . source add /tmp/general-note.md \
     --id general-refund-01 --kind unclassified --authorship ai-generated
   ```

   Queue the returned revisions with separate `ingest SOURCE_ID REVISION` calls. Re-register Atlas's identical current bytes with explicit `--project beacon`; expect `source_metadata_conflict` and verify the existing snapshot and manifest remain unchanged. Edit Atlas note content under the same source ID and omit `--project`; the new revision should inherit `atlas`. For a separate test source, edit content under an existing Atlas source ID and register with explicit `--project beacon`; the new revision should be Beacon-scoped. Use a separate source ID for general material; there is no flag to silently clear scope.

2. Search using the same topic:

   ```sh
   uv run --locked wiki --root . search --project atlas -- refund
   uv run --locked wiki --root . search -- refund
   ```

   The scoped result must include Atlas and general sources, exclude Beacon, and place the Atlas group before the stronger-matching general result. The unscoped result must include all three groups and place general sources first. Scope group priority comes before textual relevance; the existing score and authorship tie-breaks apply within each group.

3. Create a legacy fixture manifest that lacks `scope.project`. Search and scoped ask should treat it as general (`null`) without rewriting the manifest. For raw discovery, test these paths: `sources/raw/ai-generated/projects/atlas/...` (Atlas + AI), `sources/raw/human-written/projects/atlas/...` (Atlas + human), `sources/raw/projects/atlas/...` (Atlas + unknown authorship), and another path such as `sources/raw/ideas/...` (general). A repeated discovery of an already registered revision preserves its stored project, including legacy `null`; changing its contents may initialize the new revision from the recognized project folder. Moving the file changes its path-based ID and creates a separate source.

4. In an optional live check, ask `wiki ask --project atlas "What does the refund brainstorm say?"`. Confirm the answer has `scope.project: atlas`, citations identify only Atlas or general sources, and each citation's `project` matches its verified source manifest. Ask about a Beacon-only fact under Atlas; the answer must not cite Beacon and should report a gap if the permitted inventory does not support the fact. For an unscoped ask, the answer may use any project but must qualify project-specific findings. Mark live checks not run until exercised with a configured model.

5. Check flag/request mismatch with an ask request whose `scope.project` is `beacon`:

   ```sh
   uv run --locked wiki --root . ask --project atlas --request /path/to/beacon-request.json
   ```

   Expect an error and no answer because the flag and request project differ. The natural `ask --project atlas "question"` form is also supported; use `--` when question text begins with an option-like token.

6. Create an existing Atlas knowledge page with `scope: {"project": "atlas"}` and `source_refs` to Atlas and general sources, plus a general page on the same topic with only general source refs. Run an Atlas maintenance job. Confirm the proposal preserves the Atlas page's scope and leaves the general page unchanged; it must not target a page scoped to another project. A general page may cite only general source refs. `wiki/index.md` and `wiki/log.md` may link across projects but do not count as evidence for a scoped answer.
