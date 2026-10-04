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
        raise WikiError("configuration_error", "корень wiki должен быть абсолютным путём")
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise WikiError("configuration_error", f"не удалось открыть корень wiki: {exc}") from exc
    if not resolved.is_dir():
        raise WikiError("configuration_error", "корень wiki должен быть каталогом")
    return resolved


def _profile_mode(root: Path, agent: str) -> tuple[str, str]:
    profile = root / ".opencode" / "agents" / f"{agent}.md"
    try:
        for directory in (root / ".opencode", root / ".opencode" / "agents", profile):
            if directory.is_symlink():
                raise WikiError("configuration_error", f"профиль агента не должен использовать symlink: {directory.name}")
        text = profile.read_text(encoding="utf-8")
    except WikiError:
        raise
    except OSError as exc:
        raise WikiError("configuration_error", f"не найден профиль OpenCode для агента {agent}: {exc}") from exc

    if not text.startswith("---\n"):
        raise WikiError("configuration_error", f"у профиля {agent} отсутствует YAML frontmatter")
    end = text.find("\n---", 4)
    if end < 0:
        raise WikiError("configuration_error", f"у профиля {agent} не закрыт YAML frontmatter")
    frontmatter = text[4:end]
    modes = re.findall(r"(?m)^mode:\s*([A-Za-z_-]+)\s*$", frontmatter)
    if len(modes) != 1:
        raise WikiError("configuration_error", f"у профиля {agent} должен быть указан один режим mode: primary")
    mode = modes[0].strip()
    if mode != "primary":
        raise WikiError("configuration_error", f"профиль {agent} должен быть primary; OpenCode может иначе перейти к агенту по умолчанию")

    model_fields = re.findall(r"(?m)^model:\s*(.*?)\s*$", frontmatter)
    if model_fields and any(value.strip().strip('"\'') for value in model_fields):
        raise WikiError("configuration_error", f"профиль {agent} не должен подменять модель из wiki.toml")
    prompt = text[end + 4 :].strip()
    if not prompt:
        raise WikiError("configuration_error", f"у профиля {agent} отсутствует инструкция")
    return mode, prompt


def load_config(root: Path, purpose: str = "ask") -> RunConfig:
    """Load the trusted wiki profile; maintenance inherits ask's execution limits."""
    if purpose not in {"ask", "maintenance"}:
        raise WikiError("configuration_error", f"неизвестное назначение запуска: {purpose}")
    root = _root_path(root)
    config_path = root / "wiki.toml"
    if config_path.is_symlink():
        raise WikiError("configuration_error", "wiki.toml не должен быть symlink")
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WikiError("configuration_error", f"не найден файл настроек {config_path.name}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise WikiError("configuration_error", f"не удалось прочитать wiki.toml: {exc}") from exc

    if set(data) - {"ask"}:
        raise WikiError("configuration_error", "wiki.toml содержит неподдерживаемые секции")
    ask = data.get("ask")
    if not isinstance(ask, dict):
        raise WikiError("configuration_error", "wiki.toml должен содержать секцию [ask]")
    extra = set(ask) - _ALLOWED_ASK_KEYS
    if extra:
        raise WikiError("configuration_error", f"неподдерживаемые параметры [ask]: {', '.join(sorted(extra))}")

    model = ask.get("model")
    if not isinstance(model, str) or not model.strip() or model != model.strip():
        raise WikiError("configuration_error", "задайте модель в wiki.toml как provider/model")
    provider, separator, model_name = model.partition("/")
    if not separator or not provider or not model_name or any(char.isspace() for char in model):
        raise WikiError("configuration_error", "задайте модель в wiki.toml как provider/model")

    configured_agent = ask.get("agent", "librarian")
    if not isinstance(configured_agent, str) or not _AGENT_ID.fullmatch(configured_agent):
        raise WikiError("configuration_error", "параметр agent должен быть безопасным идентификатором профиля")
    agent = configured_agent if purpose == "ask" else "wiki-maintainer"
    _, profile_prompt = _profile_mode(root, agent)

    executable = ask.get("executable", "opencode")
    if not isinstance(executable, str) or not executable.strip() or "\x00" in executable:
        raise WikiError("configuration_error", "параметр executable должен содержать путь или имя команды")
    executable_path = Path(os.path.expanduser(executable))
    if executable_path.is_absolute():
        executable = str(executable_path)
    elif "/" in executable or "\\" in executable:
        executable = str((root / executable_path).resolve())

    variant = ask.get("variant")
    if variant is not None and (not isinstance(variant, str) or not variant.strip() or variant != variant.strip()):
        raise WikiError("configuration_error", "variant должен быть непустой строкой или отсутствовать")
    timeout_seconds = ask.get("timeout_seconds", 120)
    max_steps = ask.get("max_steps", 8)
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 3600:
        raise WikiError("configuration_error", "timeout_seconds должен быть целым числом от 1 до 3600")
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or not 1 <= max_steps <= 100:
        raise WikiError("configuration_error", "max_steps должен быть целым числом от 1 до 100")

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
