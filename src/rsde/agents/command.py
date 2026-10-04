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

import json
import shlex
import shutil
from pathlib import Path
from typing import Any, ClassVar, Mapping

from rsde.agents.base import AgentAdapter, AgentConfigError, AgentResult, AgentTask
from rsde.process import ProcessResult, run_process, tail

#: Linux limits a single argv entry to 128 KiB; stay well below it.
MAX_PROMPT_ARG_BYTES = 100_000


class CommandAdapter(AgentAdapter):
    name = "command"
    description = "any CLI agent configured with `command = [...]` in rsde.toml"
    default_command: ClassVar[tuple[str, ...]] = ()
    #: Environment variables removed before the agent starts.
    unset_env: ClassVar[tuple[str, ...]] = ()

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
        on_output = None
        if self.echo and task.on_output is not None:
            sink = task.on_output

            def on_output(line: str) -> None:
                shown = self.format_line(line)
                if shown:
                    for part in shown.splitlines():
                        sink(part)

        result = run_process(
            argv,
            cwd=task.workspace,
            env=env,
            unset_env=self.unset_env if self.options.get("isolate", True) else (),
            timeout=self.timeout or task.timeout,
            stdin_text=stdin,
            on_output=on_output,
            log_path=task.log_file,
        )
        if result.error:
            return AgentResult(False, f"could not start `{argv[0]}`: {result.error}", result.output, None, result.duration)
        if result.timed_out:
            return AgentResult(False, f"`{argv[0]}` timed out", tail(result.output), None, result.duration)
        return self.interpret(argv, result)

    def format_line(self, line: str) -> str | None:
        """How one line of agent output is shown live. Return None to hide it."""
        return line

    def interpret(self, argv: list[str], result: ProcessResult) -> AgentResult:
        """Turn the finished process into an AgentResult."""
        summary = f"`{argv[0]}` exited with status {result.exit_code}"
        return AgentResult(result.exit_code == 0, summary, tail(result.output), result.exit_code, result.duration)


class ClaudeCodeAdapter(CommandAdapter):
    """Claude Code in headless print mode, allowed to edit files.

    The default command asks for ``stream-json`` output so progress (tool calls,
    edits, the final result and its cost) can be shown live. Lines that are not
    recognised JSON events are shown verbatim, so a custom ``command`` using
    ``--output-format text`` works too.
    """

    name = "claude"
    description = "Claude Code (`claude -p`) with file edits auto-accepted"
    default_command = (
        "claude",
        "-p",
        "{prompt}",
        "--permission-mode",
        "acceptEdits",
        "--output-format",
        "stream-json",
        "--verbose",
    )
    # When rsde itself runs inside a Claude Code session, these variables would tie
    # the agent to that parent session. The agent must be an independent session.
    # Provider configuration (API keys, Bedrock/Vertex settings) is kept.
    unset_env = (
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_CODE_SESSION_ATTENDED",
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "CLAUDE_CODE_MESSAGING_TOKEN",
        "CLAUDE_PID",
    )

    def build_argv(self, task: AgentTask) -> tuple[list[str], str | None]:
        """Pre-approve the spec's own @verify commands, so the agent can run exactly
        the checks that define success, and nothing else, without a permission prompt."""
        argv, stdin = super().build_argv(task)
        if self.options.get("allow_verify_commands", True):
            rules = [rule for command in task.verify_commands for rule in _bash_rules(command)]
            if rules:
                argv += ["--allowedTools", *rules]  # variadic flag: keep it last
        return argv, stdin

    def format_line(self, line: str) -> str | None:
        event = _json_event(line)
        if event is None:
            return line
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            return f"Claude Code session started (model {event.get('model', '?')})"
        if kind == "assistant":
            shown = []
            for block in _content_blocks(event):
                if block.get("type") == "text" and str(block.get("text", "")).strip():
                    shown.append(_shorten(str(block["text"])))
                elif block.get("type") == "tool_use":
                    shown.append(f"{block.get('name', 'tool')} {_describe_tool_input(block.get('input'))}".rstrip())
            return "\n".join(shown).replace(f"{self.workspace}/", "") or None
        return None  # the final result is reported by the executor as the attempt summary

    def interpret(self, argv: list[str], result: ProcessResult) -> AgentResult:
        final = None
        for line in result.output.splitlines():
            event = _json_event(line)
            if event is not None and event.get("type") == "result":
                final = event
        if final is None:
            return super().interpret(argv, result)
        ok = result.exit_code == 0 and not final.get("is_error", False)
        summary = _result_summary(final)
        text = str(final.get("result", "") or "")
        return AgentResult(ok, summary, tail(text or result.output), result.exit_code, result.duration)


_SHELL_SYNTAX = ("&", "|", ";", "<", ">", "`", "$(", "(", ")", "\n")


def _bash_rules(command: str) -> list[str]:
    """Claude Code permission rules allowing ``command`` (and extra trailing arguments)."""
    command = command.strip()
    if not command or any(token in command for token in _SHELL_SYNTAX):
        return []  # compound commands cannot be expressed as one narrow rule
    return [f"Bash({command})", f"Bash({command} *)"]


def _json_event(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) and "type" in event else None


def _content_blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _shorten(text: str, width: int = 160) -> str:
    first = text.strip().splitlines()[0] if text.strip() else ""
    return first if len(first) <= width else first[: width - 1] + "…"


def _describe_tool_input(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("file_path", "path", "command", "pattern", "url", "description"):
        if value.get(key):
            return _shorten(str(value[key]), 120)
    return ""


def _result_summary(event: dict[str, Any]) -> str:
    parts = ["Claude Code " + ("failed" if event.get("is_error") else "finished")]
    if event.get("num_turns") is not None:
        parts.append(f"{event['num_turns']} turns")
    cost = event.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        parts.append(f"${cost:.2f}")
    return " · ".join(parts)


class CodexAdapter(CommandAdapter):
    """OpenAI Codex CLI in non-interactive exec mode, sandboxed to the workspace."""

    name = "codex"
    description = "OpenAI Codex (`codex exec`) sandboxed to workspace writes; prompt on stdin"
    default_command = ("codex", "exec", "--sandbox", "workspace-write", "--skip-git-repo-check", "--color", "never", "-")
