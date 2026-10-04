"""Workspace discovery and ``rsde.toml`` configuration.

Example ``rsde.toml``::

    [project]
    root = "master.md"          # the root spec
    ignore = ["docs/**"]        # never treated as implementation files

    [execute]
    agent = "claude"            # default coding-agent adapter
    max_attempts = 3
    verify_timeout = 600        # seconds per @verify command
    agent_timeout = 1800        # seconds per agent invocation
    strict_scope = false        # fail specs whose agent edits files outside @implement

    [agents.claude]             # options for one adapter
    command = ["claude", "-p", "{prompt}", "--permission-mode", "acceptEdits"]
"""

from __future__ import annotations

import difflib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_FILE = "rsde.toml"
DEFAULT_ROOT_SPEC = "master.md"

_SCHEMA: dict[str, dict[str, type | tuple[type, ...]]] = {
    "project": {"root": str, "ignore": list, "state_dir": str},
    "execute": {
        "agent": str,
        "max_attempts": int,
        "verify_timeout": (int, float),
        "agent_timeout": (int, float),
        "strict_scope": bool,
    },
}


_TYPE_NAMES: dict[type | tuple[type, ...], str] = {
    str: "a string",
    int: "an integer",
    bool: "true or false",
    list: "a list",
    (int, float): "a number",
}


class ConfigError(Exception):
    """``rsde.toml`` is missing, malformed or inconsistent."""


class WorkspaceError(Exception):
    """No RSDE workspace could be located."""


@dataclass
class Config:
    root_spec: str = DEFAULT_ROOT_SPEC
    ignore: tuple[str, ...] = ()
    state_dir: str = ".rsde"
    agent: str = "none"
    agents: dict[str, dict[str, Any]] = field(default_factory=dict)
    max_attempts: int = 3
    verify_timeout: float = 600.0
    agent_timeout: float = 1800.0
    strict_scope: bool = False
    source: Path | None = None


@dataclass(frozen=True)
class Workspace:
    """A directory governed by a root spec, plus its configuration."""

    root: Path
    config: Config

    @property
    def root_spec(self) -> str:
        return self.config.root_spec

    @property
    def state_dir(self) -> Path:
        return self.root / self.config.state_dir

    def relative(self, path: Path) -> str | None:
        """``path`` as a workspace-relative POSIX path, or None if it lies outside."""
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return None


def load_config(root: Path) -> Config:
    path = root / CONFIG_FILE
    if not path.is_file():
        return Config()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc

    config = Config(source=path)
    for section, values in data.items():
        if section == "agents":
            if not isinstance(values, dict) or not all(isinstance(v, dict) for v in values.values()):
                raise ConfigError(f"{path}: [agents] must contain tables such as [agents.claude]")
            config.agents = {name: dict(options) for name, options in values.items()}
            continue
        if section not in _SCHEMA:
            raise ConfigError(f"{path}: unknown section [{section}]{_suggest(section, [*_SCHEMA, 'agents'])}")
        if not isinstance(values, dict):
            raise ConfigError(f"{path}: [{section}] must be a table")
        for key, value in values.items():
            expected = _SCHEMA[section].get(key)
            if expected is None:
                raise ConfigError(f"{path}: unknown key `{key}` in [{section}]{_suggest(key, _SCHEMA[section])}")
            if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
                raise ConfigError(f"{path}: [{section}].{key} must be {_TYPE_NAMES[expected]}, got {value!r}")
        _apply(config, section, values, path)
    return config


def _apply(config: Config, section: str, values: dict[str, Any], path: Path) -> None:
    if section == "project":
        config.root_spec = values.get("root", config.root_spec)
        ignore = values.get("ignore", list(config.ignore))
        if not all(isinstance(item, str) for item in ignore):
            raise ConfigError(f"{path}: [project].ignore must be a list of glob strings")
        config.ignore = tuple(ignore)
        config.state_dir = values.get("state_dir", config.state_dir)
    elif section == "execute":
        config.agent = values.get("agent", config.agent)
        config.max_attempts = values.get("max_attempts", config.max_attempts)
        config.verify_timeout = float(values.get("verify_timeout", config.verify_timeout))
        config.agent_timeout = float(values.get("agent_timeout", config.agent_timeout))
        config.strict_scope = values.get("strict_scope", config.strict_scope)
        if config.max_attempts < 1:
            raise ConfigError(f"{path}: [execute].max_attempts must be at least 1")


def _suggest(word: str, options: Any) -> str:
    close = difflib.get_close_matches(word, list(options), n=1)
    return f" (did you mean `{close[0]}`?)" if close else ""


def find_workspace_root(start: Path) -> Path | None:
    """The nearest directory at or above ``start`` holding rsde.toml or master.md."""
    start = start.resolve()
    if start.is_file():
        start = start.parent
    for directory in (start, *start.parents):
        if (directory / CONFIG_FILE).is_file() or (directory / DEFAULT_ROOT_SPEC).is_file():
            return directory
    return None


def open_workspace(cwd: Path, explicit: Path | None = None, hint: Path | None = None) -> Workspace:
    """Locate the workspace: ``-C DIR`` first, then the target file's location, then ``cwd``."""
    if explicit is not None:
        root = explicit.resolve()
        if not root.is_dir():
            raise WorkspaceError(f"workspace directory {explicit} does not exist")
    else:
        found = find_workspace_root(hint) if hint is not None and hint.exists() else None
        found = found or find_workspace_root(cwd)
        if found is None:
            raise WorkspaceError(
                f"no RSDE workspace found in {cwd} or any parent directory "
                f"(looked for {CONFIG_FILE} or {DEFAULT_ROOT_SPEC}); run `rsde init` to create one"
            )
        root = found
    return Workspace(root, load_config(root))
