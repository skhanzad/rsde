# RSDE — Recursive Spec-Driven Engineering

RSDE is a declarative language and CLI for building software from specifications.
A repository's complete desired state is written as a tree of linked Markdown files
rooted at `master.md`. RSDE parses them into a typed AST and a spec graph, validates
the graph like a compiler, and drives an interchangeable AI coding agent until every
spec is backed by **passing verification evidence**.

```
master.md                            the complete desired state
├── specs/model.spec.md              each spec owns one part of the system
├── specs/storage.spec.md
└── specs/cli.spec.md
    ├── specs/cli/commands.spec.md
    └── specs/cli/format.spec.md

    intent flows down ↓        ↑ evidence flows up
```

Specs are not prompts. Parsing, dependency resolution, validation, planning,
execution state, and verification are all deterministic. A spec is satisfied
only when its declared checks pass against the exact current inputs, and its
children and dependencies are satisfied too. What an agent says about its own
work never counts.

## Contents

- [Quick start](#quick-start)
- [The language](#the-language)
- [Semantics](#semantics)
- [Validation and diagnostics](#validation-and-diagnostics)
- [Command-line interface](#command-line-interface)
- [Execution lifecycle](#execution-lifecycle)
- [Reconciliation](#reconciliation)
- [Coding-agent adapters](#coding-agent-adapters)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [Development](#development)
- [Limitations and roadmap](#limitations-and-roadmap)

## Quick start

RSDE needs Python 3.11 or newer and has no runtime dependencies.

```sh
pip install -e .                      # from this repository; installs the `rsde` command
cp -r examples/todo /tmp/todo && cd /tmp/todo

rsde check                            # validate the whole spec graph
rsde graph                            # hierarchy + dependency graph
rsde execute --plan                   # what would run, in which order, and why
rsde execute --agent replay           # offline demo agent: implement + verify everything
rsde status                           # satisfaction status from recorded evidence
```

The `replay` agent copies each spec's files from a reference implementation, so
the full lifecycle runs offline and deterministically:

```
[1/6] specs/model — Task model  (never verified)
      ✗ rsde: declared implementation exists  missing: todo/__init__.py, todo/model.py, tests/test_model.py
      ✗ python3 -m unittest tests.test_model  exit 1 · 0.03s
      → agent replay attempt 1/2
        │ wrote todo/model.py …
      ✓ rsde: declared implementation exists  3 file(s) present
      ✓ python3 -m unittest tests.test_model  0.03s
      ✓ satisfied after 1 agent attempt
…
Result
✓ master — Todo CLI
├── ✓ specs/model — Task model
├── ✓ specs/storage — JSON task storage
└── ✓ specs/cli — Command-line interface
    ├── ✓ specs/cli/commands — Sub-commands
    └── ✓ specs/cli/format — Output formatting

✓ master is satisfied — 6/6 specs satisfied · 6 agent runs · 24 checks
```

To use a real coding agent, run `rsde execute --agent claude` or `--agent codex`
(see [Coding-agent adapters](#coding-agent-adapters)). To start your own project,
run `rsde init`.

## The language

### A spec file

A spec is an ordinary Markdown file (`master.md`, or any `*.md` / `*.spec.md`)
that renders well on GitHub and in Obsidian. Lines that start with `@` are
**directives**. Everything else is prose: it reaches the agent as notes but is
never parsed for meaning.

```markdown
# JSON task storage

@goal Persist tasks between runs in a single JSON file.
@requires todo.model
@provides todo.storage — `TaskStore`, a repository of tasks backed by one JSON file

@constraint The file location comes from `TODO_FILE`, defaulting to `todo.json`.

- @behavior `TaskStore(path).load()` returns an empty list when the file does not exist.
- @behavior `TaskStore.add(title)` assigns the next id — one greater than the highest id
  ever issued — saves, and returns the new task.

@invariant Saving is atomic: write a temporary file, then `os.replace` it into place.

@implement todo/storage.py, tests/test_storage.py
@verify python3 -m unittest tests.test_storage
@done Storage tests cover a missing file, id allocation after removal and atomic replacement.
```

### Directives

| Directive | Value | Meaning | Direction |
|---|---|---|---|
| `@goal` | text | Why the spec exists. | inherited ↓ (as context) |
| `@spec` | spec references | A child spec. This spec **owns** it. | hierarchy |
| `@depends` | spec references, external deps | This spec needs another spec, or an external tool, variable, package or service. | dependency graph |
| `@requires` | capabilities | This spec needs a capability provided by another spec. | dependency graph |
| `@provides` | capabilities | This spec is the single provider of a capability. | dependency graph |
| `@constraint` | text | A rule the implementation must obey (how). | inherited ↓ |
| `@invariant` | text | A property that must always hold (what). | inherited ↓ |
| `@behavior` | text | Observable behaviour to implement. | local |
| `@implement` | paths and globs | The files this spec owns. They define the agent's scope and the inputs tracked for change detection. | local |
| `@verify` | shell command or fenced block | A check: exit status 0 means pass. **The only source of evidence.** | evidence ↑ |
| `@done` | text | The definition of done, for agents and reviewers. It never satisfies anything by itself. | — |

### Syntax rules

- **A directive is a line starting with `@name`**, optionally inside a list item
  (`- @behavior …`) and optionally followed by a colon (`@goal: …`).
- **Continuation:** following lines indented deeper than the directive belong to it.
  A blank line ends it.
- **Block values:** a directive with no inline value takes the fenced code block that
  follows it, which is how you write multi-line checks:

  ````markdown
  @verify
  ```sh
  python3 -m unittest discover -s tests
  python3 -m todo --help > /dev/null
  ```
  ````

- **Opaque regions:** directives inside code fences and HTML comments are ignored, so
  documentation can show examples. YAML front matter is skipped.
- **Escaping:** `\@` at the start of a line is a literal `@`.
- **Strictness:** an unknown directive is an error, not prose. `@requries` gets
  *"did you mean `@requires`?"* instead of silently dropping a requirement.
- **Lists:** `@spec`, `@depends`, `@requires`, `@provides` and `@implement` take
  comma-separated lists. A single item may carry a description after ` — `, ` -- `
  or ` - `. Values may be wrapped in backticks.

### Spec references

`@spec` and `@depends` accept four forms of reference. All of them resolve
deterministically to one file in the workspace.

| Form | Example | Resolution |
|---|---|---|
| path | `specs/storage.spec.md`, `./cli/format.spec.md`, `/specs/model` | Relative to the referencing spec, then to the workspace root. A leading `/` means the root. Without `.md`, `.spec.md` and then `.md` are tried. |
| Markdown link | `[Storage](specs/storage.spec.md)` | Same as a path. The label documents the link, and `#anchors` are ignored. |
| wikilink | `[[storage]]`, `[[cli/format\|the formatter]]` | By spec id: an exact match, else a unique trailing match. Ambiguity is an error. |
| name | `storage` | Same as a wikilink. |

A **spec id** is the file's workspace-relative path without `.spec.md` or `.md`.
For example, `specs/cli/format.spec.md` has the id `specs/cli/format`.

### Capabilities

`@provides todo.storage` publishes a capability, and `@requires todo.storage`
consumes it. Names are identifiers made of letters, digits and `_`, separated by
`.`, `-`, `/` or `:`. Every capability has exactly one provider, and each
requirement becomes a dependency edge to that provider. Capabilities let specs
depend on what something does rather than where it is written, so a spec can
move without breaking its dependents.

### External dependencies

`@depends` also accepts things outside the spec graph:

| Kind | Example | Checked |
|---|---|---|
| `tool:` | `tool:python3`, `tool:node@>=20` | Must be on `PATH` (a warning in `check`, blocking in `execute`). |
| `env:` | `env:DATABASE_URL` | Must be set (a warning in `check`, blocking in `execute`). |
| `pkg:` | `pkg:pypi/requests@2.31` | Declarative: shown to the agent. |
| `service:` | `service:postgres` | Declarative: shown to the agent. |

### Grammar

```ebnf
spec        = { line } ;
directive   = [ indent ] [ list-marker ] "@" name [ ":" ] ( value { continuation } | NL fenced-block ) ;
name        = "spec" | "depends" | "goal" | "requires" | "provides" | "constraint"
            | "behavior" | "invariant" | "verify" | "implement" | "done" ;

ref-list    = ref { "," ref } [ description ] ;            (* @spec *)
dep-list    = ( ref | external ) { "," ( ref | external ) } [ description ] ;
ref         = wikilink | md-link | path | name-ref ;
wikilink    = "[[" target [ "#" anchor ] [ "|" alias ] "]]" ;
md-link     = "[" label "](" target ")" ;
external    = kind ":" ident [ "@" version ] ;              kind = "tool" | "env" | "pkg" | "service"
cap-list    = capability { "," capability } [ description ] ;
capability  = ident { ( "." | "-" | "/" | ":" ) ident } ;
path-list   = glob { "," glob } [ description ] ;           (* @implement *)
description = ( "—" | "–" | "--" | "-" ) text ;
```

## Semantics

**1. Hierarchy.** `@spec` links form a tree rooted at `master.md`. Every spec has
exactly one parent, because a part of the desired state has exactly one owner.
Shared functionality is expressed through the dependency graph, never by
including a spec twice.

**2. Intent flows down.** A spec's effective intent is the chain of frames from
the root to the spec. Constraints and invariants accumulate: a child must honour
its own and every ancestor's. Goals travel along as context. `rsde show <spec>`
displays the resolved chain.

**3. The dependency graph is separate.** `@requires` (resolved through
`@provides`) and `@depends` create edges between specs anywhere in the tree.
The hierarchy says who owns what; the dependency graph says what must be
satisfied first.

**4. Satisfaction order.** A spec is processed after its dependencies and after
its children. The union of both relations must be acyclic. A descendant that
depends on its own ancestor is therefore a cycle, because the ancestor can only
be satisfied after the descendant.

**5. Satisfaction.** A spec is *satisfied* if and only if:

1. it declares at least one check (`@verify` or `@implement`) or has at least one child;
2. its own checks passed against its **current fingerprint**;
3. every child is satisfied; and
4. every spec it depends on is satisfied.

Its own checks are a built-in *implementation check* (every `@implement` path or
glob matches at least one file) plus every `@verify` command. Evidence flows up
because condition 3 is recursive: `master.md` is satisfied only when the whole
tree is.

**6. Fingerprints.** Evidence is bound to the fingerprint it was produced for,
which is a Merkle hash over the graph:

```
fp(S) = H( text of S,
           intent S inherits from its ancestors,
           contents of the files S owns,
           fp(c) for each child c,
           fp(d) for each dependency d )
```

Any change invalidates exactly the evidence it could affect. Editing a file
invalidates its owner, the owner's ancestors, and the specs that depend on any
of them. Changing a parent's constraint invalidates its whole subtree. Editing
an unowned file or a sibling invalidates nothing. Unaffected specs are never
re-run, so `execute` is incremental, like `make`.

**Statuses.** A spec is in exactly one of these states:

| Status | Meaning |
|---|---|
| ✓ `satisfied` | Fresh passing evidence, and every child and dependency is satisfied. |
| ✗ `failed` | Fresh evidence shows a failing check. |
| ↻ `stale` | Evidence exists, but the inputs changed since it was recorded. |
| ○ `pending` | Never verified. |
| ⊘ `blocked` | A dependency, or an external tool or variable, is not satisfied. |
| ◐ `incomplete` | Its own checks pass, but a child is not satisfied. |
| ? `unverifiable` | No checks and no children, so it can never be satisfied. |

## Validation and diagnostics

`rsde check` validates the complete graph without changing anything. Every
problem is reported with a stable code, the exact source span, and a hint:

```
error[E301]: no spec provides capability `auth.session`
 --> specs/api.spec.md:5:1
  |
5 | @requires auth.session
  | ^^^^^^^^^^^^^^^^^^^^^^ missing dependency
  |
  = help: add `@provides auth.session` to the spec that implements it, or remove this requirement

error[E303]: dependency cycle: `specs/api` → `specs/db` → `specs/api`
 --> specs/api.spec.md:4:1
  |
4 | @requires db.users
  | ^^^^^^^^^^^^^^^^^^ part of a cycle
  |
  = note: `specs/api` requires `db.users`, provided by `specs/db` (specs/api.spec.md:4:1)
  = note: `specs/db` requires `http.api`, provided by `specs/api` (specs/db.spec.md:5:1)
  = help: break the cycle: move the shared functionality into a new spec that both sides can depend on
```

Run it in [`examples/broken`](examples/broken) to see every major diagnostic.
Errors make the graph invalid, and `execute` refuses to run until they are
fixed. Warnings are advisory; `check --strict` turns them into failures.

| Code | Meaning |
|---|---|
| **E101** | Unknown directive (with a "did you mean" suggestion). |
| **E102** | Directive without a value. |
| **E103** | Invalid capability name. |
| **E104** | Malformed spec reference. |
| **E105** | Malformed external dependency, or an external dependency used with `@spec`. |
| **E106** | Invalid implementation path: absolute, outside the workspace, or claiming everything. |
| **E200** | Root spec not found. |
| **E201** | Referenced spec not found (lists the paths tried and similar names). |
| **E202** | Ambiguous reference. |
| **E203** | Spec included by more than one parent. |
| **E204** | Hierarchy cycle or self-inclusion. |
| **E205** | Detached spec: a dependency that no parent includes with `@spec`. |
| **E206** | Unreadable spec file. |
| **E207** | Two files map to the same spec id. |
| **E301** | Missing dependency: no spec provides a required capability. |
| **E302** | Duplicate provider. |
| **E303** | Dependency cycle, including descendant-on-ancestor dependencies. Every independent cycle is reported. |
| W101 | Duplicate directive. |
| W102 | Unterminated code fence or comment. |
| W201 | Orphan `*.spec.md` file, unreachable from the root. |
| W301 | Spec requires its own capability. |
| W302 | External tool or environment variable unavailable. |
| W401 | Spec has no `@goal`. |
| W402 | Spec cannot be verified (no `@verify` and no children). |
| W403 | Verified leaf owns no files, so code changes are invisible to it. |
| W404 | File claimed by more than one spec. |

## Command-line interface

| Command | What it does |
|---|---|
| `rsde check [ROOT] [--strict]` | Validates the complete spec graph. Read-only. |
| `rsde graph [--format text\|mermaid\|dot\|json]` | Shows the hierarchy and the dependency graph, annotated with status when evidence exists. |
| `rsde show SPEC [--prompt]` | Shows one spec: inherited intent, interfaces, files, checks and evidence. `--prompt` prints the exact agent task. |
| `rsde status [SPEC]` | Shows satisfaction from recorded evidence and current fingerprints. Runs nothing. |
| `rsde execute [SPEC] [--agent NAME]` | Resolves, plans, implements and verifies `SPEC` (default: the root) and everything it needs. |
| `rsde execute --plan` | Prints the plan only. No side effects. |
| `rsde execute --verify-only` / `--force` | Never invokes the agent (CI) / ignores fresh evidence. |
| `rsde affected SPEC_OR_FILE… [--since REV]` | Lists the specs a change may impact, in re-verification order. |
| `rsde reconcile [--dry-run] [--strict]` | Audits the repository against the specs, reports drift, then converges. |
| `rsde agents` | Lists the agent adapters and whether each is available. |
| `rsde init [DIR]` | Creates `master.md`, `rsde.toml` and a `.gitignore` entry. |

`SPEC` accepts a path (`specs/storage.spec.md`), an id (`specs/storage`), a
wikilink (`[[storage]]`) or a unique name (`storage`). Most commands accept
`--format json`, `-C DIR` and `--color auto|always|never`. Execution options:
`--max-attempts N`, `--verify-timeout SEC`, `--agent-timeout SEC`,
`--strict-scope`, and `-q` to hide the agent's output.

Exit codes are `0` for success, `1` when specs are invalid or not satisfied,
`2` for usage, workspace or configuration errors, and `130` when interrupted.

`rsde affected` accepts specs or implementation files (`rsde affected todo/format.py`
maps to the owning spec) and explains every impact:

```
6 specs may be affected (in re-verification order)
  specs/model         changed    changed
  specs/storage       dependency requires `todo.model` from `specs/model`
  specs/cli/commands  dependency requires `todo.model` from `specs/model`; requires `todo.storage` from `specs/storage`
  specs/cli/format    dependency depends on `specs/model`
  specs/cli           dependency requires `todo.storage` from `specs/storage`; contains `specs/cli/commands`; …
  master              evidence   contains `specs/model`; contains `specs/storage`; contains `specs/cli`
```

## Execution lifecycle

```
 rsde execute <spec>
   │
   ├─ 1. load      parse every reachable spec into the typed AST
   ├─ 2. resolve   build the hierarchy and dependency graph; any error aborts here
   ├─ 3. scope     the target + its descendants + their transitive dependencies
   ├─ 4. assess    fingerprints + recorded evidence → status of every spec
   ├─ 5. plan      topological order (dependencies and children first);
   │               per spec: skip (fresh) · verify · roll up · unverifiable
   ├─ 6. run       for each step, in order:
   │                 blocked?    a dependency, tool or env var is unsatisfied → skip
   │                 incomplete? a child is unsatisfied → skip
   │                 pre-check   run its checks; all pass → satisfied, no agent needed
   │                 implement   compile a task prompt → agent adapter → snapshot diff
   │                 re-verify   run the checks again; retry with the failures as feedback
   │               evidence is written to .rsde/state.json after every step
   ├─ 7. settle    re-verify specs whose inputs changed later in the run
   └─ 8. propagate recompute every status from evidence; exit 0 iff the target is satisfied
```

- **What is unsatisfied** is decided by the evidence (fingerprints) and the
  pre-check, never by asking the agent. A spec whose code already works costs one
  check run and no agent call.
- **The task prompt** is compiled from the graph, not pasted from the spec. It
  contains the scope, the goal and the ancestors' goals, behaviors, every
  constraint and invariant (marked with where it was inherited from), the
  interfaces (providers and the files that implement them), children to
  integrate, the definition of done, the verification commands, and, on a retry,
  the exact failing output. The same inputs always produce the same prompt.
  `rsde show SPEC --prompt` prints it.
- **Scope guard.** The workspace is snapshotted before and after every agent run.
  Changes outside the spec's `@implement` paths are recorded as scope violations
  and fail the spec under `--strict-scope`.
- **Integrity guard.** If an agent modifies any spec file or `rsde.toml`, the run
  aborts: specs are the source of truth and only humans change them.
- **Settling.** If a later agent breaks a spec that was already verified,
  fingerprints expose it and the spec is re-verified in the same run.
- **Audit trail.** `.rsde/runs/<run-id>/` keeps the plan, every prompt, every agent
  transcript, every check log and a JSON report.

## Reconciliation

`execute` is incremental and trusts fresh evidence. `reconcile` trusts nothing:
it compares the repository with the spec graph, then makes the implementation
conform, much like `terraform plan` followed by `apply`.

1. **Survey** structural drift: declared files that do not exist, files claimed
   by several specs, and files no spec owns. Markdown and repository metadata
   are never treated as code.
2. **Audit:** run every spec's checks bottom-up, ignoring cached evidence. This
   catches drift that fingerprints cannot see, such as a toolchain upgrade, an
   environment change, or an edit to a file a check reads but no spec owns.
3. **Report** the drift (`--dry-run` stops here).
4. **Converge:** execute the root. Only drifted specs reach the agent, starting
   from the failures the audit recorded.

Reconcile never deletes code. Unowned files are reported so a human can adopt
them into a spec (`@implement`) or remove them. `--strict` makes them fail the
command.

## Coding-agent adapters

The executor talks to agents through one small contract. The built-in adapters
are:

| Agent | What it runs |
|---|---|
| `none` | Nothing. Verify-only mode, and the default, so RSDE never edits code unless you choose an agent. |
| `manual` | You. RSDE writes the task file, waits for Enter, then verifies. |
| `claude` | Claude Code in headless mode: `claude -p {prompt} --permission-mode acceptEdits --output-format stream-json --verbose`. |
| `codex` | OpenAI Codex: `codex exec --sandbox workspace-write --skip-git-repo-check -`, with the prompt on stdin. |
| `command` | Any CLI agent you configure. |
| `replay` | An offline, deterministic agent that copies each spec's files from a reference tree. Used for demos and tests. |

Select one with `--agent NAME` or `[execute].agent` in `rsde.toml`. `rsde agents`
shows which are available.

### Claude Code

```sh
rsde execute --agent claude
```

The adapter streams Claude Code's progress live (tool calls, edits and the
final cost) and pre-approves exactly the spec's own simple `@verify` commands
with `--allowedTools "Bash(<cmd>)" "Bash(<cmd> *)"`. The agent can run the
checks that define success and nothing else. If `rsde` itself runs inside a
Claude Code session, the agent still starts as an independent session. Extra
flags are appended with `args`:

```toml
[agents.claude]
args = ["--model", "sonnet", "--max-turns", "30"]
# allow_verify_commands = false   # do not pre-approve the @verify commands
# command = [...]                 # or replace the whole command line
```

### Codex

```sh
rsde execute --agent codex
```

```toml
[agents.codex]
args = ["--model", "o3"]
```

### Any command-line agent

Add an agent to `rsde.toml` with `type = "command"`:

```toml
[agents.aider]
type = "command"
command = ["aider", "--yes-always", "--message-file", "{prompt_file}"]
timeout = 1200
env = { AIDER_AUTO_COMMITS = "false" }
```

| Placeholder | Value |
|---|---|
| `{prompt}` | The full task prompt (or a pointer to the prompt file when it is too long for one argument). |
| `{prompt_file}` | Path of the task written to `.rsde/runs/<run>/`. |
| `{spec_id}`, `{spec_file}` | The spec being implemented. |
| `{workspace}`, `{attempt}` | Absolute workspace root, and the attempt number (1-based). |

If no placeholder mentions the prompt, it is sent on stdin. The agent runs from
the workspace root with `RSDE_SPEC_ID`, `RSDE_SPEC_FILE`, `RSDE_PROMPT_FILE`,
`RSDE_ATTEMPT` and `RSDE_WORKSPACE` in its environment.

### Writing an adapter in Python

Subclass `AgentAdapter` and implement `run`:

```python
# my_agents/http_agent.py
from rsde.agents import AgentAdapter, AgentResult, AgentTask


class HttpAgent(AgentAdapter):
    """Sends the task to an internal agent service."""

    description = "our in-house agent service"

    def unavailable_reason(self) -> str | None:
        return None if self.options.get("url") else "set [agents.http].url in rsde.toml"

    def run(self, task: AgentTask) -> AgentResult:
        # task.prompt          compiled task (Markdown)
        # task.scope           @implement patterns the agent may touch
        # task.verify_commands the checks that will decide success
        # task.failures        failing CheckResults from the previous attempt
        # task.workspace       where to make the changes
        # task.on_output(line) stream progress to the RSDE console
        ...  # call your service, apply its edits under task.workspace
        return AgentResult(completed=True, summary="applied 3 edits")
```

Register it in one of three ways:

```toml
# 1. In rsde.toml (the workspace root is importable)
[agents.http]
type = "python"
class = "my_agents.http_agent:HttpAgent"
url = "https://agents.internal/run"     # every other key arrives in self.options
```

```sh
# 2. Directly on the command line
rsde execute --agent my_agents.http_agent:HttpAgent
```

```toml
# 3. As an installable plugin (pyproject.toml of your package)
[project.entry-points."rsde.agents"]
http = "my_agents.http_agent:HttpAgent"
```

**The contract.** An adapter changes files in `task.workspace` and reports only
whether it ran (`completed`, `summary`, `output`). RSDE decides the outcome by
re-running the checks. It also enforces the scope and integrity guards, retries
with failure feedback up to `max_attempts`, and records the transcript.
Exceptions raised by an adapter count as a failed attempt and do not crash the
run. Set `deferred=True` to stop the run gracefully, for example when a human
must act.

## Configuration

Everything is optional; without `rsde.toml`, `master.md` in the current directory
or a parent is the root.

```toml
[project]
root = "master.md"          # the root spec
ignore = ["vendor/**"]      # never implementation; hidden from scope checks and surveys
state_dir = ".rsde"         # evidence and run logs (add it to .gitignore)

[execute]
agent = "none"              # default adapter for execute / reconcile
max_attempts = 3            # agent attempts per spec
verify_timeout = 600        # seconds per @verify command
agent_timeout = 1800        # seconds per agent invocation
strict_scope = false        # fail specs whose agent edits files outside @implement

[agents.NAME]               # options for one adapter (see above)
```

Unknown keys are errors with suggestions, such as *"unknown key `max_atempts` in
[execute] (did you mean `max_attempts`?)"*.

## Architecture

RSDE is a pipeline of small, deterministic stages. Each stage is a package with
one responsibility:

| Module | Responsibility |
|---|---|
| `rsde.syntax` | **Parsing.** `lexer` scans Markdown into raw directives (fences, comments, continuations, escapes). `parser` interprets them into the typed, immutable `SpecDocument` AST (`ast`). `references` is the grammar for references, externals and lists. |
| `rsde.graph` | **Graph construction.** `builder` discovers specs recursively from the root, resolves references, and validates the hierarchy, capabilities and cycles. `model` holds `Hierarchy` and `DependencyGraph` as separate structures. `intent` resolves inheritance. `analysis` computes order, scope, affected specs and ownership. |
| `rsde.planning` | **Planning.** `fingerprint` computes the Merkle fingerprints, `status` holds the satisfaction semantics, and `planner` builds the ordered plan with a reason for every step. |
| `rsde.agents` | **Agent execution.** `base` defines the adapter contract, `prompt` compiles the deterministic task, and `builtin`, `command` and `replay` provide the adapters. The package `__init__` is the registry. |
| `rsde.verification` | **Verification.** Runs the built-in implementation check and `@verify` commands, and turns exit codes into `CheckResult` evidence. |
| `rsde.repository` | **Repository state.** `files` handles gitignore-aware listing, ownership globs, hashing and snapshots. `config` loads `rsde.toml` and discovers the workspace. `state` is the atomic `.rsde/state.json` evidence store. |
| `rsde.engine` | **Orchestration.** `executor` runs the lifecycle above. `reconcile` runs the survey, audit and converge steps. |
| `rsde.cli` | **CLI output.** `main` holds the commands and argument parsing, `render` produces diagnostics, trees, Mermaid/DOT/JSON, plans and reports, `progress` streams live execution, and `style` handles colour and glyphs. |
| `rsde.diagnostics` | Source-located diagnostics with stable codes. |
| `rsde.process` | Subprocesses with process-tree timeouts, shared by checks and agents. |

```
Markdown files ─▶ lexer ─▶ parser ─▶ SpecDocument (AST)
                                       │
                         builder: resolve + validate
                                       ▼
                 SpecGraph = Hierarchy ⊕ DependencyGraph (+ diagnostics)
                                       │
       files + state ─▶ fingerprints ─▶ statuses ─▶ plan
                                       │
                 executor ⇄ verifier (evidence) ⇄ agent adapter (changes)
                                       ▼
                             .rsde/state.json + run logs
```

Two examples ship with the repository. [`examples/todo`](examples/todo) is a
three-level spec graph for a todo CLI, with capabilities, a wikilink dependency,
inherited constraints, an end-to-end check at the root, and a reference
implementation for the `replay` agent. [`examples/broken`](examples/broken)
contains deliberate mistakes that showcase the diagnostics.

## Development

```sh
pip install -e ".[dev]"
pytest                      # 191 tests, a few seconds, no network
mypy src                    # the codebase is type-clean
```

The tests cover every diagnostic code, fingerprint invalidation, satisfaction
semantics, the executor lifecycle (ordering, blocking, retries, the scope and
integrity guards, settling), reconciliation, every CLI command, the adapters
(including stream-json parsing against a fake `claude`), and both examples end
to end.

## Limitations and roadmap

- **Sequential execution.** Independent specs could run in parallel; the plan
  already exposes each step's prerequisites.
- **POSIX shell.** `@verify` commands run with `/bin/sh`.
- **Agent-written tests.** An agent that writes its own tests can write weak ones.
  Keep acceptance tests out of the implementing agent's reach: put them in an
  ancestor's `@implement` and its `@verify`, which descendants' agents cannot
  touch (enforced by `--strict-scope`), or write the `@verify` commands yourself.
- **Content-addressed evidence.** A check that reads files no spec owns can go
  stale invisibly. `W403` flags verified specs that own no files, and
  `rsde reconcile` audits without trusting the cache.
- **Next steps:** parallel execution, versioned capabilities, cross-repository
  specs, a language server for editors, and evidence signing for CI.
