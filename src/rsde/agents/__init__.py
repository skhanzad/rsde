"""Agent execution: interchangeable coding-agent adapters.

Adapters are resolved by name, in this order:

1. ``[agents.NAME]`` in rsde.toml, whose ``type`` selects a built-in adapter
   (``command``, ``claude``, ``codex``, ``replay``, ``manual``, ``none``) or
   ``python`` with ``class = "package.module:ClassName"``;
2. a built-in adapter called NAME;
3. ``package.module:ClassName`` given directly as the name;
4. an installed plugin registered under the ``rsde.agents`` entry-point group.
"""

from __future__ import annotations

import difflib
import importlib
import importlib.metadata
import sys
from pathlib import Path
from typing import Any

from rsde.agents.base import AgentAdapter, AgentConfigError, AgentResult, AgentTask
from rsde.agents.builtin import ManualAdapter, NoneAdapter
from rsde.agents.command import ClaudeCodeAdapter, CodexAdapter, CommandAdapter
from rsde.agents.prompt import render_task_prompt
from rsde.agents.replay import ReplayAdapter
from rsde.repository.config import Config

BUILTIN_ADAPTERS: dict[str, type[AgentAdapter]] = {
    "none": NoneAdapter,
    "manual": ManualAdapter,
    "command": CommandAdapter,
    "claude": ClaudeCodeAdapter,
    "codex": CodexAdapter,
    "replay": ReplayAdapter,
}
ENTRY_POINT_GROUP = "rsde.agents"


def load_class(spec: str, workspace: Path) -> type[AgentAdapter]:
    """Import ``package.module:ClassName``; the workspace root is importable too."""
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise AgentConfigError(f"agent class `{spec}` must look like `package.module:ClassName`")
    added = str(workspace) not in sys.path
    if added:
        sys.path.insert(0, str(workspace))
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise AgentConfigError(f"cannot import agent module `{module_name}`: {exc}") from exc
    finally:
        if added:
            sys.path.remove(str(workspace))
    cls = getattr(module, attr, None)
    if not (isinstance(cls, type) and issubclass(cls, AgentAdapter)):
        raise AgentConfigError(f"`{spec}` is not a subclass of rsde.agents.AgentAdapter")
    return cls


def _entry_points() -> dict[str, importlib.metadata.EntryPoint]:
    return {ep.name: ep for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)}


def available_agents(config: Config) -> dict[str, str]:
    """Name → description of every adapter that can be selected."""
    agents = {name: cls.description for name, cls in BUILTIN_ADAPTERS.items()}
    for name, options in config.agents.items():
        kind = str(options.get("type", name))
        base = BUILTIN_ADAPTERS[kind].description if kind in BUILTIN_ADAPTERS else f"type {kind}"
        agents[name] = f"{base} [configured]"
    for name in _entry_points():
        agents.setdefault(name, "installed plugin")
    return agents


def create_adapter(name: str, config: Config, workspace: Path) -> AgentAdapter:
    options: dict[str, Any] = dict(config.agents.get(name, {}))
    kind = str(options.pop("type", name))
    if kind == "python" or (":" in kind and kind not in BUILTIN_ADAPTERS):
        target = str(options.pop("class", kind if ":" in kind else ""))
        if not target:
            raise AgentConfigError(f'[agents.{name}] has type "python" but no `class = "package.module:Class"`')
        cls = load_class(target, workspace)
    elif kind in BUILTIN_ADAPTERS:
        cls = BUILTIN_ADAPTERS[kind]
    else:
        plugins = _entry_points()
        if kind not in plugins:
            known = sorted({*BUILTIN_ADAPTERS, *config.agents, *plugins})
            close = difflib.get_close_matches(kind, known, n=1)
            hint = f"did you mean `{close[0]}`? " if close else ""
            raise AgentConfigError(f"unknown agent `{name}`; {hint}available: {', '.join(known)}")
        loaded = plugins[kind].load()
        if not (isinstance(loaded, type) and issubclass(loaded, AgentAdapter)):
            raise AgentConfigError(f"plugin `{kind}` does not provide an rsde.agents.AgentAdapter subclass")
        cls = loaded
    return cls(options, workspace=workspace, name=name)


__all__ = [
    "BUILTIN_ADAPTERS",
    "AgentAdapter",
    "AgentConfigError",
    "AgentResult",
    "AgentTask",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "CommandAdapter",
    "ManualAdapter",
    "NoneAdapter",
    "ReplayAdapter",
    "available_agents",
    "create_adapter",
    "render_task_prompt",
]
