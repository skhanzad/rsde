# Command-line interface

@goal Expose the todo operations as a friendly `python3 -m todo` command.
@requires todo.storage
@provides todo.cli — `main(argv)`, the program entry point

@constraint Parse arguments with `argparse`.
@invariant Exit status is 0 on success, 1 when a task id does not exist, and 2 on usage errors.

The CLI is a thin shell: what each sub-command *does* and how tasks are
*printed* are owned by the two child specs below. This spec owns only the
argument parsing and the wiring between them.

@spec cli/commands.spec.md — what each sub-command does
@spec cli/format.spec.md — how tasks are printed

@behavior `todo add <title>`, `todo list [--all]`, `todo done <id>` and `todo remove <id>` are available.
@behavior Errors are printed to stderr as `error: <message>`.

@implement todo/cli.py, todo/__main__.py, tests/test_cli.py
@verify {python} -m unittest tests.test_cli
@done Every sub-command is reachable through `main(argv)` and returns the documented exit status.
