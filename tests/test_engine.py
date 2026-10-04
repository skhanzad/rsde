"""The execution lifecycle and reconciliation, driven by a scripted agent."""

import pytest

from helpers import ScriptedAgent, chain, graph_of, workspace, write_files, writes
from rsde.agents import ManualAdapter, NoneAdapter
from rsde.engine import ExecuteOptions, Executor, Reconciler, survey
from rsde.planning import Action, Status
from rsde.repository.state import StateStore

SPECS = {
    "master.md": """
        # Root
        @goal Both parts work together.
        @spec lib.spec.md
        @spec app.spec.md
        @verify test -f lib.txt && test -f app.txt
        """,
    "lib.spec.md": """
        # Lib
        @goal A library.
        @provides lib.api
        @implement lib.txt
        @verify grep -q "lib ok" lib.txt
        """,
    "app.spec.md": """
        # App
        @goal An app on top of the library.
        @requires lib.api
        @implement app.txt
        @verify grep -q "app ok" app.txt
        """,
}

GOOD = {"lib": writes("lib.txt", "lib ok\n"), "app": writes("app.txt", "app ok\n")}


def setup_workspace(tmp_path, extra=None):
    write_files(tmp_path, {**SPECS, **(extra or {})})
    return graph_of(tmp_path), workspace(tmp_path)


def run(tmp_path, agent=None, target="master", extra=None, **options):
    graph, ws = setup_workspace(tmp_path, extra)
    agent = agent or ScriptedAgent(tmp_path, GOOD)
    options.setdefault("max_attempts", 2)
    executor = Executor(graph, ws, agent, options=ExecuteOptions(**options))
    return executor.execute(target), agent


def test_full_run_implements_bottom_up_and_propagates_satisfaction(tmp_path):
    report, agent = run(tmp_path)
    assert report.satisfied and report.aborted is None
    assert agent.spec_ids == ["lib", "app"]  # master's own check already passes once its children exist
    assert [report.outcomes[s].status for s in ("lib", "app", "master")] == [Status.SATISFIED] * 3
    assert report.outcomes["master"].message == "checks pass; no implementation needed"
    assert report.agent_runs == 2
    stored = StateStore(tmp_path / ".rsde")
    assert set(stored.evidence) == {"lib", "app", "master"} and all(e.passed for e in stored.evidence.values())
    assert stored.runs[-1]["satisfied"] is True
    assert (report.run_dir / "plan.json").exists() and (report.run_dir / "report.json").exists()
    assert (report.run_dir / "lib.attempt-1.prompt.md").read_text().startswith("# RSDE task")


def test_second_run_trusts_fresh_evidence(tmp_path):
    run(tmp_path)
    graph, ws = graph_of(tmp_path), workspace(tmp_path)
    agent = ScriptedAgent(tmp_path, GOOD)
    report = Executor(graph, ws, agent).execute("master")
    assert report.satisfied and agent.tasks == [] and report.checks_run == 0
    assert {s.action for s in report.plan.steps} == {Action.SKIP}


def test_failure_blocks_dependents_and_parents(tmp_path):
    report, agent = run(tmp_path, ScriptedAgent(tmp_path, {}))
    assert agent.spec_ids == ["lib", "lib"]  # two attempts, then give up
    outcomes = report.outcomes
    assert outcomes["lib"].status is Status.FAILED and outcomes["lib"].message == "still failing after 2 agent attempts"
    assert outcomes["app"].status is Status.BLOCKED and outcomes["app"].message == "waiting for lib"
    assert outcomes["master"].status is Status.INCOMPLETE
    assert not report.satisfied
    assert report.statuses["lib"].status is Status.FAILED


def test_retries_receive_the_previous_failures(tmp_path):
    agent = ScriptedAgent(tmp_path, {"lib": [writes("lib.txt", "nope\n"), writes("lib.txt", "lib ok\n")], "app": GOOD["app"]})
    report, _ = run(tmp_path, agent)
    assert report.satisfied
    lib_tasks = [t for t in agent.tasks if t.spec_id == "lib"]
    assert len(lib_tasks) == 2
    assert lib_tasks[1].failures and "grep -q" in lib_tasks[1].failures[-1].command
    assert "## What is currently unsatisfied" in lib_tasks[1].prompt
    assert report.outcomes["lib"].message == "satisfied after 2 agent attempts"


def test_existing_implementations_need_no_agent(tmp_path):
    report, agent = run(tmp_path, extra={"lib.txt": "lib ok\n", "app.txt": "app ok\n"})
    assert report.satisfied and agent.tasks == []


def test_verify_only_never_invokes_the_agent(tmp_path):
    report, agent = run(tmp_path, verify_only=True)
    assert agent.tasks == [] and report.outcomes["lib"].status is Status.FAILED
    assert report.outcomes["lib"].message == "2 check(s) failed"
    graph, ws = graph_of(tmp_path), workspace(tmp_path)
    report = Executor(graph, ws, NoneAdapter(workspace=tmp_path)).execute("master")
    assert report.outcomes["lib"].status is Status.FAILED and report.agent_runs == 0


def test_out_of_scope_changes_are_recorded_and_fail_under_strict_scope(tmp_path):
    sloppy = {"lib": chain(GOOD["lib"], writes("notes.txt", "x")), "app": GOOD["app"]}
    report, _ = run(tmp_path, ScriptedAgent(tmp_path, sloppy))
    assert report.satisfied
    assert report.outcomes["lib"].attempts[0].out_of_scope == ["notes.txt"]
    assert StateStore(tmp_path / ".rsde").evidence["lib"].scope_violations == ["notes.txt"]

    strict_dir = tmp_path / "strict"
    strict_dir.mkdir()
    report, _ = run(strict_dir, ScriptedAgent(strict_dir, sloppy), strict_scope=True)
    assert report.outcomes["lib"].status is Status.FAILED
    assert "outside the spec's scope" in report.outcomes["lib"].message


def test_an_agent_that_edits_a_spec_aborts_the_run(tmp_path):
    tamper = {"lib": chain(GOOD["lib"], writes("lib.spec.md", "# Lib\n@verify true\n"))}
    report, agent = run(tmp_path, ScriptedAgent(tmp_path, tamper))
    assert report.aborted and "lib.spec.md" in report.aborted and "source of truth" in report.aborted
    assert not report.satisfied and agent.spec_ids == ["lib"]


def test_changes_invalidate_exactly_the_affected_evidence(tmp_path):
    run(tmp_path)
    (tmp_path / "app.txt").write_text("app ok\nmore\n")
    graph, ws = graph_of(tmp_path), workspace(tmp_path)
    executor = Executor(graph, ws, ScriptedAgent(tmp_path))
    _, statuses = executor.assess()
    assert {s: statuses[s].status for s in statuses} == {
        "master": Status.STALE,
        "lib": Status.SATISFIED,
        "app": Status.STALE,
    }
    report = executor.execute("master")
    assert report.satisfied
    assert [report.outcomes[s].action for s in ("lib", "app", "master")] == [Action.SKIP, Action.VERIFY, Action.VERIFY]


def test_settle_re_verifies_specs_broken_later_in_the_run(tmp_path):
    vandal = {"lib": GOOD["lib"], "app": chain(GOOD["app"], writes("lib.txt", "broken\n"))}
    report, _ = run(tmp_path, ScriptedAgent(tmp_path, vandal))
    assert not report.satisfied
    assert report.outcomes["lib"].status is Status.FAILED
    assert report.outcomes["lib"].message == "broken by changes made later in this run"
    assert report.outcomes["app"].attempts[0].out_of_scope == ["lib.txt"]


def test_missing_external_tools_block_the_spec(tmp_path):
    extra = {"lib.spec.md": SPECS["lib.spec.md"] + "@depends tool:rsde-no-such-tool\n"}
    report, agent = run(tmp_path, extra=extra)
    assert report.outcomes["lib"].status is Status.BLOCKED
    assert "rsde-no-such-tool" in report.outcomes["lib"].message and agent.tasks == []


def test_a_crashing_adapter_does_not_take_the_run_down(tmp_path):
    class Crashing(ScriptedAgent):
        def run(self, task):
            raise RuntimeError("boom")

    report, _ = run(tmp_path, Crashing(tmp_path))
    attempt = report.outcomes["lib"].attempts[0]
    assert not attempt.completed and "raised RuntimeError: boom" in attempt.summary
    assert report.outcomes["lib"].status is Status.FAILED and report.aborted is None


def test_deferred_manual_agent_stops_the_run(tmp_path):
    report, _ = run(tmp_path, ManualAdapter({"interactive": False}, workspace=tmp_path))
    assert report.aborted and "Implement it" in report.aborted


def test_executing_a_subtree_only_touches_its_scope(tmp_path):
    report, agent = run(tmp_path, target="app")
    assert report.satisfied and report.scope == ["lib", "app"] and agent.spec_ids == ["lib", "app"]
    assert "master" not in report.outcomes


def test_planning_has_no_side_effects(tmp_path):
    graph, ws = setup_workspace(tmp_path)
    plan = Executor(graph, ws, ScriptedAgent(tmp_path)).plan("master")
    assert [s.spec_id for s in plan.steps] == ["lib", "app", "master"]
    assert not (tmp_path / ".rsde").exists()


def test_invalid_graphs_cannot_be_executed(tmp_path):
    write_files(tmp_path, {"master.md": "@requires nothing.provides.this\n"})
    with pytest.raises(ValueError, match="rsde check"):
        Executor(graph_of(tmp_path), workspace(tmp_path), ScriptedAgent(tmp_path))


# --------------------------------------------------------------------------- reconcile


def test_survey_reports_structural_drift(tmp_path):
    graph, _ = setup_workspace(
        tmp_path,
        {
            "lib.txt": "lib ok\n",
            "stray.py": "",
            "README.md": "",
            "rsde.toml": "",
            "extra.spec.md": "# Orphan\n",
            "lib.spec.md": SPECS["lib.spec.md"] + "@implement docs/\n",
        },
    )
    drift = {(d.kind, d.spec_id): d for d in survey(graph, graph.files)}
    assert drift[("missing-files", "app")].paths == ("app.txt",)
    assert drift[("missing-files", "lib")].paths == ("docs/",)
    assert drift[("unowned-files", None)].paths == ("stray.py",)  # Markdown and rsde.toml are never "code"


def test_reconcile_dry_run_audits_without_changing_anything(tmp_path):
    graph, ws = setup_workspace(tmp_path, {"lib.txt": "lib ok\n", "app.txt": "wrong\n"})
    agent = ScriptedAgent(tmp_path, GOOD)
    report = Reconciler(graph, ws, agent).run(dry_run=True)
    assert agent.tasks == [] and report.execution is None and not report.conformant
    assert [d.spec_id for d in report.before if d.kind == "failing-checks"] == ["app"]
    assert set(report.audit) == {"lib", "app", "master"}
    assert (tmp_path / "app.txt").read_text() == "wrong\n"


def test_reconcile_converges_and_only_fixes_what_drifted(tmp_path):
    graph, ws = setup_workspace(tmp_path, {"lib.txt": "lib ok\n", "app.txt": "wrong\n"})
    agent = ScriptedAgent(tmp_path, GOOD)
    report = Reconciler(graph, ws, agent).run()
    assert report.conformant and agent.spec_ids == ["app"]
    assert report.after == [] and sorted(report.conformant_specs) == ["app", "lib", "master"]


def test_reconcile_catches_drift_that_fingerprints_cannot_see(tmp_path):
    # lib's check reads a file it does not own, so editing that file leaves the
    # cached evidence "fresh" — only a full audit notices the drift.
    extra = {
        "lib.spec.md": SPECS["lib.spec.md"].replace('grep -q "lib ok" lib.txt', 'grep -q "lib ok" lib.txt && grep -q on flag.txt'),
        "flag.txt": "on\n",
    }
    run(tmp_path, extra=extra)
    (tmp_path / "flag.txt").write_text("off\n")
    graph, ws = graph_of(tmp_path), workspace(tmp_path)
    executor = Executor(graph, ws, ScriptedAgent(tmp_path))
    assert executor.assess()[1]["lib"].satisfied  # cached evidence still looks fine
    report = Reconciler(graph, ws, ScriptedAgent(tmp_path)).run(dry_run=True)
    assert [d.spec_id for d in report.before if d.kind == "failing-checks"] == ["lib"]
