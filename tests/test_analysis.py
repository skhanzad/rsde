"""Intent inheritance, ordering, scope, impact analysis and ownership."""

from helpers import graph_of, write_files
from rsde.graph import affected, execution_order, owners_of, resolve_intent, scope, specs_for_paths

APP = {
    "master.md": """
        # App
        @goal Ship the app.
        @constraint Standard library only.
        @invariant Never lose data.
        @spec lib.spec.md
        @spec app.spec.md
        @verify true
        """,
    "lib.spec.md": """
        # Lib
        @provides lib.api
        @implement lib/
        @verify true
        """,
    "app.spec.md": """
        # App shell
        @constraint Use argparse.
        @spec app/ui.spec.md
        @spec app/cmd.spec.md
        @implement app/main.py
        @verify true
        """,
    "app/ui.spec.md": """
        # UI
        @invariant Output is stable.
        @requires lib.api
        @implement app/ui.py
        @verify true
        """,
    "app/cmd.spec.md": """
        # Commands
        @provides cmd.api
        @implement app/cmd.py
        @verify true
        """,
}


def graph(tmp_path):
    write_files(tmp_path, APP)
    g = graph_of(tmp_path)
    assert g.errors == []
    return g


def test_intent_accumulates_down_the_hierarchy(tmp_path):
    intent = resolve_intent(graph(tmp_path), "app/ui")
    assert [f.spec_id for f in intent.frames] == ["master", "app", "app/ui"]
    assert [(origin, t.text) for origin, t in intent.constraints()] == [
        ("master", "Standard library only."),
        ("app", "Use argparse."),
    ]
    assert [(origin, t.text) for origin, t in intent.invariants()] == [
        ("master", "Never lose data."),
        ("app/ui", "Output is stable."),
    ]
    assert [f.spec_id for f in intent.inherited] == ["master", "app"]


def test_inherited_digest_changes_only_when_an_ancestor_changes_what_it_passes_down(tmp_path):
    before = resolve_intent(graph(tmp_path), "app/ui").inherited_digest()
    write_files(tmp_path, {"app/ui.spec.md": APP["app/ui.spec.md"] + "@constraint Own constraint.\n"})
    assert resolve_intent(graph_of(tmp_path), "app/ui").inherited_digest() == before
    write_files(tmp_path, {"app.spec.md": APP["app.spec.md"] + "@behavior Not inherited.\n"})
    assert resolve_intent(graph_of(tmp_path), "app/ui").inherited_digest() == before
    write_files(tmp_path, {"app.spec.md": APP["app.spec.md"] + "@constraint Inherited.\n"})
    assert resolve_intent(graph_of(tmp_path), "app/ui").inherited_digest() != before


def test_execution_order_puts_dependencies_and_children_first(tmp_path):
    order = execution_order(graph(tmp_path))
    assert order == ["lib", "app/ui", "app/cmd", "app", "master"]


def test_scope_is_the_subtree_plus_transitive_dependencies(tmp_path):
    g = graph(tmp_path)
    assert scope(g, "app/ui") == ["lib", "app/ui"]
    assert scope(g, "app") == ["lib", "app/ui", "app/cmd", "app"]
    assert scope(g, "lib") == ["lib"]


def test_affected_follows_dependencies_and_evidence_up(tmp_path):
    impacts = {i.spec_id: i for i in affected(graph(tmp_path), ["lib"])}
    assert list(impacts) == ["lib", "app/ui", "app", "master"]
    assert impacts["lib"].kind == "changed"
    assert impacts["app/ui"].kind == "dependency" and "requires `lib.api` from `lib`" in impacts["app/ui"].reasons
    assert impacts["app"].kind == "evidence"
    assert "app/cmd" not in impacts


def test_affected_flows_intent_down_to_descendants(tmp_path):
    impacts = {i.spec_id: i for i in affected(graph(tmp_path), ["app"])}
    assert set(impacts) == {"app", "app/ui", "app/cmd", "master"}
    assert impacts["app/cmd"].kind == "intent"
    assert impacts["master"].kind == "evidence"
    assert "lib" not in impacts


def test_ownership_maps_files_to_specs(tmp_path):
    g = graph(tmp_path)
    assert owners_of(g, "lib/core/x.py") == ["lib"]
    assert owners_of(g, "app/ui.py") == ["app/ui"]
    by_spec, unowned = specs_for_paths(g, ["app/ui.py", "app.spec.md", "README"])
    assert by_spec == {"app/ui": ["app/ui.py"], "app": ["app.spec.md"]}
    assert unowned == ["README"]
