# Raw-folder import implementation report

## Behavior

Files placed in sources/raw/ or its ordinary subdirectories are discovered during a valid wiki search, a configured wiki ask, or wiki maintenance run. Discovery registers immutable snapshots and queues one ingest job per source revision. It does not call a model by itself or publish wiki changes.

The source ID is drop- followed by the SHA-256 of the root-relative POSIX path encoded as UTF-8. This keeps identity stable when the file contents change, supports spaces and Cyrillic names, and distinguishes equal basenames in different folders. The manifest origin is the same repository-relative path. The original file is only read; its bytes and path remain in place. Newly discovered revisions use the unclassified kind until a person determines their meaning.

Each scan reuses the existing source and queue APIs. Identical scans reuse the current manifest and SQLite ingest job. An overwritten input keeps its previous snapshot and receives a new revision and job. Returning to an older revision raises historical_revision with the input path in the error instead of changing history.

Discovery skips symbolic links, hidden and service files, common copy-in-progress suffixes (.tmp, .part, .partial, .download, .crdownload), editor swap files, and files matching the managed snapshot shape sources/raw/<safe-id>/<sha256>[.<extension>]. That shape is excluded by name even when its bytes are damaged or its manifest is absent. Ask revision hashing applies the same raw-name exclusions, while continuing to hash managed snapshots and ordinary wiki/source content. Copies should finish before search, ask, or maintenance runs. Scan, read, registration, and queue failures are returned as path-qualified WikiError values.

All ordinary file suffixes can be snapshotted and queued. Search and citation validation continue to use the existing text suffix allowlist. Lint warns with unsupported_source_format for registered files outside that list; it does not claim to parse them. Before a drop is discovered, read-only lint emits an unregistered_raw_source warning containing the input path. Lint does not register sources or create a queue.

## Code and coverage

src/wiki_tools/raw.py implements shared discovery and read-only raw-file inspection. Search, ask, and maintenance invoke the discovery helper at their required boundaries. unclassified is accepted by source validation, CLI source registration, and lint.

The regression coverage checks automatic search discovery without source add, pre-process registration during ask with a fake OpenCode executable, maintenance queueing against real SQLite, repeated scan idempotency, revisions after overwrite, Cyrillic and spaced nested paths, same-basename separation, original-file preservation, historical revision errors, managed-snapshot exclusion, symlink and temporary-file exclusion, read-only lint warnings, and ask revision stability when Finder/temp files appear during the fake process. Ask still rejects concurrent changes to wiki pages and registered snapshots.

Verification: all 69 unittest cases passed with the project virtual environment. No live model request was used.
