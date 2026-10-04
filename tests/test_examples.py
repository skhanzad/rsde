"""The shipped example workspaces stay valid and executable."""

import shutil
from pathlib import Path

from helpers import codes
from rsde.cli.main import main
from rsde.graph import build_graph
from rsde.repository.config import load_config

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_todo_example_is_a_clean_spec_graph():
    root = EXAMPLES / "todo"
    graph = build_graph(root, ignore=load_config(root).ignore)
    assert graph.diagnostics == []
    assert list(graph.specs) == ["master", "specs/model", "specs/storage", "specs/cli", "specs/cli/commands", "specs/cli/format"]
    assert graph.hierarchy.depth("specs/cli/format") == 2


def test_todo_example_executes_end_to_end_with_the_replay_agent(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "todo"
    shutil.copytree(EXAMPLES / "todo", workspace, ignore=shutil.ignore_patterns(".rsde", "__pycache__"))
    monkeypatch.chdir(workspace)

    assert main(["--color", "never", "execute", "master.md", "--agent", "replay"]) == 0
    out = capsys.readouterr().out
    assert "master is satisfied" in out and "6/6 specs satisfied" in out

    # A prose-only edit changes the fingerprint; the checks still pass, so no agent is needed.
    spec = workspace / "specs" / "storage.spec.md"
    spec.write_text(spec.read_text() + "\nA clarifying note.\n")
    assert main(["--color", "never", "execute", "--verify-only"]) == 0
    out = capsys.readouterr().out
    assert "0 agent runs" in out


def test_broken_example_reports_its_deliberate_mistakes():
    graph = build_graph(EXAMPLES / "broken", probe_environment=False)
    assert codes(graph) == ["E101", "E201", "E301", "E302", "E303", "W201", "W403"]
