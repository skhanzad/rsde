"""A deterministic, offline agent that replays a reference implementation.

For the spec being executed, every file under ``source`` whose relative path
is claimed by the spec's ``@implement`` patterns is copied into the
workspace. It needs no network or model, so it is ideal for demos, tests and
CI dry-runs of the full execution lifecycle — and it exercises the spec's
ownership declarations: a spec that forgets to claim a file it needs fails.

Configure it in rsde.toml::

    [agents.replay]
    source = ".reference"
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from rsde.agents.base import AgentAdapter, AgentConfigError, AgentResult, AgentTask
from rsde.repository.files import IGNORED_DIRS, matching_files


class ReplayAdapter(AgentAdapter):
    name = "replay"
    description = "offline agent that copies each spec's files from a reference tree"

    @property
    def source(self) -> Path:
        source = self.options.get("source")
        if not source:
            raise AgentConfigError(
                'the replay agent needs a reference tree: add\n  [agents.replay]\n  source = "path/to/reference"\n'
                "to rsde.toml"
            )
        return (self.workspace / str(source)).resolve()

    def unavailable_reason(self) -> str | None:
        if not self.source.is_dir():
            return f"reference directory `{self.source}` does not exist"
        return None

    def run(self, task: AgentTask) -> AgentResult:
        started = time.monotonic()
        source = self.source
        available = sorted(
            p.relative_to(source).as_posix()
            for p in source.rglob("*")
            if p.is_file() and not any(part in IGNORED_DIRS for part in p.relative_to(source).parts)
        )
        selected = matching_files(available, task.scope)
        for rel in selected:
            destination = task.workspace / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / rel, destination)
            if task.on_output:
                task.on_output(f"wrote {rel}")
        summary = f"copied {len(selected)} file(s) from {source.name}/"
        return AgentResult(True, summary, "\n".join(selected), 0, time.monotonic() - started)
