# Example: a todo CLI, specified with RSDE

This directory is an RSDE workspace that contains **only specifications**. The
implementation (`todo/`, `tests/`) does not exist yet: `rsde execute` builds it.

```
master.md                      Todo CLI: constraints and invariants for everything, end-to-end check
├── specs/model.spec.md        provides todo.model    — the Task value
├── specs/storage.spec.md      provides todo.storage  — JSON persistence (requires todo.model)
└── specs/cli.spec.md          provides todo.cli      — argparse wiring (requires todo.storage)
    ├── specs/cli/commands.spec.md   provides todo.commands (requires todo.storage, todo.model)
    └── specs/cli/format.spec.md     provides todo.format   (depends on [[model]])
```

It exercises every part of the language: nested `@spec` links in path form,
capabilities, a wikilink `@depends`, constraints
and invariants that every descendant inherits, `@implement` ownership, a
`@verify` command per spec, and an end-to-end check at the root that is
satisfied only after every child is.

## Run it

Work on a copy so the repository stays clean:

```sh
python -c "import shutil; shutil.copytree('examples/todo', 'todo-demo')"
cd todo-demo

rsde check                      # the graph is valid
rsde graph                      # hierarchy and dependencies
rsde show specs/cli/format.spec.md          # inherited intent, interfaces, scope
rsde show specs/cli/format.spec.md --prompt # the exact task an agent would get
rsde execute --plan             # 6 specs, dependencies and children first
```

### Offline, deterministic: the `replay` agent

`.reference/` holds a reference implementation. The `replay` agent "implements"
a spec by copying the files its `@implement` patterns claim, so you can watch
the whole lifecycle (pre-check fails → agent → re-verify → evidence flows up)
without a model:

```sh
rsde execute --agent replay
rsde status                     # everything satisfied
```

Then see what fingerprints do:

```sh
echo "# tweak" >> todo/format.py
rsde status                     # format, cli and master are stale; nothing else
rsde affected todo/format.py    # why
rsde execute                    # re-verifies only those three (no agent needed)
```

### A real coding agent

Delete the generated files (or start from a fresh copy) and run:

```sh
rsde execute --agent claude     # Claude Code
rsde execute --agent codex      # OpenAI Codex
```

`rsde.toml` sets `[project].ignore = [".reference/**"]`, so the reference tree is
never treated as part of this project's implementation.
