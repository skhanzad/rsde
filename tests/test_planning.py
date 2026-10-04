"""Fingerprints, satisfaction semantics and planning."""

from helpers import graph_of, write_files
from rsde.planning import Action, OwnState, Status, build_plan, compute_fingerprints, evaluate
from rsde.repository.files import FileHasher, list_workspace_files
from rsde.repository.state import CheckResult, Evidence

SPECS = {
    "master.md": """
        # Root
        @goal g
        @constraint Be careful.
        @spec a.spec.md
        @spec b.spec.md
        @verify true
        """,
    "a.spec.md": """
        # A
        @provides a.cap
        @implement a.txt
        @verify true
        """,
    "b.spec.md": """
        # B
        @requires a.cap
        @implement b.txt
        @verify true
        """,
    "a.txt": "a",
    "b.txt": "b",
    "unrelated.txt": "u",
}


def fingerprints(root):
    graph = graph_of(root)
    return compute_fingerprints(graph, list_workspace_files(root), FileHasher(root))


def changed(before, after):
    return sorted(s for s in before if before[s].value != after[s].value)


def prepare(tmp_path):
    write_files(tmp_path, SPECS)
    return fingerprints(tmp_path)


def test_fingerprints_are_deterministic(tmp_path):
    assert {k: v.value for k, v in prepare(tmp_path).items()} == {k: v.value for k, v in fingerprints(tmp_path).items()}


def test_owned_file_change_invalidates_owner_dependents_and_ancestors(tmp_path):
    before = prepare(tmp_path)
    (tmp_path / "a.txt").write_text("a changed")
    assert changed(before, fingerprints(tmp_path)) == ["a", "b", "master"]


def test_change_without_dependents_only_bubbles_up(tmp_path):
    before = prepare(tmp_path)
    (tmp_path / "b.txt").write_text("b changed")
    assert changed(before, fingerprints(tmp_path)) == ["b", "master"]


def test_unowned_files_do_not_change_fingerprints(tmp_path):
    before = prepare(tmp_path)
    (tmp_path / "unrelated.txt").write_text("u changed")
    assert changed(before, fingerprints(tmp_path)) == []


def test_non_inherited_parent_change_stays_local(tmp_path):
    before = prepare(tmp_path)
    write_files(tmp_path, {"master.md": SPECS["master.md"] + "@behavior Something new.\n"})
    assert changed(before, fingerprints(tmp_path)) == ["master"]


def test_inherited_parent_change_flows_down(tmp_path):
    before = prepare(tmp_path)
    write_files(tmp_path, {"master.md": SPECS["master.md"] + "@constraint Be even more careful.\n"})
    assert changed(before, fingerprints(tmp_path)) == ["a", "b", "master"]


def evidence(spec_id, fp, passed=True, builtin_failed=False):
    checks = [CheckResult("true", passed, 0 if passed else 1)]
    if builtin_failed:
        checks = [CheckResult("rsde: declared implementation exists", False, 1, builtin=True)]
    return Evidence(spec_id, fp, passed and not builtin_failed, checks, "2026-01-01T00:00:00Z")


def statuses(tmp_path, records):
    graph = graph_of(tmp_path)
    fps = compute_fingerprints(graph, list_workspace_files(tmp_path), FileHasher(tmp_path))
    built = {}
    for spec_id, kind in records.items():
        fp = fps[spec_id].value if kind != "stale" else "old"
        built[spec_id] = evidence(spec_id, fp, passed=kind != "fail", builtin_failed=kind == "missing")
    return graph, fps, evaluate(graph, fps, built)


def test_no_evidence_means_pending_and_dependents_are_blocked(tmp_path):
    write_files(tmp_path, SPECS)
    _, _, st = statuses(tmp_path, {})
    assert st["a"].status is Status.PENDING and st["a"].own is OwnState.UNVERIFIED
    assert st["b"].status is Status.BLOCKED and st["b"].blockers == ("a",)
    assert st["master"].status is Status.PENDING


def test_fresh_passing_evidence_everywhere_satisfies_the_root(tmp_path):
    write_files(tmp_path, SPECS)
    _, _, st = statuses(tmp_path, {"a": "pass", "b": "pass", "master": "pass"})
    assert all(s.satisfied for s in st.values())


def test_own_checks_passing_is_not_enough_when_a_child_is_not_satisfied(tmp_path):
    write_files(tmp_path, SPECS)
    _, _, st = statuses(tmp_path, {"a": "pass", "b": "fail", "master": "pass"})
    assert st["b"].status is Status.FAILED and "`true` failed" in st["b"].reason
    assert st["master"].status is Status.INCOMPLETE and st["master"].blockers == ("b",)


def test_evidence_for_an_old_fingerprint_is_stale(tmp_path):
    write_files(tmp_path, SPECS)
    _, _, st = statuses(tmp_path, {"a": "stale"})
    assert st["a"].status is Status.STALE and st["a"].own is OwnState.STALE


def test_missing_implementation_reason(tmp_path):
    write_files(tmp_path, SPECS)
    _, _, st = statuses(tmp_path, {"a": "missing"})
    assert st["a"].reason == "declared implementation missing"


def test_leaf_without_checks_is_unverifiable_and_blocks_its_parent(tmp_path):
    write_files(tmp_path, {"master.md": "@spec a.spec.md\n", "a.spec.md": "@goal nothing to check\n"})
    _, _, st = statuses(tmp_path, {})
    assert st["a"].status is Status.UNVERIFIABLE
    assert st["master"].status is Status.INCOMPLETE  # no own checks: it rolls up its children


def test_parent_without_own_checks_is_satisfied_by_its_children(tmp_path):
    write_files(tmp_path, {"master.md": "@spec a.spec.md\n", "a.spec.md": "@verify true\n"})
    _, _, st = statuses(tmp_path, {"a": "pass"})
    assert st["master"].status is Status.SATISFIED and st["master"].own is OwnState.NO_CHECKS


def test_plan_actions(tmp_path):
    write_files(tmp_path, SPECS)
    graph, _, st = statuses(tmp_path, {"a": "pass", "b": "stale"})
    plan = build_plan(graph, "master", st)
    assert [(s.spec_id, s.action, s.reason) for s in plan.steps] == [
        ("a", Action.SKIP, "fresh passing evidence"),
        ("b", Action.VERIFY, "inputs changed since last verification"),
        ("master", Action.VERIFY, "never verified"),
    ]
    assert plan.steps[2].prerequisites == ("a", "b")
    assert [s.spec_id for s in plan.work] == ["b", "master"]


def test_plan_force_verify_only_rollup_and_unverifiable(tmp_path):
    write_files(
        tmp_path,
        {"master.md": "@spec a.spec.md\n@spec empty.spec.md\n", "a.spec.md": "@verify true\n", "empty.spec.md": ""},
    )
    graph, _, st = statuses(tmp_path, {"a": "pass"})
    forced = build_plan(graph, "master", st, force=True, verify_only=True)
    assert [(s.spec_id, s.action) for s in forced.steps] == [
        ("a", Action.CHECK),
        ("empty", Action.UNVERIFIABLE),
        ("master", Action.ROLLUP),
    ]
    assert forced.steps[0].reason == "re-verification forced"


def test_plan_for_a_subtree_includes_its_dependencies(tmp_path):
    write_files(tmp_path, SPECS)
    graph, _, st = statuses(tmp_path, {})
    assert [s.spec_id for s in build_plan(graph, "b", st).steps] == ["a", "b"]
