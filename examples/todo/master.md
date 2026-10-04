# Todo CLI

@goal A tiny, dependable command-line todo manager: add tasks, list them, mark them done and remove them.

@constraint Target Python 3.11+ and use only the standard library — no third-party packages.
@constraint Every module starts with a docstring that says what it is for.
@invariant The data file on disk is always valid JSON that `json.load` can read.
@invariant Task ids are positive integers and are never reused, even after a task is removed.

This is the root of the spec graph: it describes the complete desired state of
the repository. Each `@spec` below owns one part of the system. Intent (goals,
constraints, invariants) flows down to every child; verification evidence flows
back up, so this spec is satisfied only when every child is satisfied *and* the
end-to-end test below passes.

## Architecture

@spec specs/model.spec.md — the task domain model
@spec specs/storage.spec.md — persistence
@spec specs/cli.spec.md — the command-line interface

## Acceptance

@behavior `python3 -m todo add "Buy milk"` followed by `python3 -m todo list` shows the new task.
@behavior `python3 -m todo done 1` marks task 1 as completed, and `python3 -m todo list --all` shows it as `[x]`.
@behavior Referring to a task id that does not exist prints `error: ...` to stderr and exits with status 1.

@implement tests/test_end_to_end.py
@verify {python} -m unittest tests.test_end_to_end
@done The end-to-end test drives the real CLI, as a subprocess, through a full add → list → done cycle.
