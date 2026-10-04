import pytest

from helpers import codes, graph_of, write_files
from rsde.graph import SpecSelectorError, build_graph


def test_hierarchy_follows_declaration_order(tmp_path):
    write_files(
        tmp_path,
        {
            "master.md": "# M\n@spec a.spec.md\n@spec b/b.spec.md\n",
            "a.spec.md": "# A\n@spec a/sub.spec.md\n",
            "a/sub.spec.md": "# Sub\n",
            "b/b.spec.md": "# B\n",
        },
    )
    graph = graph_of(tmp_path)
    assert graph.errors == []
    assert list(graph.specs) == ["master", "a", "a/sub", "b/b"]
    hierarchy = graph.hierarchy
    assert hierarchy.children("master") == ("a", "b/b")
    assert hierarchy.parent("a/sub") == "a" and hierarchy.parent("master") is None
    assert hierarchy.lineage("a/sub") == ["master", "a", "a/sub"]
    assert hierarchy.ancestors("a/sub") == ["a", "master"]
    assert hierarchy.descendants("master") == ["a", "a/sub", "b/b"]
    assert hierarchy.postorder() == ["a/sub", "a", "b/b", "master"]
    assert hierarchy.depth("a/sub") == 2 and hierarchy.is_leaf("b/b")


def test_paths_resolve_relative_to_the_spec_then_to_the_root(tmp_path):
    write_files(
        tmp_path,
        {
            "master.md": "@spec specs/a.spec.md\n",
            "specs/a.spec.md": "@spec b.spec.md\n@spec specs/c\n@spec /specs/d.spec.md\n",
            "specs/b.spec.md": "",
            "specs/c.spec.md": "",
            "specs/d.spec.md": "",
        },
    )
    graph = graph_of(tmp_path)
    assert graph.errors == []
    assert graph.hierarchy.children("specs/a") == ("specs/b", "specs/c", "specs/d")


def test_wikilinks_names_and_markdown_links(tmp_path):
    write_files(
        tmp_path,
        {
            "master.md": "@spec [[storage]]\n@spec cli\n@spec [The API](specs/api.spec.md)\n@spec specs/web\n",
            "specs/storage.spec.md": "",
            "specs/cli.md": "",
            "specs/api.spec.md": "",
            "specs/web.spec.md": "",
        },
    )
    graph = graph_of(tmp_path)
    assert graph.errors == []
    assert graph.hierarchy.children("master") == ("specs/storage", "specs/cli", "specs/api", "specs/web")


def test_ambiguous_wikilink(tmp_path):
    write_files(
        tmp_path,
        {
            "master.md": "@spec [[model]]\n@spec [[x/model]]\n",
            "x/model.spec.md": "",
            "y/model.spec.md": "",
        },
    )
    graph = graph_of(tmp_path)
    assert [c for c in codes(graph) if c in ("E202", "W201")] == ["E202", "W201"]  # y/ is also an orphan
    ambiguous = next(d for d in graph.diagnostics if d.code == "E202")
    assert "x/model.spec.md" in ambiguous.message and "y/model.spec.md" in ambiguous.message
    assert graph.hierarchy.children("master") == ("x/model",)


def test_missing_reference_suggests_a_close_name(tmp_path):
    write_files(tmp_path, {"master.md": "@spec [[storag]]\n@spec storage.spec.md\n", "storage.spec.md": ""})
    graph = graph_of(tmp_path)
    diag = next(d for d in graph.diagnostics if d.code == "E201")
    assert "`[[storage]]`" in diag.help
    assert diag.span.line == 1


def test_missing_path_lists_the_locations_tried(tmp_path):
    write_files(tmp_path, {"master.md": "@spec specs/missing\n"})
    diag = graph_of(tmp_path).errors[0]
    assert diag.code == "E201"
    assert "specs/missing.spec.md" in diag.notes[0] and "specs/missing.md" in diag.notes[0]


def test_missing_root_spec(tmp_path):
    graph = build_graph(tmp_path)
    assert codes(graph) == ["E200"] and graph.specs == {}


def test_custom_root_spec(tmp_path):
    write_files(tmp_path, {"spec/root.md": "# Root\n@spec child.spec.md\n", "spec/child.spec.md": ""})
    graph = graph_of(tmp_path, root_spec="spec/root.md")
    assert graph.root_id == "spec/root" and graph.hierarchy.children("spec/root") == ("spec/child",)


def test_selectors_accept_paths_ids_wikilinks_and_names(tmp_path):
    write_files(tmp_path, {"master.md": "@spec a.spec.md\n", "a.spec.md": "@spec a/sub.spec.md\n", "a/sub.spec.md": ""})
    graph = graph_of(tmp_path)
    for selector in ("a/sub.spec.md", "a/sub", "[[sub]]", "sub"):
        assert graph.resolve(selector, tmp_path) == "a/sub"
    assert graph.resolve("sub.spec.md", tmp_path / "a") == "a/sub"
    with pytest.raises(SpecSelectorError, match="did you mean"):
        graph.resolve("su", tmp_path)


def test_dependency_edges_from_capabilities_and_depends(tmp_path):
    write_files(
        tmp_path,
        {
            "master.md": "@spec a.spec.md\n@spec b.spec.md\n@spec c.spec.md\n",
            "a.spec.md": "@provides cap.a — the a capability\n",
            "b.spec.md": "@requires cap.a\n@provides cap.b\n",
            "c.spec.md": "@requires cap.b\n@depends [[a]]\n",
        },
    )
    deps = graph_of(tmp_path).dependencies
    assert [(e.source, e.target, e.kind, e.capability) for e in deps.edges] == [
        ("b", "a", "requires", "cap.a"),
        ("c", "b", "requires", "cap.b"),
        ("c", "a", "depends", None),
    ]
    assert deps.dependencies("c") == ["b", "a"]
    assert deps.dependents("a") == ["b", "c"]
    assert deps.provider("cap.b") == "b" and deps.consumers("cap.a") == ["b"]
