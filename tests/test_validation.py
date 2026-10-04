"""Every structural rule of the spec graph, one diagnostic code at a time."""

from helpers import codes, graph_of, write_files
from rsde.graph import build_graph

VALID = {
    "master.md": """
        # Root
        @goal Everything.
        @spec a.spec.md
        @spec b.spec.md
        """,
    "a.spec.md": """
        # A
        @goal Part a.
        @provides cap.a
        @implement a.txt
        @verify true
        """,
    "b.spec.md": """
        # B
        @goal Part b.
        @requires cap.a
        @implement b.txt
        @verify true
        """,
}


def build(tmp_path, files, **kwargs):
    write_files(tmp_path, files)
    return graph_of(tmp_path, **kwargs)


def test_a_well_formed_graph_has_no_diagnostics(tmp_path):
    graph = build(tmp_path, VALID)
    assert graph.diagnostics == [] and graph.ok


def test_spec_included_by_two_parents(tmp_path):
    graph = build(
        tmp_path,
        {
            "master.md": "@spec a.spec.md\n@spec b.spec.md\n",
            "a.spec.md": "@spec shared.spec.md\n",
            "b.spec.md": "@spec shared.spec.md\n",
            "shared.spec.md": "",
        },
    )
    diag = next(d for d in graph.errors)
    assert diag.code == "E203" and diag.span.path == "b.spec.md"
    assert diag.related[0].span.path == "a.spec.md"
    assert graph.hierarchy.parent("shared") == "a"


def test_spec_included_twice_by_the_same_parent(tmp_path):
    graph = build(tmp_path, {"master.md": "@spec a.spec.md\n@spec [[a]]\n", "a.spec.md": ""})
    assert codes(graph).count("E203") == 1
    assert "again" in graph.errors[0].message


def test_hierarchy_cycles_and_self_inclusion(tmp_path):
    graph = build(
        tmp_path,
        {
            "master.md": "@spec a.spec.md\n",
            "a.spec.md": "@spec b.spec.md\n@spec a.spec.md\n",
            "b.spec.md": "@spec a.spec.md\n@spec master.md\n",
        },
    )
    messages = sorted(d.message for d in graph.errors if d.code == "E204")
    assert len(messages) == 3
    assert any("includes itself" in m for m in messages)
    assert any("`master` ⊃ `a` ⊃ `b` ⊃ `master`" in m for m in messages)


def test_missing_provider_suggests_the_closest_capability(tmp_path):
    files = dict(VALID, **{"b.spec.md": "@requires cap.aa\n@implement b.txt\n@verify true\n"})
    graph = build(tmp_path, files)
    diag = next(d for d in graph.errors)
    assert diag.code == "E301" and "`cap.a`" in diag.help


def test_duplicate_providers(tmp_path):
    files = dict(VALID, **{"b.spec.md": "@provides cap.a\n@implement b.txt\n@verify true\n"})
    graph = build(tmp_path, files)
    diag = next(d for d in graph.errors)
    assert diag.code == "E302"
    assert diag.span.path == "b.spec.md" and diag.related[0].span.path == "a.spec.md"


def test_dependency_cycle_through_capabilities(tmp_path):
    graph = build(
        tmp_path,
        {
            "master.md": "@spec a.spec.md\n@spec b.spec.md\n",
            "a.spec.md": "@provides x\n@requires y\n",
            "b.spec.md": "@provides y\n@requires x\n",
        },
    )
    cycles = [d for d in graph.errors if d.code == "E303"]
    assert len(cycles) == 1
    assert cycles[0].message == "dependency cycle: `a` → `b` → `a`"
    assert len(cycles[0].notes) == 2


def test_dependency_cycle_through_depends(tmp_path):
    graph = build(
        tmp_path,
        {"master.md": "@spec a.spec.md\n@spec b.spec.md\n", "a.spec.md": "@depends [[b]]\n", "b.spec.md": "@depends [[a]]\n"},
    )
    assert codes(graph).count("E303") == 1


def test_self_dependency_is_a_cycle(tmp_path):
    graph = build(tmp_path, {"master.md": "@spec a.spec.md\n", "a.spec.md": "@depends [[a]]\n"})
    assert codes(graph).count("E303") == 1


def test_descendant_depending_on_its_ancestor(tmp_path):
    graph = build(
        tmp_path,
        {
            "master.md": "@spec parent.spec.md\n",
            "parent.spec.md": "@provides p.cap\n@spec child.spec.md\n",
            "child.spec.md": "@requires p.cap\n",
        },
    )
    diag = next(d for d in graph.errors if d.code == "E303")
    assert diag.message == "`child` depends on its ancestor `parent`"
    assert "sibling" in diag.help


def test_indirect_cycle_through_the_hierarchy(tmp_path):
    # x needs `p.cap` from p, but p is only satisfied after its child c, which needs x.
    graph = build(
        tmp_path,
        {
            "master.md": "@spec p.spec.md\n@spec x.spec.md\n",
            "p.spec.md": "@provides p.cap\n@spec c.spec.md\n",
            "c.spec.md": "@requires x.cap\n",
            "x.spec.md": "@provides x.cap\n@requires p.cap\n",
        },
    )
    diag = next(d for d in graph.errors if d.code == "E303")
    assert "`p` → `c` → `x` → `p`" in diag.message
    assert any("contains" in note for note in diag.notes)


def test_every_independent_cycle_is_reported(tmp_path):
    graph = build(
        tmp_path,
        {
            "master.md": "@spec a.spec.md\n@spec b.spec.md\n@spec c.spec.md\n",
            "a.spec.md": "@provides a\n@requires b\n@spec a/sub.spec.md\n",
            "a/sub.spec.md": "@requires a\n",
            "b.spec.md": "@provides b\n@requires c\n",
            "c.spec.md": "@provides c\n@requires a\n",
        },
    )
    messages = sorted(d.message for d in graph.errors if d.code == "E303")
    assert messages == [
        "`a/sub` depends on its ancestor `a`",
        "dependency cycle: `a` → `b` → `c` → `a`",
    ]


def test_requiring_your_own_capability_is_a_warning(tmp_path):
    graph = build(tmp_path, {"master.md": "@spec a.spec.md\n", "a.spec.md": "@provides x\n@requires x\n"})
    assert "W301" in codes(graph) and graph.ok


def test_dependency_outside_the_hierarchy_is_detached(tmp_path):
    graph = build(
        tmp_path,
        {"master.md": "@spec a.spec.md\n", "a.spec.md": "@depends other.spec.md\n", "other.spec.md": ""},
    )
    assert "E205" in codes(graph)
    assert "W201" not in codes(graph)  # reported once, as detached


def test_orphan_spec_files(tmp_path):
    graph = build(tmp_path, {"master.md": "# M\n", "orphan.spec.md": "", "notes.md": ""})
    orphans = [d for d in graph.diagnostics if d.code == "W201"]
    assert [d.span.path for d in orphans] == ["orphan.spec.md"]


def test_conflicting_spec_ids(tmp_path):
    graph = build(tmp_path, {"master.md": "@spec a.md\n@spec a.spec.md\n", "a.md": "", "a.spec.md": ""})
    assert "E207" in codes(graph)


def test_completeness_warnings(tmp_path):
    graph = build(
        tmp_path,
        {
            "master.md": "# M\n@goal g\n@spec nogoal.spec.md\n@spec noverify.spec.md\n@spec onlyfiles.spec.md\n"
            "@spec nofiles.spec.md\n@spec overlap.spec.md\n",
            "nogoal.spec.md": "@implement n.txt\n@verify true\n",
            "noverify.spec.md": "@goal g\n",
            "onlyfiles.spec.md": "@goal g\n@implement o.txt\n",
            "nofiles.spec.md": "@goal g\n@verify true\n",
            "overlap.spec.md": "@goal g\n@implement n.txt\n@verify true\n",
        },
    )
    found = {(d.code, d.message.split("`")[1]) for d in graph.warnings}
    assert ("W401", "nogoal") in found
    assert ("W402", "noverify") in found and ("W402", "onlyfiles") in found
    assert ("W403", "nofiles") in found
    assert ("W404", "nogoal") in found
    messages = {d.message for d in graph.warnings}
    assert "`noverify` can never be satisfied: it has no @verify and no child specs" in messages


def test_external_dependencies_are_probed(tmp_path, monkeypatch):
    monkeypatch.delenv("RSDE_TEST_UNSET", raising=False)
    monkeypatch.setenv("RSDE_TEST_SET", "1")
    write_files(
        tmp_path,
        {
            "master.md": "@spec a.spec.md\n",
            "a.spec.md": "@depends tool:rsde-no-such-tool, env:RSDE_TEST_UNSET, env:RSDE_TEST_SET, pkg:anything\n",
        },
    )
    probed = build_graph(tmp_path)
    assert [d.code for d in probed.diagnostics if d.code == "W302"] == ["W302", "W302"]
    assert "W302" not in codes(graph_of(tmp_path))  # probing can be disabled


def test_syntax_errors_in_the_root_do_not_hide_the_rest_of_the_graph(tmp_path):
    graph = build(tmp_path, {"master.md": "@requries x\n@spec a.spec.md\n", "a.spec.md": "@goal fine\n@verify true\n"})
    assert "E101" in codes(graph)
    assert list(graph.specs) == ["master", "a"]
