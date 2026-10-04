import time

from helpers import PYTHON
from rsde.diagnostics import SourceSpan
from rsde.process import tail
from rsde.syntax.ast import ImplementTarget, SpecDocument, VerifyCommand
from rsde.verification import IMPLEMENTATION_CHECK, Verifier

SPAN = SourceSpan("a.spec.md", 1)


def spec(*commands, implement=()):
    return SpecDocument(
        id="a",
        path="a.spec.md",
        title="A",
        title_span=SPAN,
        verify=tuple(VerifyCommand(c, SPAN) for c in commands),
        implement=tuple(ImplementTarget(p, SPAN) for p in implement),
    )


def test_passing_and_failing_commands(tmp_path):
    results = Verifier(tmp_path).verify(spec("echo hello", "echo oops >&2; exit 3"), [])
    assert [(r.passed, r.exit_code) for r in results] == [(True, 0), (False, 3)]
    assert results[0].output == "hello" and results[1].output == "oops"


def test_commands_run_in_the_workspace_with_rsde_variables(tmp_path):
    (tmp_path / "marker.txt").write_text("x")
    result = Verifier(tmp_path).run_command(spec(), 'test -f marker.txt && test "$RSDE_SPEC_ID" = a')
    assert result.passed


def test_multi_line_scripts(tmp_path):
    result = Verifier(tmp_path).run_command(spec(), "set -e\necho one\nfalse\necho never")
    assert not result.passed and result.output == "one"


def test_timeouts_kill_the_process_tree(tmp_path):
    started = time.monotonic()
    result = Verifier(tmp_path, timeout=0.5).run_command(spec(), f'{PYTHON} -c "import time; time.sleep(30)"')
    assert result.timed_out and not result.passed and result.exit_code is None
    assert time.monotonic() - started < 10
    assert "timed out" in result.output


def test_unknown_commands_fail_cleanly(tmp_path):
    result = Verifier(tmp_path).run_command(spec(), "rsde-definitely-not-a-command")
    assert not result.passed and result.exit_code == 127


def test_implementation_check(tmp_path):
    verifier = Verifier(tmp_path)
    doc = spec(implement=("src/", "README.txt"))
    missing = verifier.implementation_check(doc, ["src/a.py"])
    assert missing.builtin and not missing.passed and missing.output == "missing: README.txt"
    present = verifier.implementation_check(doc, ["src/a.py", "src/b.py", "README.txt"])
    assert present.passed and present.output == "3 file(s) present" and present.command == IMPLEMENTATION_CHECK
    assert verifier.implementation_check(spec("true"), []) is None


def test_every_check_runs_and_logs_are_written(tmp_path):
    seen = []
    results = Verifier(tmp_path).verify(
        spec("exit 1", "echo second", implement=("x.txt",)),
        [],
        on_result=seen.append,
        log_dir=tmp_path / "logs",
        label="pre-check",
    )
    assert [r.passed for r in results] == [False, False, True]
    assert seen == results
    assert (tmp_path / "logs" / "a.pre-check.check-2.log").read_text() == "second\n"


def test_tail_keeps_the_end():
    text = "\n".join(str(i) for i in range(200))
    assert tail(text, lines=3) == "197\n198\n199"
    assert tail("x" * 100, chars=10) == "…" + "x" * 10
