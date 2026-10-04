"""Agent adapters, the plug-in registry and task-prompt compilation."""

import json
import uuid

import pytest

from helpers import PYTHON, graph_of, write_files
from rsde.agents import (
    AgentAdapter,
    AgentConfigError,
    AgentTask,
    ClaudeCodeAdapter,
    CodexAdapter,
    CommandAdapter,
    ManualAdapter,
    NoneAdapter,
    ReplayAdapter,
    create_adapter,
    render_task_prompt,
)
from rsde.agents.command import MAX_PROMPT_ARG_BYTES
from rsde.repository.config import Config
from rsde.repository.state import CheckResult

SPECS = {
    "master.md": """
        # Shop
        @goal Sell things.
        @constraint Standard library only.
        @spec catalog.spec.md
        @spec cart.spec.md
        """,
    "catalog.spec.md": """
        # Catalog
        @goal List products.
        @provides catalog.api — product lookup
        @implement shop/catalog.py
        @verify python3 -m unittest tests.test_catalog
        """,
    "cart.spec.md": """
        # Cart
        @goal Hold products before checkout.
        @requires catalog.api
        @depends tool:python3
        @invariant Totals are never negative.
        @behavior `Cart.total()` sums line prices.
        @implement shop/cart.py, tests/test_cart.py
        @verify python3 -m unittest tests.test_cart
        @done Cart tests pass.

        Prices are integers in cents.
        """,
}


def task(tmp_path, prompt="do the thing", **overrides):
    values = dict(
        spec_id="cart",
        spec_path="cart.spec.md",
        title="Cart",
        prompt=prompt,
        attempt=1,
        max_attempts=2,
        workspace=tmp_path,
        scope=("shop/cart.py",),
        verify_commands=("true",),
        prompt_file=tmp_path / "task.md",
        log_file=tmp_path / "agent.log",
        timeout=30,
    )
    values.update(overrides)
    (tmp_path / "task.md").write_text(prompt)
    return AgentTask(**values)


def test_prompt_compiles_scope_intent_interfaces_and_failures(tmp_path):
    write_files(tmp_path, SPECS)
    graph = graph_of(tmp_path)
    failure = CheckResult("python3 -m unittest tests.test_cart", False, 1, output="AssertionError: 3 != 4")
    missing = CheckResult("rsde: declared implementation exists", False, 1, output="missing: shop/cart.py", builtin=True)
    prompt = render_task_prompt(
        graph, "cart", attempt=2, max_attempts=3, results=[missing, failure], files=[], scope_violations=["x.py"]
    )
    assert prompt.startswith("# RSDE task: satisfy spec `cart`")
    assert "Attempt 2 of 3." in prompt
    assert "- `shop/cart.py` — missing" in prompt
    assert "- Standard library only. _(inherited from `master`)_" in prompt
    assert "- Totals are never negative." in prompt
    assert "1. `Cart.total()` sums line prices." in prompt
    assert "- Requires `catalog.api` from `catalog` (Catalog; implemented in `shop/catalog.py`)" in prompt
    assert "- External: `tool:python3`" in prompt
    assert "Prices are integers in cents." in prompt
    assert "Missing implementation — missing: shop/cart.py" in prompt
    assert "AssertionError: 3 != 4" in prompt
    assert "`x.py`" in prompt and "Revert" in prompt
    assert "`master.md`" in prompt  # specs are listed as protected


def test_prompt_is_deterministic_and_lists_children_of_composites(tmp_path):
    write_files(tmp_path, SPECS)
    graph = graph_of(tmp_path)
    assert render_task_prompt(graph, "master") == render_task_prompt(graph, "master")
    prompt = render_task_prompt(graph, "master")
    assert "### Child specs (already satisfied" in prompt
    assert "- `catalog` — Catalog: List products. (owns `shop/catalog.py`)" in prompt
    assert "declares no @verify" in prompt


def test_command_adapter_substitutes_placeholders(tmp_path):
    script = "import sys, pathlib; pathlib.Path('out.txt').write_text(' '.join(sys.argv[1:]))"
    adapter = CommandAdapter(
        {"command": [PYTHON, "-c", script, "{spec_id}", "{attempt}", "{prompt_file}"]}, workspace=tmp_path
    )
    result = adapter.run(task(tmp_path))
    assert result.completed and result.exit_code == 0
    assert (tmp_path / "out.txt").read_text() == f"cart 1 {tmp_path / 'task.md'}"
    assert (tmp_path / "agent.log").exists()


def test_command_adapter_sends_the_prompt_on_stdin_without_placeholders(tmp_path):
    script = "import sys, os, pathlib; pathlib.Path('out.txt').write_text(sys.stdin.read() + os.environ['RSDE_SPEC_ID'])"
    adapter = CommandAdapter({"command": [PYTHON, "-c", script]}, workspace=tmp_path)
    assert adapter.run(task(tmp_path, prompt="PROMPT:")).completed
    assert (tmp_path / "out.txt").read_text() == "PROMPT:cart"


def test_command_adapter_points_at_the_file_for_huge_prompts(tmp_path):
    adapter = CommandAdapter({"command": ["agent", "{prompt}"]}, workspace=tmp_path)
    argv, stdin = adapter.build_argv(task(tmp_path, prompt="x" * (MAX_PROMPT_ARG_BYTES + 1)))
    assert stdin is None and argv[1].startswith("Read the task in ")


def test_command_adapter_reports_failures_and_timeouts(tmp_path):
    failing = CommandAdapter({"command": [PYTHON, "-c", "raise SystemExit(4)"]}, workspace=tmp_path)
    result = failing.run(task(tmp_path))
    assert not result.completed and result.exit_code == 4 and "status 4" in result.summary
    slow = CommandAdapter({"command": [PYTHON, "-c", "import time; time.sleep(30)"], "timeout": 0.5}, workspace=tmp_path)
    assert "timed out" in slow.run(task(tmp_path)).summary
    missing = CommandAdapter({"command": ["rsde-no-such-agent"]}, workspace=tmp_path)
    assert "not found" in missing.unavailable_reason()
    assert "could not start" in missing.run(task(tmp_path)).summary


def test_command_adapter_requires_a_command(tmp_path):
    with pytest.raises(AgentConfigError, match="needs a command"):
        CommandAdapter({}, workspace=tmp_path)


def test_claude_and_codex_presets(tmp_path):
    claude_argv, claude_stdin = ClaudeCodeAdapter({}, workspace=tmp_path).build_argv(
        task(tmp_path, prompt="P", verify_commands=("python3 -m unittest tests.test_cart", "make a && make b"))
    )
    assert claude_argv[:3] == ["claude", "-p", "P"] and "acceptEdits" in claude_argv and claude_stdin is None
    assert claude_argv[-3:] == [
        "--allowedTools",
        "Bash(python3 -m unittest tests.test_cart)",
        "Bash(python3 -m unittest tests.test_cart *)",
    ]  # compound commands are never pre-approved
    locked, _ = ClaudeCodeAdapter({"allow_verify_commands": False}, workspace=tmp_path).build_argv(task(tmp_path))
    assert "--allowedTools" not in locked
    codex_argv, codex_stdin = CodexAdapter({}, workspace=tmp_path).build_argv(task(tmp_path, prompt="P"))
    assert codex_argv[:2] == ["codex", "exec"] and codex_argv[-1] == "-" and codex_stdin == "P"
    extra, _ = ClaudeCodeAdapter({"args": ["--model", "opus"]}, workspace=tmp_path).build_argv(
        task(tmp_path, verify_commands=())
    )
    assert extra[-2:] == ["--model", "opus"]


def test_replay_copies_only_files_in_scope(tmp_path):
    write_files(tmp_path, {"ref/shop/cart.py": "cart", "ref/shop/catalog.py": "catalog", "ref/tests/test_cart.py": "t"})
    adapter = ReplayAdapter({"source": "ref"}, workspace=tmp_path)
    assert adapter.unavailable_reason() is None
    result = adapter.run(task(tmp_path, scope=("shop/cart.py", "tests/")))
    assert result.completed and "copied 2 file(s)" in result.summary
    assert (tmp_path / "shop/cart.py").read_text() == "cart"
    assert not (tmp_path / "shop/catalog.py").exists()
    with pytest.raises(AgentConfigError, match="source"):
        ReplayAdapter({}, workspace=tmp_path).unavailable_reason()


def test_none_and_manual_adapters(tmp_path):
    assert not NoneAdapter(workspace=tmp_path).implements
    result = ManualAdapter({"interactive": False}, workspace=tmp_path).run(task(tmp_path))
    assert result.deferred and "task.md" in result.summary


def test_registry_resolves_builtins_configured_agents_and_python_classes(tmp_path):
    module = f"rsde_test_agent_{uuid.uuid4().hex}"
    write_files(
        tmp_path,
        {
            f"{module}.py": """
                from rsde.agents import AgentAdapter, AgentResult

                class Mine(AgentAdapter):
                    description = "test agent"

                    def run(self, task):
                        return AgentResult(True, "mine")
                """
        },
    )
    config = Config(
        agents={
            "custom": {"type": "command", "command": ["my-agent"]},
            "py": {"type": "python", "class": f"{module}:Mine"},
            "claude": {"args": ["--verbose"]},
        }
    )
    assert isinstance(create_adapter("none", config, tmp_path), NoneAdapter)
    custom = create_adapter("custom", config, tmp_path)
    assert isinstance(custom, CommandAdapter) and custom.name == "custom" and custom.command == ["my-agent"]
    claude = create_adapter("claude", config, tmp_path)
    assert isinstance(claude, ClaudeCodeAdapter) and claude.args == ["--verbose"]
    mine = create_adapter("py", config, tmp_path)
    assert isinstance(mine, AgentAdapter) and mine.name == "py" and mine.run(task(tmp_path)).summary == "mine"
    direct = create_adapter(f"{module}:Mine", Config(), tmp_path)
    assert type(direct).__name__ == "Mine"


def test_registry_errors_are_helpful(tmp_path):
    with pytest.raises(AgentConfigError, match="did you mean `claude`"):
        create_adapter("claud", Config(), tmp_path)
    with pytest.raises(AgentConfigError, match="cannot import"):
        create_adapter("x", Config(agents={"x": {"type": "python", "class": "rsde_missing_mod:X"}}), tmp_path)
    with pytest.raises(AgentConfigError, match="not a subclass"):
        create_adapter("x", Config(agents={"x": {"type": "python", "class": "pathlib:Path"}}), tmp_path)
    with pytest.raises(AgentConfigError, match="package.module:ClassName"):
        create_adapter("x", Config(agents={"x": {"type": "python", "class": "nocolon"}}), tmp_path)


CLAUDE_EVENTS = [
    {"type": "system", "subtype": "init", "model": "claude-test"},
    {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "I'll write the cart.\nDetails follow."},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "WORKSPACE/shop/cart.py"}},
            ]
        },
    },
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
    {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "total_cost_usd": 0.0123, "result": "Done."},
]


def fake_claude(tmp_path, events):
    script = "import json, sys\nfor line in json.loads(sys.argv[1]): print(line if isinstance(line, str) else json.dumps(line))"
    payload = json.dumps(events).replace("WORKSPACE", json.dumps(str(tmp_path))[1:-1])
    return ClaudeCodeAdapter({"command": [PYTHON, "-c", script, payload]}, workspace=tmp_path)


def test_claude_stream_json_is_rendered_live_and_interpreted(tmp_path):
    shown = []
    result = fake_claude(tmp_path, CLAUDE_EVENTS).run(task(tmp_path, on_output=shown.append))
    assert shown == [
        "Claude Code session started (model claude-test)",
        "I'll write the cart.",
        "Edit shop/cart.py",
    ]
    assert result.completed and result.summary == "Claude Code finished · 3 turns · $0.01" and result.output == "Done."


def test_tool_paths_are_made_relative_before_truncating(tmp_path):
    workspace = tmp_path / ("long-workspace-" * 12)
    adapter = ClaudeCodeAdapter(workspace=workspace)
    event = {
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use",
            "name": "Edit",
            "input": {"file_path": str(workspace / "shop" / "cart.py")},
        }]},
    }
    assert adapter.format_line(json.dumps(event)) == "Edit shop/cart.py"


def test_claude_error_results_are_not_completed(tmp_path):
    events = [{"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 9}]
    result = fake_claude(tmp_path, events).run(task(tmp_path))
    assert not result.completed and result.summary == "Claude Code failed · 9 turns"


def test_claude_plain_text_output_is_shown_verbatim(tmp_path):
    shown = []
    result = fake_claude(tmp_path, ["plain text answer", "{not json"]).run(task(tmp_path, on_output=shown.append))
    assert shown == ["plain text answer", "{not json"]
    assert result.completed and "exited with status 0" in result.summary
