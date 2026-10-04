# Task 1 — runner and CLI report

Implemented the standard-library config loader, strict headless OpenCode subprocess runner, validated ask flow, local answer records, JSON CLI, Python launcher, and package entry point. The runner uses the explicit wiki root and configured model, embeds the validated local profile prompt into runtime config, preserves provider authentication, disables sharing, denies external directories and all non-read tools, and passes prompt text as one literal argument. It rejects process errors, event errors, malformed or incomplete streams, invalid answers, bad citations, and knowledge changes during a query. Timeout, SIGINT, and CLI SIGTERM cleanup stop the complete OpenCode process group.

Task 1 files: .gitignore, pyproject.toml, wiki.toml, scripts/wiki, src/wiki_tools/{__init__,__main__,errors,config,runner,cli}.py, and tests/{test_config,test_runner,test_cli}.py. wiki.toml leaves model empty until the owner selects a provider/model. No live model request was made.

Verification: all 31 Task 1 tests pass under bundled CPython 3.12.14 (7 config, 17 runner/query, 7 CLI). The parent independently ran the integrated suite: 58 tests passed, including the external-cwd source to fake answer to feedback to proposal to Git completion workflow. The installed OpenCode 1.18.18 runtime overlay was also checked without a model call.

No files were staged or committed; staged IDE files were left untouched.
