# Manual Testing Guide Report

Created a single `docs/manual-testing.md` with a file-drop scenario for `sources/raw/` that requires no manual `source add`/`ingest`, plus checks for snapshots, manifests, the queue, repeated revisions, and paths containing Cyrillic characters. The guide also contains the five control questions and the manual review/feedback cycle.

The README was shortened to cover the normal workflow and link to the guide. `AGENTS.md` and the OpenCode instructions were updated to describe automatic discovery, `unclassified` sources, the `supersedes` chain, and read-only headless profiles. `docs/check-questions.md` remains as a short link to the new detailed scenarios.

The parent agent reported that the full automated suite passed after implementation: 66 tests. No live OpenCode calls were run as part of this documentation change, and the guide does not claim that any manual model checks have passed.

## Parent's Final Verification

After Finder service-file handling was fixed, the full suite passed through uv: 69 tests in 11.387 seconds. Wiki lint reported no errors or warnings. The parent independently verified an ordinary raw-folder drop, deduplication, a new revision after editing the file, preservation of the previous snapshot, and no false `knowledge_changed` result caused by `.DS_Store`. Live model scenarios were not run.
