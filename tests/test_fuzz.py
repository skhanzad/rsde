"""The front end never crashes: arbitrary input yields a document and diagnostics."""

import random

from helpers import graph_of, write_files
from rsde.syntax import parse_document

FRAGMENTS = [
    "@goal x", "@spec ", "@spec [[a]]", "@spec [b](c.md", "@depends tool:", "@depends env:X, [[",
    "@requires a.b,", "@provides , ", "@implement ../x", "@implement *", "@verify", "@verify `", "@done",
    "@unknown y", "\\@escaped", "- @behavior z", "  continued", "\t@constraint tab", "1) @invariant n",
    "```", "~~~", "```sh", "<!--", "-->", "---", "# Title", "## Sub", "", " ", "@", "@@", "@-x", "[[", "]]",
    "— description", "@spec a, b — d", "@requires a b", "@goal: colon", "\r", "é ünïcödé",
]


def random_document(rng: random.Random) -> str:
    lines = []
    for _ in range(rng.randint(0, 25)):
        line = " ".join(rng.choice(FRAGMENTS) for _ in range(rng.randint(1, 3)))
        lines.append(" " * rng.choice((0, 0, 0, 2, 4)) + line)
    return "\n".join(lines)


def test_parser_never_crashes_and_spans_stay_in_bounds():
    rng = random.Random(20261003)
    for _ in range(1500):
        text = random_document(rng)
        doc, diags = parse_document(text, "fuzz.spec.md")
        line_count = max(len(text.splitlines()), 1)
        for diag in diags:
            assert diag.span is not None and 1 <= diag.span.line <= line_count, (text, diag)
        for directive in doc.directives:
            assert 1 <= directive.span.line <= line_count


def test_graph_builder_never_crashes_on_random_spec_trees(tmp_path):
    rng = random.Random(7)
    names = ["master.md", "a.spec.md", "b.spec.md", "sub/c.spec.md", "sub/d.md"]
    for round_ in range(60):
        root = tmp_path / f"ws{round_}"
        files = {}
        for name in names:
            refs = [f"@spec {rng.choice(names)}" for _ in range(rng.randint(0, 2))]
            refs += [f"@depends [[{rng.choice(['a', 'b', 'c', 'd', 'master'])}]]" for _ in range(rng.randint(0, 1))]
            caps = [f"@provides cap.{rng.randint(0, 3)}", f"@requires cap.{rng.randint(0, 3)}"]
            files[name] = "\n".join(refs + rng.sample(caps, rng.randint(0, 2)) + ["@verify true"])
        write_files(root, files)
        graph = graph_of(root)
        assert graph.root_id == "master"
        if graph.ok:  # a valid graph must have a consistent hierarchy
            assert set(graph.hierarchy.preorder()) == set(graph.specs)
