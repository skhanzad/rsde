"""Regression tests for behaviour found in review; each mirrors a concrete failure scenario."""

import shutil
import subprocess

import pytest

from helpers import ScriptedAgent, chain, graph_of, workspace, write_files, writes
from rsde.cli.main import main
from rsde.engine import ExecuteOptions, Executor
from rsde.planning import Fingerprinter, Status, evaluate
from rsde.repository.state import StateStore


def execute(root, agent, **options):
    options.setdefault("max_attempts", 2)
    executor = Executor(graph_of(root), workspace(root), agent, options=ExecuteOptions(**options))
    return executor.execute("master")


def deletes(rel):
    def action(root, task):
        (root / rel).unlink(missing_ok=True)

    return action


# An ancestor owns the acceptance test; a child's agent "cheats" by rewriting it.
GUARDED = {
    "master.md": "# Root\n@goal g\n@spec child.spec.md\n@implement accept.py\n@verify {python} accept.py\n",
    "child.spec.md": "# Child\n@goal c\n@implement impl.txt\n@verify {python} -c \"from pathlib import Path; assert Path('impl.txt').is_file()\"\n",
    "accept.py": "from pathlib import Path\nassert 'correct' in Path('impl.txt').read_text()\n",
}
CHEAT = {"child": chain(writes("impl.txt", "wrong\n"), writes("accept.py", "pass\n"))}


def test_strict_scope_violations_survive_a_plain_rerun(tmp_path):
    write_files(tmp_path, GUARDED)
    first = execute(tmp_path, ScriptedAgent(tmp_path, CHEAT), strict_scope=True)
    assert first.outcomes["child"].status is Status.FAILED
    recorded = StateStore(tmp_path / ".rsde").violations
    assert list(recorded) == ["child"] and list(recorded["child"]) == ["accept.py"]

    second = execute(tmp_path, ScriptedAgent(tmp_path), strict_scope=True)  # same options, no agent work
    assert not second.satisfied
    assert second.outcomes["child"].status is Status.FAILED
    assert "accept.py" in second.outcomes["child"].message and "--accept-scope-changes" in second.outcomes["child"].message
    assert second.statuses["child"].status is Status.FAILED and not second.statuses["master"].satisfied


def test_restoring_the_file_clears_the_violation_and_the_real_test_catches_the_cheat(tmp_path):
    write_files(tmp_path, GUARDED)
    execute(tmp_path, ScriptedAgent(tmp_path, CHEAT), strict_scope=True)
    write_files(tmp_path, {"accept.py": GUARDED["accept.py"]})  # a human restores the acceptance test
    report = execute(tmp_path, ScriptedAgent(tmp_path), strict_scope=True)
    assert StateStore(tmp_path / ".rsde").violations == {}
    assert report.outcomes["child"].status is Status.SATISFIED
    assert report.outcomes["master"].status is Status.FAILED  # "wrong" no longer passes the real test


def test_reviewed_changes_can_be_accepted(tmp_path):
    write_files(tmp_path, GUARDED)
    execute(tmp_path, ScriptedAgent(tmp_path, CHEAT), strict_scope=True)
    report = execute(tmp_path, ScriptedAgent(tmp_path), strict_scope=True, accept_scope_changes=True)
    assert report.satisfied and StateStore(tmp_path / ".rsde").violations == {}


def test_agent_edits_to_a_spec_stay_failed_until_restored_or_accepted(tmp_path):
    write_files(tmp_path, GUARDED)
    tamper = {"child": chain(writes("impl.txt", "correct\n"), writes("child.spec.md", "# Child\n@verify {python} --version\n"))}
    first = execute(tmp_path, ScriptedAgent(tmp_path, tamper))
    assert first.aborted and "child.spec.md" in first.aborted
    second = execute(tmp_path, ScriptedAgent(tmp_path))  # the graph now comes from the edited spec
    assert second.outcomes["child"].status is Status.FAILED and "child.spec.md" in second.outcomes["child"].message
    write_files(tmp_path, {"child.spec.md": GUARDED["child.spec.md"]})
    assert execute(tmp_path, ScriptedAgent(tmp_path)).satisfied


def test_strict_scope_accepts_an_agent_that_reverts_its_stray_edit(tmp_path):
    write_files(tmp_path, GUARDED)
    agent = ScriptedAgent(
        tmp_path,
        {"child": [chain(writes("impl.txt", "correct\n"), writes("notes.txt", "x")), deletes("notes.txt")]},
    )
    report = execute(tmp_path, agent, strict_scope=True)
    assert report.satisfied
    assert len(report.outcomes["child"].attempts) == 2
    assert StateStore(tmp_path / ".rsde").evidence["child"].scope_violations == []


def test_a_custom_state_directory_is_never_an_out_of_scope_change(tmp_path):
    write_files(tmp_path, GUARDED)
    agent = ScriptedAgent(tmp_path, {"child": writes("impl.txt", "correct\n")})
    graph = graph_of(tmp_path, ignore=("rsde-state/",))
    executor = Executor(
        graph, workspace(tmp_path, state_dir="rsde-state"), agent, options=ExecuteOptions(strict_scope=True)
    )
    report = executor.execute("master")
    assert report.satisfied and report.outcomes["child"].attempts[0].out_of_scope == []
    assert (tmp_path / "rsde-state" / "state.json").exists()


def test_settling_retries_specs_blocked_by_a_dependency_that_was_only_stale(tmp_path):
    write_files(
        tmp_path,
        {
            "master.md": "# R\n@goal g\n@spec a.spec.md\n@spec b.spec.md\n@spec c.spec.md\n",
            "a.spec.md": "@goal a\n@provides a.cap\n@implement a.txt\n@verify {python} -c \"from pathlib import Path; assert 'alpha' in Path('a.txt').read_text()\"\n",
            "b.spec.md": "@goal b\n@implement b.txt\n@verify {python} -c \"from pathlib import Path; assert Path('b.txt').is_file()\"\n",
            "c.spec.md": "@goal c\n@requires a.cap\n@implement c.txt\n@verify {python} -c \"from pathlib import Path; assert Path('c.txt').is_file()\"\n",
        },
    )

    def append_comment(root, task):
        with (root / "a.txt").open("a") as fh:
            fh.write("# touched by b\n")

    agent = ScriptedAgent(
        tmp_path,
        {
            "a": writes("a.txt", "alpha\n"),
            "b": chain(writes("b.txt", "b"), append_comment),
            "c": writes("c.txt", "c"),
        },
    )
    report = execute(tmp_path, agent)
    assert report.satisfied, {k: (v.status, v.message) for k, v in report.outcomes.items()}
    assert agent.spec_ids == ["a", "b", "c"]


def test_missing_external_tools_block_even_with_fresh_evidence(tmp_path, monkeypatch):
    write_files(
        tmp_path,
        {"master.md": "@spec a.spec.md\n", "a.spec.md": "@goal a\n@depends env:RSDE_REGRESSION\n@verify {python} --version\n"},
    )
    monkeypatch.setenv("RSDE_REGRESSION", "1")
    assert execute(tmp_path, ScriptedAgent(tmp_path)).satisfied
    monkeypatch.delenv("RSDE_REGRESSION")
    graph = graph_of(tmp_path)
    fingerprinter = Fingerprinter(graph)
    statuses = evaluate(graph, fingerprinter.compute(), StateStore(tmp_path / ".rsde").evidence)
    assert statuses["a"].status is Status.BLOCKED and "RSDE_REGRESSION" in statuses["a"].reason
    assert not statuses["master"].satisfied


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_ignored_files_a_spec_owns_are_visible(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    write_files(
        tmp_path,
        {
            ".gitignore": "dist/\n",
            "master.md": "@spec build.spec.md\n",
            "build.spec.md": "@goal a build\n@implement dist/app.js\n@verify {python} -c \"from pathlib import Path; assert Path('dist/app.js').is_file()\"\n",
            "dist/app.js": "built",
        },
    )
    report = execute(tmp_path, ScriptedAgent(tmp_path))
    assert report.satisfied, report.outcomes["build"].message
    (tmp_path / "dist/app.js").write_text("rebuilt with new content")
    graph = graph_of(tmp_path)
    statuses = evaluate(graph, Fingerprinter(graph).compute(), StateStore(tmp_path / ".rsde").evidence)
    assert statuses["build"].status is Status.STALE


# ----------------------------------------------------------------------------- CLI


def test_dash_c_resolves_paths_like_git(tmp_path, monkeypatch, capsys):
    write_files(tmp_path, {"proj/master.md": "# M\n@goal g\n@verify {python} --version\n"})
    monkeypatch.chdir(tmp_path)
    assert main(["-C", "proj", "check", "master.md"]) == 0
    assert main(["check", "-C", "proj", "master.md"]) == 0


def test_status_exits_1_until_the_target_is_satisfied(tmp_path, monkeypatch, capsys):
    write_files(tmp_path, {"master.md": "# M\n@goal g\n@verify {python} --version\n"})
    monkeypatch.chdir(tmp_path)
    assert main(["status"]) == 1
    assert main(["execute"]) == 0
    assert main(["status"]) == 0


def test_numeric_options_must_be_positive(tmp_path, monkeypatch, capsys):
    write_files(tmp_path, {"master.md": "# M\n"})
    monkeypatch.chdir(tmp_path)
    for option in (["--max-attempts", "0"], ["--verify-timeout", "-1"], ["--max-attempts", "two"]):
        with pytest.raises(SystemExit) as exc:
            main(["execute", *option])
        assert exc.value.code == 2


def test_init_refuses_an_existing_file(tmp_path, monkeypatch, capsys):
    (tmp_path / "taken").write_text("")
    monkeypatch.chdir(tmp_path)
    assert main(["init", "taken"]) == 2
    assert "not a directory" in capsys.readouterr().err


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_affected_since_follows_renamed_files_to_their_old_owner(tmp_path, monkeypatch, capsys):
    write_files(
        tmp_path,
        {
            "master.md": "@spec a.spec.md\n@spec b.spec.md\n",
            "a.spec.md": "@goal a\n@implement old.txt\n@verify {python} --version\n",
            "b.spec.md": "@goal b\n@implement new.txt\n@verify {python} --version\n",
            "old.txt": "content\n",
        },
    )
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "mv", "old.txt", "new.txt"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    assert main(["--color", "never", "affected", "--since", "HEAD"]) == 0
    out = capsys.readouterr().out
    assert "changed a" in out and "changed b" in out
