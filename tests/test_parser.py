import textwrap

import pytest

from rsde.syntax import DirectiveKind, RefStyle, content_hash, parse_document, spec_id_for


def parse(text, path="specs/x.spec.md"):
    return parse_document(textwrap.dedent(text).lstrip("\n"), path)


def only(diags):
    assert len(diags) == 1, [d.message for d in diags]
    return diags[0]


def test_parses_every_directive_into_typed_nodes():
    doc, diags = parse(
        """
        # Storage
        @goal Persist tasks.
        @requires todo.model
        @provides todo.storage — JSON repository
        @constraint Standard library only.
        @behavior Loading a missing file returns no tasks.
        @invariant Writes are atomic.
        @spec child.spec.md
        @depends [[model]], tool:git@2.40
        @implement todo/storage.py, tests/
        @verify python -m unittest
        @done Tests pass.

        Some notes.
        """
    )
    assert diags == []
    assert (doc.id, doc.path, doc.title) == ("specs/x", "specs/x.spec.md", "Storage")
    assert doc.goal == "Persist tasks."
    assert [c.name for c in doc.requires] == ["todo.model"]
    assert (doc.provides[0].name, doc.provides[0].description) == ("todo.storage", "JSON repository")
    assert [t.text for t in doc.constraints] == ["Standard library only."]
    assert [t.text for t in doc.behaviors] == ["Loading a missing file returns no tasks."]
    assert [t.text for t in doc.invariants] == ["Writes are atomic."]
    assert (doc.children[0].target, doc.children[0].style) == ("child.spec.md", RefStyle.PATH)
    assert (doc.depends[0].target, doc.depends[0].style) == ("model", RefStyle.WIKILINK)
    assert str(doc.externals[0]) == "tool:git@2.40" and doc.externals[0].probed
    assert doc.implement_patterns == ("todo/storage.py", "tests/")
    assert doc.verify_commands == ("python -m unittest",)
    assert [t.text for t in doc.done] == ["Tests pass."]
    assert doc.notes == "Some notes."
    assert doc.directives[0].kind is DirectiveKind.GOAL
    assert doc.requires[0].span.line == 3


@pytest.mark.parametrize(
    "path,expected",
    [("master.md", "master"), ("a/b.spec.md", "a/b"), ("a/b.md", "a/b"), ("notes.txt", "notes.txt")],
)
def test_spec_ids_are_paths_without_spec_suffixes(path, expected):
    assert spec_id_for(path) == expected


def test_title_falls_back_to_the_file_name():
    doc, _ = parse("@goal x\n", path="specs/user-store.spec.md")
    assert doc.title == "User store"


def test_unknown_directive_suggests_the_closest_one():
    diag = only(parse("@requries a.b\n")[1])
    assert diag.code == "E101" and diag.is_error
    assert "did you mean `@requires`?" in diag.help
    assert (diag.span.line, diag.span.column) == (1, 1)


def test_unknown_directive_without_a_close_match_lists_known_directives():
    assert "known directives:" in only(parse("@frobnicate x\n")[1]).help


def test_empty_directive_is_an_error_with_an_example():
    diag = only(parse("@goal\n")[1])
    assert diag.code == "E102"
    assert "@goal" in diag.help


def test_capability_names_must_be_identifiers():
    diag = only(parse("@requires a database connection\n")[1])
    assert diag.code == "E103"
    assert "put prose after a dash" in diag.help


def test_capability_lists_accept_commas_and_backticks():
    doc, diags = parse("@requires `a.b`, c-d, e/f:g\n")
    assert diags == []
    assert [c.name for c in doc.requires] == ["a.b", "c-d", "e/f:g"]


def test_descriptions_only_attach_to_a_single_capability():
    doc, _ = parse("@provides a, b — shared description\n")
    assert [(c.name, c.description) for c in doc.provides] == [("a", ""), ("b", "")]


def test_duplicate_directive_is_a_warning_pointing_at_the_first():
    diag = only(parse("@goal same\n@goal  same\n")[1])
    assert diag.code == "W101" and not diag.is_error
    assert diag.related[0].span.line == 1


def test_external_dependencies_are_not_children():
    diag = only(parse("@spec tool:git\n")[1])
    assert diag.code == "E105"
    assert "@depends tool:git" in diag.help


def test_unknown_external_kind_suggests_a_fix():
    diag = only(parse("@depends tol:git\n")[1])
    assert diag.code == "E105"
    assert "did you mean `tool:`" in diag.help


def test_remote_references_are_rejected():
    assert only(parse("@spec [x](https://example.com/a.md)\n")[1]).code == "E104"


@pytest.mark.parametrize("value", ["a b", "[[a", "a,", "[x](", ", a"])
def test_malformed_reference_lists(value):
    assert only(parse(f"@spec {value}\n")[1]).code == "E104"


def test_markdown_link_references_drop_anchors_and_keep_labels():
    doc, diags = parse("@spec [Storage](specs/storage.spec.md#top)\n")
    assert diags == []
    ref = doc.children[0]
    assert (ref.target, ref.label, ref.style) == ("specs/storage.spec.md", "Storage", RefStyle.MARKDOWN)


def test_reference_description_becomes_the_label():
    doc, _ = parse("@spec storage.spec.md — persistence\n")
    assert doc.children[0].label == "persistence"


def test_bare_names_are_name_references():
    doc, _ = parse("@depends storage, [[cli|the CLI]]\n")
    assert [(r.target, r.style, r.label) for r in doc.depends] == [
        ("storage", RefStyle.NAME, ""),
        ("cli", RefStyle.WIKILINK, "the CLI"),
    ]


@pytest.mark.parametrize(
    "value,message",
    [
        ("/etc/passwd", "absolute path"),
        ("../outside.py", "outside the workspace"),
        (".", "entire workspace"),
        ("**", "entire workspace"),
    ],
)
def test_implementation_paths_are_validated(value, message):
    diag = only(parse(f"@implement {value}\n")[1])
    assert diag.code == "E106"
    assert message in diag.message


def test_implementation_paths_are_normalised():
    doc, diags = parse("@implement ./src//a.py, `lib/`, pkg\\mod.py\n")
    assert diags == []
    assert doc.implement_patterns == ("src/a.py", "lib/", "pkg/mod.py")


def test_verify_continuation_lines_fold_into_one_command():
    doc, _ = parse("@verify python -m unittest \\\n  tests.test_x\n")
    assert doc.verify_commands == ("python -m unittest tests.test_x",)


def test_verify_blocks_keep_their_lines():
    doc, _ = parse("@verify\n```sh\nmake build\nmake test\n```\n")
    assert doc.verify_commands == ("make build\nmake test",)


def test_verify_strips_inline_code_markers():
    doc, _ = parse("@verify `make test`\n")
    assert doc.verify_commands == ("make test",)


def test_unterminated_fence_warns():
    assert only(parse("```\n@goal hidden\n")[1]).code == "W102"


def test_content_hash_ignores_trailing_whitespace_and_line_endings():
    assert content_hash("a  \r\nb\n\n") == content_hash("a\nb")
    assert content_hash("a\nb") != content_hash("a\nc")


def test_intent_digest_tracks_only_inherited_directives():
    base = parse("@goal g\n@constraint c\n@behavior one\n")[0]
    other_behavior = parse("@goal g\n@constraint c\n@behavior two\n")[0]
    other_constraint = parse("@goal g\n@constraint d\n@behavior one\n")[0]
    assert base.intent_digest() == other_behavior.intent_digest()
    assert base.intent_digest() != other_constraint.intent_digest()
