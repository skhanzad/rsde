"""The rsde command line: every command, its output formats and exit codes."""

import json
import os
import shutil
import subprocess
import sys

import pytest

from helpers import write_files
from rsde import __version__
from rsde.cli.main import main

SPECS = {
    "master.md": """
        # Root
        @goal Everything works.
        @spec lib.spec.md
        @spec app.spec.md
        @verify {python} -c "from pathlib import Path; assert Path('lib.txt').is_file()"
        """,
    "lib.spec.md": """
        # Lib
        @goal A library.
        @provides lib.api
        @implement lib.txt
        @verify {python} -c "from pathlib import Path; assert 'ok' in Path('lib.txt').read_text()"
        """,
    "app.spec.md": """
        # App
        @goal An app.
        @requires lib.api
        @implement app.txt
        @verify {python} -c "from pathlib import Path; assert 'ok' in Path('app.txt').read_text()"
        """,
    "ref/lib.txt": "ok\n",
    "ref/app.txt": "ok\n",
    "rsde.toml": """
        [project]
        ignore = ["ref/**"]
        [agents.replay]
        source = "ref"
        """,
}


@pytest.fixture
def project(tmp_path, monkeypatch):
    write_files(tmp_path, SPECS)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def rsde(capsys, *args):
    code = main(["--color", "never", *args])
    out, err = capsys.readouterr()
    return code, out, err


def test_version_and_help(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0 and __version__ in capsys.readouterr().out
    assert main([]) == 2


def test_module_entry_point():
    proc = subprocess.run([sys.executable, "-m", "rsde", "--version"], capture_output=True, text=True)
    assert proc.returncode == 0 and proc.stdout.strip() == f"rsde {__version__}"


def test_cli_handles_redirected_ascii_output(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "rsde", "init", str(tmp_path / "project")],
        env={**os.environ, "PYTHONIOENCODING": "ascii:strict"},
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert b"Initialised an RSDE workspace" in result.stdout


def test_check_valid_and_json(project, capsys):
    code, out, _ = rsde(capsys, "check")
    assert code == 0 and "spec graph is valid" in out and "3 specs" in out
    code, out, _ = rsde(capsys, "check", "--format", "json")
    data = json.loads(out)
    assert code == 0 and data["ok"] and data["specs"] == 3 and data["dependency_edges"] == 1


def test_check_reports_errors_and_strict_warnings(project, capsys):
    write_files(project, {"app.spec.md": "# App\n@requires lib.apl\n@implment app.txt\n"})
    code, out, _ = rsde(capsys, "check")
    assert code == 1
    assert "error[E301]: no spec provides capability `lib.apl`" in out
    assert "did you mean `lib.api`" in out and "error[E101]" in out
    assert "--> app.spec.md:2:1" in out and "^^^^" in out
    write_files(project, {"app.spec.md": "# App\n@requires lib.api\n"})  # valid, but unverifiable (warning)
    assert rsde(capsys, "check")[0] == 0
    assert rsde(capsys, "check", "--strict")[0] == 1


def test_check_accepts_a_root_spec_path_from_anywhere(project, capsys, monkeypatch):
    monkeypatch.chdir(project.parent)
    code, out, _ = rsde(capsys, "check", f"{project.name}/master.md")
    assert code == 0 and "3 specs" in out


def test_graph_formats(project, capsys):
    out = rsde(capsys, "graph")[1]
    assert "Hierarchy" in out and "├── lib — Lib [provides lib.api]" in out
    assert "app  requires lib.api → lib" in out
    assert rsde(capsys, "graph", "--format", "mermaid")[1].startswith("flowchart TD")
    assert rsde(capsys, "graph", "--format", "dot")[1].startswith("digraph rsde {")
    data = json.loads(rsde(capsys, "graph", "--format", "json")[1])
    assert data["root"] == "master" and data["dependencies"][0]["capability"] == "lib.api"


def test_show_and_prompt(project, capsys):
    code, out, _ = rsde(capsys, "show", "app.spec.md")
    assert code == 0 and "requires" in out and "lib.api" in out and "app.txt  missing" in out
    out = rsde(capsys, "show", "[[app]]", "--prompt")[1]
    assert out.startswith("# RSDE task: satisfy spec `app`")
    assert json.loads(rsde(capsys, "show", "lib", "--format", "json")[1])["provides"] == ["lib.api"]
    code, _, err = rsde(capsys, "show", "nope")
    assert code == 2 and "no spec named `nope`" in err


def test_execute_plan_run_status_and_exit_codes(project, capsys):
    code, out, _ = rsde(capsys, "execute", "--plan")
    assert code == 0 and "Plan for `master`" in out and "verify only" in out
    assert not (project / ".rsde").exists()

    code, out, err = rsde(capsys, "execute")  # default agent: none → verify only
    assert code == 1 and "only verifies" in err and "is not satisfied" in out

    code, out, _ = rsde(capsys, "execute", "master.md", "--agent", "replay")
    assert code == 0 and "master is satisfied" in out and "agent replay" in out

    code, out, _ = rsde(capsys, "status")
    assert code == 0 and ("✓ master" in out or "+ master" in out)
    data = json.loads(rsde(capsys, "status", "--format", "json")[1])
    assert data["satisfied"] and data["specs"]["lib"]["status"] == "satisfied"

    data = json.loads(rsde(capsys, "execute", "--format", "json", "--force", "--agent", "replay")[1])
    assert data["satisfied"] and data["outcomes"]["lib"]["checks_run"] == 2


def test_execute_rejects_unknown_or_unavailable_agents(project, capsys):
    code, _, err = rsde(capsys, "execute", "--agent", "replai")
    assert code == 2 and "did you mean `replay`" in err
    write_files(project, {"rsde.toml": SPECS["rsde.toml"] + '[agents.ghost]\ntype = "command"\ncommand = ["rsde-ghost"]\n'})
    code, _, err = rsde(capsys, "execute", "--agent", "ghost")
    assert code == 2 and "not available" in err


def test_execute_refuses_an_invalid_graph(project, capsys):
    write_files(project, {"app.spec.md": "@requires missing.cap\n"})
    code, _, err = rsde(capsys, "execute")
    assert code == 1 and "E301" in err and "fix them first" in err


def test_affected_by_spec_and_by_file(project, capsys):
    code, out, _ = rsde(capsys, "affected", "lib.spec.md")
    assert code == 0 and "3 specs may be affected" in out and "requires `lib.api` from `lib`" in out
    code, out, _ = rsde(capsys, "affected", "app.txt")
    assert code == 0 and "(owns app.txt)" in out and "2 specs may be affected" in out
    data = json.loads(rsde(capsys, "affected", "app", "--format", "json")[1])
    assert [a["spec"] for a in data["affected"]] == ["app", "master"]
    code, _, err = rsde(capsys, "affected", "nothing.txt")
    assert code == 2 and "no spec owns" in err
    assert rsde(capsys, "affected")[0] == 2


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_affected_since_a_git_revision(project, capsys):
    def git(*args):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)

    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    (project / "lib.txt").write_text("changed\n")
    code, out, _ = rsde(capsys, "affected", "--since", "HEAD")
    assert code == 0 and "changed lib" in out and "lib.txt" in out
    code, _, err = rsde(capsys, "affected", "--since", "no-such-rev")
    assert code == 2 and "git diff" in err


def test_reconcile(project, capsys):
    code, out, _ = rsde(capsys, "reconcile", "--dry-run")
    assert code == 1 and "declared implementation missing" in out and "does not conform" in out
    code, out, _ = rsde(capsys, "reconcile", "--agent", "replay")
    assert code == 0 and "repository conforms" in out
    (project / "stray.py").write_text("")
    assert rsde(capsys, "reconcile", "--dry-run")[0] == 0
    assert rsde(capsys, "reconcile", "--dry-run", "--strict")[0] == 1


def test_init(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out, _ = rsde(capsys, "init", "proj")
    assert code == 0 and (tmp_path / "proj/master.md").exists() and (tmp_path / "proj/rsde.toml").exists()
    assert ".rsde/" in (tmp_path / "proj/.gitignore").read_text()
    assert rsde(capsys, "init", "proj")[0] == 1
    assert rsde(capsys, "init", "proj", "--force")[0] == 0
    assert (tmp_path / "proj/.gitignore").read_text().count(".rsde/") == 1
    monkeypatch.chdir(tmp_path / "proj")
    code, out, _ = rsde(capsys, "check")
    assert code == 0 and "W402" in out  # the template asks for checks and children


def test_agents_command(project, capsys):
    code, out, _ = rsde(capsys, "agents")
    assert code == 0
    for name in ("none", "manual", "command", "claude", "codex", "replay"):
        assert name in out


def test_workspace_and_config_errors(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, _, err = rsde(capsys, "check")
    assert code == 2 and "no RSDE workspace found" in err
    write_files(tmp_path, {"master.md": "# M\n", "rsde.toml": "[execute]\nagent = 3\n"})
    code, _, err = rsde(capsys, "check")
    assert code == 2 and "must be a string" in err
