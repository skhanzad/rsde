"""Run any command-line coding agent, plus presets for Claude Code and Codex.

The command is a list of arguments. These placeholders are substituted:

=================  ==========================================================
``{prompt}``       the full task prompt (falls back to a pointer at the
                   prompt file when the prompt is too long for one argument)
``{prompt_file}``  path of the task prompt written under ``.rsde/runs/``
``{spec_id}``      id of the spec being implemented
``{spec_file}``    workspace-relative path of that spec
``{workspace}``    absolute workspace root
``{attempt}``      attempt number, starting at 1
=================  ==========================================================

If neither ``{prompt}`` nor ``{prompt_file}`` appears, the prompt is written to
the agent's standard input. The agent always runs with the workspace root as
its working directory and with ``RSDE_*`` environment variables set.
"""

from __future__ import annotations

import shlex
import shutil
from pathlib import Path
from typing import Any, ClassVar, Mapping

from rsde.agents.base import AgentAdapter, AgentConfigError, AgentResult, AgentTask
from rsde.process import run_process, tail

#: Linux limits a single argv entry to 128 KiB; stay well below it.
MAX_PROMPT_ARG_BYTES = 100_000


class CommandAdapter(AgentAdapter):
    name = "command"
    description = "any CLI agent configured with `command = [...]` in rsde.toml"
    default_command: ClassVar[tuple[str, ...]] = ()

    def __init__(self, options: Mapping[str, Any] | None = None, *, workspace: Path, name: str | None = None) -> None:
        super().__init__(options, workspace=workspace, name=name)
        command = self.options.get("command", list(self.default_command))
        if isinstance(command, str):
            command = shlex.split(command)
        if not command:
            raise AgentConfigError(
                f"agent `{self.name}` needs a command, e.g.\n"
                f'  [agents.{self.name or "my-agent"}]\n'
                '  type = "command"\n'
                '  command = ["my-agent", "--task-file", "{prompt_file}"]'
            )
        self.command = [str(part) for part in command]
        self.args = [str(part) for part in self.options.get("args", [])]
        self.env = {str(k): str(v) for k, v in dict(self.options.get("env", {})).items()}
        self.timeout = float(self.options["timeout"]) if "timeout" in self.options else None
        self.echo = bool(self.options.get("echo", True))

    def unavailable_reason(self) -> str | None:
        executable = self.command[0]
        if shutil.which(executable) is None and not (self.workspace / executable).is_file():
            return f"`{executable}` was not found on PATH"
        return None

    def build_argv(self, task: AgentTask) -> tuple[list[str], str | None]:
        """The argv to run and the text to send on stdin (if any)."""
        prompt = task.prompt
        if len(prompt.encode("utf-8")) > MAX_PROMPT_ARG_BYTES:
            prompt = f"Read the task in {task.prompt_file} and carry it out exactly."
        values = {
            "{prompt}": prompt,
            "{prompt_file}": str(task.prompt_file),
            "{spec_id}": task.spec_id,
            "{spec_file}": task.spec_path,
            "{workspace}": str(task.workspace),
            "{attempt}": str(task.attempt),
        }
        parts = [*self.command, *self.args]
        uses_prompt = any("{prompt}" in p or "{prompt_file}" in p for p in parts)
        argv = []
        for part in parts:
            for key, value in values.items():
                part = part.replace(key, value)
            argv.append(part)
        return argv, None if uses_prompt else task.prompt

    def run(self, task: AgentTask) -> AgentResult:
        argv, stdin = self.build_argv(task)
        env = {
            **self.env,
            "RSDE_SPEC_ID": task.spec_id,
            "RSDE_SPEC_FILE": task.spec_path,
            "RSDE_PROMPT_FILE": str(task.prompt_file),
            "RSDE_ATTEMPT": str(task.attempt),
            "RSDE_WORKSPACE": str(task.workspace),
        }
        result = run_process(
            argv,
            cwd=task.workspace,
            env=env,
            timeout=self.timeout or task.timeout,
            stdin_text=stdin,
            on_output=task.on_output if self.echo else None,
            log_path=task.log_file,
        )
        if result.error:
            return AgentResult(False, f"could not start `{argv[0]}`: {result.error}", result.output, None, result.duration)
        if result.timed_out:
            return AgentResult(False, f"`{argv[0]}` timed out", tail(result.output), None, result.duration)
        summary = f"`{argv[0]}` exited with status {result.exit_code}"
        return AgentResult(result.exit_code == 0, summary, tail(result.output), result.exit_code, result.duration)


class ClaudeCodeAdapter(CommandAdapter):
    """Claude Code in headless print mode, allowed to edit files."""

    name = "claude"
    description = "Claude Code (`claude -p`) with file edits auto-accepted"
    default_command = ("claude", "-p", "{prompt}", "--permission-mode", "acceptEdits", "--output-format", "text")


class CodexAdapter(CommandAdapter):
    """OpenAI Codex CLI in non-interactive exec mode, sandboxed to the workspace."""

    name = "codex"
    description = "OpenAI Codex (`codex exec`) sandboxed to workspace writes; prompt on stdin"
    default_command = ("codex", "exec", "--sandbox", "workspace-write", "--skip-git-repo-check", "--color", "never", "-")
