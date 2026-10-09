"""Configuration loading for the local wiki runner."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .errors import WikiError


_AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_ALLOWED_ASK_KEYS = {
    "executable",
    "agent",
    "model",
    "variant",
    "timeout_seconds",
    "max_steps",
}


@dataclass(frozen=True)
class RunConfig:
    root: Path
    executable: str
    agent: str
    model: str
    variant: str | None
    timeout_seconds: int
    max_steps: int
    purpose: str
    profile_prompt: str = ""


def _root_path(root: Path) -> Path:
    root = Path(root)
    if not root.is_absolute():
        raise WikiError("configuration_error", "Wiki root must be an absolute path.")
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise WikiError("configuration_error", f"Failed to open the wiki root: {exc}.") from exc
    if not resolved.is_dir():
        raise WikiError("configuration_error", "Wiki root must be a directory.")
    return resolved


def _profile_mode(root: Path, agent: str) -> tuple[str, str]:
    profile = root / ".opencode" / "agents" / f"{agent}.md"
    try:
        for directory in (root / ".opencode", root / ".opencode" / "agents", profile):
            if directory.is_symlink():
                raise WikiError("configuration_error", f"The agent profile must not use a symlink: {directory.name}.")
        text = profile.read_text(encoding="utf-8")
    except WikiError:
        raise
    except OSError as exc:
        raise WikiError("configuration_error", f"OpenCode profile for agent {agent} was not found: {exc}.") from exc

    if not text.startswith("---\n"):
        raise WikiError("configuration_error", f"The {agent} profile must include YAML frontmatter.")
    end = text.find("\n---", 4)
    if end < 0:
        raise WikiError("configuration_error", f"The {agent} profile YAML frontmatter must be closed.")
    frontmatter = text[4:end]
    modes = re.findall(r"(?m)^mode:\s*([A-Za-z_-]+)\s*$", frontmatter)
    if len(modes) != 1:
        raise WikiError("configuration_error", f"The {agent} profile must contain exactly one `mode: primary` declaration.")
    mode = modes[0].strip()
    if mode != "primary":
        raise WikiError("configuration_error", f"The {agent} profile must be primary; OpenCode may otherwise fall back to its default agent.")

    model_fields = re.findall(r"(?m)^model:\s*(.*?)\s*$", frontmatter)
    if model_fields and any(value.strip().strip('"\'') for value in model_fields):
        raise WikiError("configuration_error", f"The {agent} profile must not override the model in wiki.toml.")
    prompt = text[end + 4 :].strip()
    if not prompt:
        raise WikiError("configuration_error", f"The {agent} profile must include instructions.")
    return mode, prompt


def load_config(root: Path, purpose: str = "ask") -> RunConfig:
    """Load a trusted wiki profile; non-ask purposes inherit ask's execution limits."""
    if purpose not in {"ask", "maintenance", "confluence"}:
        raise WikiError("configuration_error", f"Unknown run purpose: {purpose}.")
    root = _root_path(root)
    config_path = root / "wiki.toml"
    if config_path.is_symlink():
        raise WikiError("configuration_error", "The wiki.toml file must not be a symlink.")
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WikiError("configuration_error", f"Settings file {config_path.name} was not found.") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise WikiError("configuration_error", f"Failed to read wiki.toml: {exc}.") from exc

    if set(data) - {"ask"}:
        raise WikiError("configuration_error", "The wiki.toml file contains unsupported sections.")
    ask = data.get("ask")
    if not isinstance(ask, dict):
        raise WikiError("configuration_error", "The wiki.toml file must contain an [ask] section.")
    extra = set(ask) - _ALLOWED_ASK_KEYS
    if extra:
        raise WikiError("configuration_error", f"Unsupported [ask] settings: {', '.join(sorted(extra))}.")

    model = ask.get("model")
    if not isinstance(model, str) or not model.strip() or model != model.strip():
        raise WikiError("configuration_error", "Set the model in wiki.toml using provider/model format.")
    provider, separator, model_name = model.partition("/")
    if not separator or not provider or not model_name or any(char.isspace() for char in model):
        raise WikiError("configuration_error", "Set the model in wiki.toml using provider/model format.")

    configured_agent = ask.get("agent", "librarian")
    if not isinstance(configured_agent, str) or not _AGENT_ID.fullmatch(configured_agent):
        raise WikiError("configuration_error", "The agent parameter must be a safe profile identifier.")
    if purpose == "ask":
        agent = configured_agent
    elif purpose == "maintenance":
        agent = "wiki-maintainer"
    else:
        agent = "wiki-source-sync"
    _, profile_prompt = _profile_mode(root, agent)

    executable = ask.get("executable", "opencode")
    if not isinstance(executable, str) or not executable.strip() or "\x00" in executable:
        raise WikiError("configuration_error", "The executable parameter must contain a path or command name.")
    executable_path = Path(os.path.expanduser(executable))
    if executable_path.is_absolute():
        executable = str(executable_path)
    elif "/" in executable or "\\" in executable:
        executable = str((root / executable_path).resolve())

    variant = ask.get("variant")
    if variant is not None and (not isinstance(variant, str) or not variant.strip() or variant != variant.strip()):
        raise WikiError("configuration_error", "The variant parameter must be a non-empty string or omitted.")
    timeout_seconds = ask.get("timeout_seconds", 120)
    max_steps = ask.get("max_steps", 8)
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 3600:
        raise WikiError("configuration_error", "The timeout_seconds parameter must be an integer from 1 to 3600.")
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or not 1 <= max_steps <= 100:
        raise WikiError("configuration_error", "The max_steps parameter must be an integer from 1 to 100.")

    return RunConfig(
        root=root,
        executable=executable,
        agent=agent,
        model=model,
        variant=variant,
        timeout_seconds=timeout_seconds,
        max_steps=max_steps,
        purpose=purpose,
        profile_prompt=profile_prompt,
    )
