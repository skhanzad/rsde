from rsde.syntax.lexer import scan


def values(text):
    return [(d.name, d.value) for d in scan(text).directives]


def test_directives_carry_name_value_and_position():
    scanned = scan("# Title\n@goal Do it\n   @verify make test\n")
    assert [(d.name, d.value, d.line, d.column) for d in scanned.directives] == [
        ("goal", "Do it", 2, 1),
        ("verify", "make test", 3, 4),
    ]
    assert scanned.title.text == "Title"


def test_directives_inside_list_items_and_with_colon():
    scanned = scan("- @behavior one\n* @goal: two\n1. @done three\n")
    assert [(d.name, d.value) for d in scanned.directives] == [("behavior", "one"), ("goal", "two"), ("done", "three")]
    assert scanned.directives[0].column == 3


def test_continuation_lines_must_be_indented_deeper():
    text = (
        "@behavior first line\n"
        "  second line\n"
        "prose after\n"
        "- @behavior a\n"
        "  more\n"
        "- @behavior b\n"
    )
    scanned = scan(text)
    assert [d.value for d in scanned.directives] == ["first line\nsecond line", "a\nmore", "b"]
    assert "prose after" in scanned.prose


def test_blank_line_ends_a_directive():
    assert values("@goal one\n\n  indented prose\n") == [("goal", "one")]


def test_code_fences_and_html_comments_are_opaque():
    text = "```md\n@goal not me\n```\n<!--\n@goal nor me\n-->\n~~~\n@goal tilde\n~~~\n<!-- @goal inline -->\n@goal yes\n"
    assert values(text) == [("goal", "yes")]


def test_fenced_block_becomes_the_value_of_an_empty_directive():
    scanned = scan("@verify\n```sh\nset -e\n  echo hi\n```\n@goal after\n")
    verify, goal = scanned.directives
    assert verify.value == "set -e\n  echo hi"
    assert verify.block and verify.block_info == "sh"
    assert goal.value == "after"


def test_escaped_at_is_literal_prose():
    scanned = scan("\\@goal literal\n- \\@spec also literal\n")
    assert scanned.directives == ()
    assert "@goal literal" in scanned.prose
    assert "- @spec also literal" in scanned.prose


def test_front_matter_is_skipped():
    assert values("---\nid: x\n@goal hidden\n---\n# T\n@goal yes\n") == [("goal", "yes")]


def test_unterminated_fence_is_reported():
    scanned = scan("text\n```\n@goal hidden\n")
    assert scanned.directives == ()
    assert [issue.line for issue in scanned.issues] == [2]


def test_title_falls_back_to_the_first_heading():
    assert scan("## Sub\ntext\n").title.text == "Sub"
    assert scan("no headings").title is None


def test_mentions_and_emails_are_not_directives():
    assert values("Contact me@example.com\n@example.com is not one\n@ alone\n") == []


def test_prose_excludes_title_and_directives_and_collapses_blank_lines():
    scanned = scan("# T\n\nIntro text.\n@goal g\n\n\n\nMore.\n")
    assert scanned.prose == "Intro text.\n\nMore."
