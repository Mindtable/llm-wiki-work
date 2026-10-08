"""JSON-producing command line interface for the local wiki."""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from typing import Any, TextIO

from .config import load_config
from .errors import WikiError
from .runner import ask, run_opencode
from .sources import SOURCE_KINDS


class _HelpRequested(Exception):
    def __init__(self, text: str) -> None:
        self.text = text


class _JsonArgumentParser(argparse.ArgumentParser):
    def print_help(self, file: TextIO | None = None) -> None:
        # Keep help inside the one-JSON-value stdout contract.
        self._help_text = self.format_help()

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status == 0:
            raise _HelpRequested(getattr(self, "_help_text", self.format_help()))
        raise WikiError("argument_error", (message or "Argument error.").strip())

    def error(self, message: str) -> None:
        raise WikiError("argument_error", message)


def _build_parser() -> _JsonArgumentParser:
    parser = _JsonArgumentParser(prog="wiki", description="Local wiki with sources and verifiable answers.")
    parser.add_argument("--root", type=Path, help="Absolute or relative path to the wiki root.")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_JsonArgumentParser)

    source = commands.add_parser("source", help="Manage source snapshots.")
    source_commands = source.add_subparsers(dest="source_command", required=True)
    source_add = source_commands.add_parser("add", help="Save an immutable source snapshot.")
    source_add.add_argument("path", help="Path to a local source file.")
    source_add.add_argument("--id", required=True, dest="source_id")
    source_add.add_argument(
        "--kind",
        required=True,
        choices=sorted(SOURCE_KINDS),
    )
    source_add.add_argument("--origin", default="")
    source_add.add_argument("--upstream-revision", default="")

    ingest = commands.add_parser("ingest", help="Queue a source for ingestion.")
    ingest.add_argument("source_id")
    ingest.add_argument("revision")

    search = commands.add_parser("search", help="Search current wiki pages.")
    search.add_argument("query", nargs="+", help="Words or a phrase to search for.")

    ask_parser = commands.add_parser("ask", help="Ask the configured librarian.")
    ask_parser.add_argument("--request", dest="request_file", type=Path, help="JSON file with question and optional scope.")

    feedback = commands.add_parser("feedback", help="Submit and check feedback.")
    feedback_commands = feedback.add_subparsers(dest="feedback_command", required=True)
    submit = feedback_commands.add_parser("submit", help="Save feedback.")
    submit.add_argument("--file", required=True, dest="feedback_file", type=Path)
    status = feedback_commands.add_parser("status", help="Show feedback status.")
    status.add_argument("--id", required=True, dest="feedback_id")

    lint = commands.add_parser("lint", help="Check wiki structure and links.")

    maintenance = commands.add_parser("maintenance", help="Process the queue manually.")
    maintenance_commands = maintenance.add_subparsers(dest="maintenance_command", required=True)
    run = maintenance_commands.add_parser("run", help="Prepare a bounded number of proposals.")
    run.add_argument("--limit", type=int, default=1)
    complete = maintenance_commands.add_parser("complete", help="Record the manual review result.")
    complete.add_argument("--id", required=True, dest="job_id")
    complete.add_argument("--revision", help="Full Git commit verified for a proposed change.")
    retry = maintenance_commands.add_parser("retry", help="Explicitly return a job to the queue.")
    retry.add_argument("--id", required=True, dest="job_id")
    return parser


def _first_command_index(argv: list[str]) -> int | None:
    index = 0
    while index < len(argv):
        value = argv[index]
        if value == "--root":
            index += 2
            continue
        if value.startswith("--root="):
            index += 1
            continue
        if value in {"-h", "--help"} or value.startswith("-"):
            return None
        return index
    return None


def _parse_args(parser: _JsonArgumentParser, argv: list[str]) -> argparse.Namespace:
    command_index = _first_command_index(argv)
    if command_index is not None and argv[command_index] == "ask":
        tail = argv[command_index + 1 :]
        option_request = bool(tail) and (tail[0] == "--request" or tail[0].startswith("--request="))
        option_help = bool(tail) and tail[0] in {"-h", "--help"}
        if tail and not option_request and not option_help:
            question = tail[1:] if tail[0] == "--" else tail
            args = parser.parse_args(argv[: command_index + 1])
            args.question = question
            return args
        args = parser.parse_args(argv)
        args.question = []
        return args
    return parser.parse_args(argv)


def _discover_root() -> Path:
    module = Path(__file__).resolve()
    for candidate in module.parents:
        if (candidate / "wiki.toml").is_file():
            return candidate.resolve()
    raise WikiError("configuration_error", "Failed to find the wiki root; pass --root PATH.")


def _select_root(value: Path | None) -> Path:
    if value is None:
        return _discover_root()
    try:
        resolved = value.expanduser().resolve(strict=True)
    except OSError as exc:
        raise WikiError("configuration_error", f"Wiki root was not found: {value}.") from exc
    if not resolved.is_dir():
        raise WikiError("configuration_error", "Wiki root must be a directory.")
    return resolved


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}.")
        result[key] = value
    return result


def _read_json_object(path: Path, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise WikiError("argument_error", f"Failed to read {description} JSON: {exc}.") from exc
    if not isinstance(payload, dict):
        raise WikiError("argument_error", f"JSON {description} must be an object.")
    return payload


def _dispatch(args: argparse.Namespace, root: Path) -> Any:
    if args.command == "source" and args.source_command == "add":
        from .sources import add_source

        return add_source(
            root,
            Path(args.path),
            source_id=args.source_id,
            kind=args.kind,
            origin=args.origin,
            upstream_revision=args.upstream_revision,
        )
    if args.command == "ingest":
        from .feedback import enqueue_ingest

        return enqueue_ingest(root, args.source_id, args.revision)
    if args.command == "search":
        from .knowledge import search

        return search(root, " ".join(args.query))
    if args.command == "ask":
        if args.request_file is not None:
            if args.question:
                raise WikiError("argument_error", "Use either --request or a question on the command line.")
            request = _read_json_object(args.request_file, "request")
        else:
            question_parts = getattr(args, "question", [])
            question = " ".join(question_parts)
            if not question.strip():
                raise WikiError("argument_error", "Provide a question positionally or use --request FILE.")
            request = {"question": question}
        return ask(root, request)
    if args.command == "feedback" and args.feedback_command == "submit":
        from .feedback import submit_feedback

        return submit_feedback(root, _read_json_object(args.feedback_file, "feedback"))
    if args.command == "feedback" and args.feedback_command == "status":
        from .feedback import feedback_status

        return feedback_status(root, args.feedback_id)
    if args.command == "lint":
        from .knowledge import lint

        return lint(root)
    if args.command == "maintenance" and args.maintenance_command == "run":
        from .maintenance import run_maintenance

        config = load_config(root, purpose="maintenance")
        execute = lambda prompt: run_opencode(root, config, prompt)
        return run_maintenance(root, execute, limit=args.limit)
    if args.command == "maintenance" and args.maintenance_command == "complete":
        from .maintenance import complete_job

        return complete_job(root, args.job_id, args.revision)
    if args.command == "maintenance" and args.maintenance_command == "retry":
        from .maintenance import retry_job

        return retry_job(root, args.job_id)
    raise WikiError("argument_error", "Unknown wiki command.")


def _operation_failed(args: argparse.Namespace, result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if args.command == "lint":
        errors = result.get("errors")
        return isinstance(errors, list) and bool(errors)
    if args.command == "maintenance" and args.maintenance_command == "run":
        items = result.get("items")
        return isinstance(items, list) and any(
            isinstance(item, dict) and item.get("status") == "failed" for item in items
        )
    return False


def _emit(value: Any, stream: TextIO | None = None) -> None:
    if stream is None:
        stream = sys.stdout
    stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
    stream.flush()


def _sigterm_as_interrupt(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    previous_sigterm = signal.signal(signal.SIGTERM, _sigterm_as_interrupt)
    try:
        args = _parse_args(parser, arguments)
        root = _select_root(args.root)
        result = _dispatch(args, root)
        _emit(result)
        if _operation_failed(args, result):
            print("Command failed; details are in the JSON result.", file=sys.stderr)
            return 1
        return 0
    except _HelpRequested as requested:
        _emit({"help": requested.text})
        return 0
    except WikiError as exc:
        print(exc.message, file=sys.stderr)
        _emit({"error": {"code": exc.code, "message": exc.message}})
        return 1
    except KeyboardInterrupt:
        message = "Operation cancelled."
        print(message, file=sys.stderr)
        _emit({"error": {"code": "cancelled", "message": message}})
        return 130
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        _emit({"error": {"code": "internal_error", "message": "Internal CLI error."}})
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
