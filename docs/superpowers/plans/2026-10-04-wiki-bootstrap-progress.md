# Progress for 2026-10-04-wiki-bootstrap.md

- User authorized implementation with Luna Max and parent review.
- Spec read; implementation plan created. Future automatic publication remains out of scope.
- Repository has no commits; worktree cannot start from a commit. Created branch `codex/wiki-bootstrap` in existing checkout. Staged IDE files preserved.
- Python runtime: bundled Python 3.12.14 via Node subprocess tool; shell Python execution is unavailable in this tool environment.

## Preflight interfaces

| Tasks | Shared interface or file | Check |
| --- | --- | --- |
| 1 and 2 | WikiError and source/queue function signatures | Defined once in task 1; task 2 owns data modules |
| 1 and 2 | Maintenance execute callback and proposal result | Shared contract explicit; CLI owns OpenCode call |
| 1 and 3 | Agent names, config, command entry points | librarian and wiki-maintainer fixed; docs follow CLI |
| 2 and 3 | Examples, queue states and manual completion | ready_for_review precedes Git-verified resolved |
| 1 | Config and process failure tests | Same headless operation, no network in unit tests |
| 2 | Durable state and immutable sources | Real SQLite/filesystem tests, no auto-publication |
| 3 | Profiles/skills/docs | Read-only proposal profiles match first iteration |

## Decisions

- Ruling: interpret the user's explicit implementation request and chosen execution method as authorization to proceed with this plan without a redundant approval round; cost if wrong is reversible changes in the new branch.
- Ruling: use a new branch in place because the repository is unborn and contains staged user files; cost is no separate worktree, while implementation paths do not overlap existing IDE files.
- Ruling: add ready_for_review as an intermediate state for the agreed manual-review first cycle; cost is one additional state in local JSON/SQLite contracts.

## Tasks

- Task 1: complete; Luna Max implementation reviewed by parent, integrated tests passed.
- Task 2: complete; Luna Max implementation reviewed by parent, integrated tests passed.
- Task 3: complete; Luna Max profiles/docs and independent workflow test reviewed by parent, integrated tests passed.

## Review preparation

- Parent inspected OpenCode 1.18.18 official run.ts: events are step_start, step_finish, text, tool_use and error. Missing/subagent-only profiles silently fall back to default, so profile validation is essential.
- Local OpenCode user config exists but declares no explicit model. Question about runtime model remains pending; no live model calls made.
- Parent validated the actual primary librarian profile with OpenCode debug agent and isolated diagnostic XDG directories. No model request was made.
- Interim profile review found caller/headless routing recursion, missing command argument placeholders, and rejected/needs_evidence ambiguity. Luna profiles corrected them; parent reread the fixes.
- Ruling: wiki page metadata uses a JSON object between Markdown frontmatter delimiters (valid YAML subset), avoiding a partial general YAML parser under the stdlib-only constraint; cost is that existing arbitrary YAML pages require conversion.
- Data test review requested writable tempfile locations, canonical metadata fixtures, and evidence-backed proposals.
- Parent independently ran test_config.py: 7 tests pass after nested provider/model ID fix (multiple slashes are valid).
- Parent prepared ignored `.venv` from bundled Python 3.12.14 with system site packages; no network install performed.
- Parent reproduced two knowledge.py bugs in a temporary fixture: valid ../concepts link produced unsafe_markdown_link; depends_on=[{}] crashed lint with TypeError. Luna data is fixing them with regression tests.
- Additional data review requests: validate duplicate manifests consistently; serialize concurrent registration; reject malformed metadata cleanly; support .bpmn/.dmn as text; preserve reference validation.
- Additional runner review requests: correct process-group timeout fixture, use same Python executable for fake child processes, preserve nested model IDs, deny routing skills in headless profile. Runner acknowledged/fixed test harness and config.
- Profiles reviewed for caller/headless separation, argument placeholders, exact JSON schema and unresolved-vs-refuted distinction; latest requested changes are direct template reading and consistent index/log proposals.
- Parent independently ran current source/lint/feedback tests: 5 + 4 + 3 pass (19 including prior config suite).
- Parent runner suite confirmed pending cancellation and stderr regressions; 9/11 passed at that point. Luna runner is fixing both and ensuring the cancellation test waits for child signal-handler readiness.
- Luna profiles is also assigned an independent cross-component test in `tests/test_workflow.py`, using real CLI/SQLite/temp Git and fake NDJSON model process. Parent remains sole reviewer.
- Maintenance review requests: completed corrections must be present at current HEAD; outcomes with no page changes must not require a dummy commit; validate proposed page metadata and source refs; share source/locator validation with ask and support Russian section anchors.
- Ruling: require current HEAD for first-cycle proposed-correction completion, while rejected/needs_evidence complete explicitly without a revision; cost is that users must supply the current commit, rather than an arbitrary older or side-branch commit.
- Parent independently verified all Task 2 suites: sources 6, knowledge 6, feedback 3, maintenance 9; 24 passing tests. Data code review findings addressed.
- Parent validated both final primary OpenCode profiles through the installed CLI; read-only policy and denied skill/delegation/external tools confirmed in resolved permission rules (managed tool-output scratch remains OpenCode's native exception).
- Ruling: first-cycle answers identify the observed wiki/source contents with `wiki_revision=sha256:<digest>`, not a potentially misleading HEAD label; completed corrections still record a Git `published_revision`. Cost is that first-cycle content hashes are not checkout-able Git refs. Immutable snapshot queries remain the later stage.
- Source-confirmed runner finding: disabling default OpenCode plugins also disables built-in Codex/Copilot auth adapters. Luna runner is retaining built-in auth while `--pure` disables external plugins.
- Runner owner reports 24 config/query/process tests green after fixing citation validation, interruption cleanup, inherited configuration flags and built-in auth preservation. CLI dispatch is the remaining implementation step before integrated verification.

## Final verification

- Parent ran the full suite: **58 tests passed in 9.392 seconds**, CPython 3.12.14.
- The independent workflow test uses the real CLI, SQLite and a temporary Git repo, with only the model process replaced by deterministic NDJSON output.
- Installed `.venv/bin/wiki lint`: exit 0, no errors or warnings.
- Installed search for ParcelFlow: empty results, confirming examples are not implicitly ingested.
- Installed ask from another cwd with the intentionally empty model: exit 1 and a structured configuration_error.
- Real OpenCode 1.18.18 debug/runtime overlay: primary profile, exact local prompt, configured step limit, project-config override removed, built-in auth preserved. A deliberately nonexistent provider exercises the real headless error path and yields a controlled process_error.
- Successful live LLM response remains unverified because no runtime model was selected. No corporate materials were imported and no model request was sent to a working provider.
- Final docs-only fix removes Git self-reference from publication-log instructions. Commit SHA is recorded afterward in queue completion, not embedded in its own commit.
- Work remains uncommitted on `codex/wiki-bootstrap`. Pre-existing staged IDE files were preserved. No daemon or recurring schedule was installed.
