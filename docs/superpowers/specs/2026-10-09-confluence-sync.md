# Confluence links during maintenance

The user keeps one or more text or Markdown files containing Confluence page links in `sources/raw/human-written/confluence/`. Only this directory and its descendants declare remote pages to synchronize. Existing OpenCode configuration supplies Atlassian MCP access and credentials. Synchronization happens during maintenance, including when the local link files have not changed.

## Source lifecycle

Link files are control inputs, excluded from ordinary raw discovery and knowledge retrieval. Plain full page URLs and Markdown links are accepted; duplicate links share one source, identified by the Confluence site/context and page ID. Unsupported short links receive an actionable error. Files elsewhere remain ordinary raw inputs; links in fetched pages are not followed.

A separate primary OpenCode profile, `wiki-source-sync`, reads one requested page through read-only MCP tools and returns its identity, title, version when available, and complete content. Python validates the response and saves the content as a `confluence_export`. The folder declares `human-written` provenance, not independent verification. These sources have general scope. Python does not implement an Atlassian network client or store credentials.

Compare title and content independently of check times and remote version metadata. Unchanged content reuses its current immutable snapshot and ingest job. Changed content creates a revision linked by `supersedes`, including a restoration of earlier content. Comparison can recover from tracked snapshots after cloning without local state. Removed links stop future polling; saved evidence and wiki pages remain.

Malformed inputs and fetch failures are visible in the result. Other requested pages can succeed. Failed fetches preserve the last successful revision; they do not imply deletion or currentness. Older pending or ready ingest jobs for an updated Confluence source become failed with an explicit supersession explanation, retaining their saved proposals. Completed history remains unchanged. Explicit retry and completion must not publish an obsolete Confluence revision.

## Maintenance and review

`wiki maintenance sync` synchronizes sources without preparing wiki proposals. An ordinary `wiki maintenance run` synchronizes first, then processes its bounded proposal queue. `--skip-sync` permits the calling agent to avoid a duplicate fetch after an explicit sync. Targeted `run --id`, review, completion, retry, ask, search, and low-level ingest do not fetch remote pages.

The `/wiki-maintain` calling-agent flow synchronizes once, reviews existing ready proposals, and prepares at most one pending proposal if none are ready. Human accept/revise/defer review remains required for publication. Reading an explicit saved review stays read-only.

Maintenance receives the verified source/page inventory missing from its former prompt, plus directly affected pages and reverse `depends_on` dependencies within its existing permitted scope. This directs the agent to revisit current knowledge instead of blindly adding duplicate pages. General-source jobs retain the existing general-page publication restriction; propagation into project pages is not added by this change.

## Verification

Use deterministic fake model responses around real filesystem, manifests, SQLite, and CLI behavior. Cover directory exclusion, URL identity/deduplication, unchanged and reverted content, state loss, failures, superseded jobs, tool permissions, and inventory scope. Add an explicit manual scenario for real OpenCode/Atlassian access and remote changes with unchanged local link files. Do not claim live MCP verification without performing it. No scheduler or daemon is introduced.
