"""The ``rsde`` command-line interface.

Exit codes: 0 success · 1 the specs are invalid or not satisfied ·
2 usage, workspace or configuration error · 130 interrupted.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence

from rsde import __version__
from rsde.agents import AgentAdapter, AgentConfigError, available_agents, create_adapter, render_task_prompt
from rsde.cli.progress import CliEvents
from rsde.cli.render import (
    graph_to_dict,
    render_affected,
    render_check_summary,
    render_diagnostics,
    render_dot,
    render_execution_summary,
    render_graph_text,
    render_mermaid,
    render_plan,
    render_reconcile,
    render_show,
    render_status,
    to_json,
)
from rsde.cli.style import Style
from rsde.engine.executor import ExecuteOptions, Executor
from rsde.engine.reconcile import Reconciler
from rsde.graph import SpecGraph, SpecSelectorError, affected, build_graph, owners_of, specs_for_paths
from rsde.planning.fingerprint import Fingerprint, Fingerprinter
from rsde.planning.status import SpecStatus, evaluate
from rsde.repository.config import (
    CONFIG_FILE,
    DEFAULT_ROOT_SPEC,
    ConfigError,
    Workspace,
    WorkspaceError,
    load_config,
    open_workspace,
)
from rsde.repository.state import CheckResult, StateStore

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_INTERRUPTED = 0, 1, 2, 130


class UsageError(Exception):
    """A problem with how rsde was invoked (exit code 2)."""


class Context:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.cwd = Path.cwd()
        color = getattr(args, "color", "auto") or "auto"
        self.style = Style.for_stream(sys.stdout, color)
        self.err_style = Style.for_stream(sys.stderr, color)

    def out(self, text: str = "") -> None:
        print(text, flush=True)

    def err(self, text: str) -> None:
        print(text, file=sys.stderr, flush=True)

    def warn(self, text: str) -> None:
        self.err(self.err_style.yellow(self.err_style.bold("warning")) + f": {text}")

    def workspace(self, target: str | None = None) -> Workspace:
        explicit = getattr(self.args, "workspace", None)
        hint = Path(target) if target and Path(target).exists() else None
        try:
            return open_workspace(self.cwd, explicit, hint)
        except WorkspaceError:
            if hint is not None and hint.is_file():  # an explicit root spec with no config around it
                root = hint.resolve().parent
                return Workspace(root, load_config(root))
            raise

    def root_spec(self, ws: Workspace, given: str | None) -> str:
        if not given:
            return ws.root_spec
        rel = ws.relative(self.cwd / given)
        if rel is None:
            raise UsageError(f"`{given}` is outside the workspace {ws.root}")
        return rel

    def graph(self, ws: Workspace, root_spec: str | None = None) -> SpecGraph:
        return build_graph(ws.root, root_spec or ws.root_spec, ignore=ws.config.ignore)

    def valid_graph(self, ws: Workspace) -> SpecGraph | None:
        """Build the graph; on errors print them and return None."""
        graph = self.graph(ws)
        if graph.ok:
            if graph.warnings:
                self.err(self.err_style.dim(f"({len(graph.warnings)} spec warning(s); run `rsde check` for details)"))
            return graph
        self.err(render_diagnostics(graph.errors, graph.sources, self.err_style))
        self.err("")
        self.err(self.err_style.red(f"the spec graph has {len(graph.errors)} error(s); fix them first (see `rsde check`)"))
        return None

    def resolve(self, graph: SpecGraph, selector: str | None) -> str:
        if not selector:
            return graph.root_id
        try:
            return graph.resolve(selector, self.cwd)
        except SpecSelectorError as exc:
            raise UsageError(str(exc)) from exc


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

MASTER_TEMPLATE = """\
# {name}

@goal Describe, in one sentence, the complete desired state of this project.
@constraint List rules that every part of the project must follow.

This file is the root of the spec graph: it describes the whole project.
Split the work into child specs with `@spec`, give each child a `@goal`,
the files it owns (`@implement`) and the commands that prove it works
(`@verify`). Evidence flows up: this spec is satisfied only when every
child is.

<!-- Uncomment and adapt:
@spec specs/example.spec.md — the first part of the system
-->
"""

CONFIG_TEMPLATE = """\
# RSDE workspace configuration. See README for every option.

[project]
root = "master.md"
# Files that are never treated as implementation (globs):
ignore = []

[execute]
# Coding agent used by `rsde execute` / `rsde reconcile`:
#   none (verify only) · manual · claude · codex · replay · command · <your own>
agent = "none"
max_attempts = 3
verify_timeout = 600
agent_timeout = 1800
strict_scope = false

# [agents.claude]
# command = ["claude", "-p", "{prompt}", "--permission-mode", "acceptEdits"]
"""


def cmd_init(ctx: Context, args: argparse.Namespace) -> int:
    target = (ctx.cwd / (args.directory or ".")).resolve()
    target.mkdir(parents=True, exist_ok=True)
    files = {
        DEFAULT_ROOT_SPEC: MASTER_TEMPLATE.format(name=target.name.replace("-", " ").title() or "Project"),
        CONFIG_FILE: CONFIG_TEMPLATE,
    }
    existing = [name for name in files if (target / name).exists()]
    if existing and not args.force:
        ctx.err(f"refusing to overwrite {', '.join(existing)} in {target} (use --force)")
        return EXIT_FAIL
    for name, content in files.items():
        (target / name).write_text(content, encoding="utf-8")
    gitignore = target / ".gitignore"
    lines = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    if ".rsde/" not in lines and ".rsde" not in lines:
        with gitignore.open("a", encoding="utf-8") as fh:
            if lines and lines[-1].strip():
                fh.write("\n")
            fh.write("# RSDE execution state and run logs\n.rsde/\n")
    ctx.out(f"Initialised an RSDE workspace in {target}")
    ctx.out(f"  {DEFAULT_ROOT_SPEC}   the root spec — describe your project here")
    ctx.out(f"  {CONFIG_FILE}   workspace configuration")
    ctx.out("Next: write child specs, then run `rsde check`.")
    return EXIT_OK


def cmd_check(ctx: Context, args: argparse.Namespace) -> int:
    ws = ctx.workspace(args.root)
    graph = ctx.graph(ws, ctx.root_spec(ws, args.root))
    failing = bool(graph.errors) or (args.strict and bool(graph.warnings))
    if args.format == "json":
        ctx.out(
            to_json(
                {
                    "ok": not failing,
                    "root": graph.root_id,
                    "specs": len(graph.specs),
                    "dependency_edges": len(graph.dependencies.edges),
                    "errors": len(graph.errors),
                    "warnings": len(graph.warnings),
                    "diagnostics": [d.to_dict() for d in graph.diagnostics],
                }
            )
        )
        return EXIT_FAIL if failing else EXIT_OK
    if graph.diagnostics:
        ctx.out(render_diagnostics(graph.diagnostics, graph.sources, ctx.style))
        ctx.out()
    ctx.out(render_check_summary(graph, ctx.style, strict=args.strict))
    return EXIT_FAIL if failing else EXIT_OK


def _statuses(
    ws: Workspace, graph: SpecGraph
) -> tuple[StateStore, dict[str, Fingerprint], dict[str, SpecStatus]]:
    state = StateStore(ws.state_dir)
    fingerprints = Fingerprinter(graph, ws.config.ignore).compute(refresh=False)
    return state, fingerprints, evaluate(graph, fingerprints, state.evidence)


def cmd_graph(ctx: Context, args: argparse.Namespace) -> int:
    ws = ctx.workspace(args.root)
    graph = ctx.graph(ws, ctx.root_spec(ws, args.root))
    if not graph.specs:
        ctx.err(render_diagnostics(graph.errors, graph.sources, ctx.err_style))
        return EXIT_FAIL
    statuses = None
    if graph.ok and not args.no_status and (ws.state_dir / "state.json").exists():
        statuses = _statuses(ws, graph)[2]
    if args.format == "mermaid":
        ctx.out(render_mermaid(graph, statuses))
    elif args.format == "dot":
        ctx.out(render_dot(graph, statuses))
    elif args.format == "json":
        ctx.out(to_json(graph_to_dict(graph, statuses)))
    else:
        ctx.out(render_graph_text(graph, ctx.style, statuses))
    if graph.errors:
        ctx.err(ctx.err_style.yellow(f"note: the spec graph has {len(graph.errors)} error(s); run `rsde check`"))
    return EXIT_OK


def cmd_show(ctx: Context, args: argparse.Namespace) -> int:
    ws = ctx.workspace(args.spec)
    graph = ctx.graph(ws)
    spec_id = ctx.resolve(graph, args.spec)
    statuses = fingerprints = None
    evidence_checks: tuple[CheckResult, ...] = ()
    if graph.ok:
        _, fingerprints, statuses = _statuses(ws, graph)
        evidence = statuses[spec_id].evidence
        if evidence is not None and not evidence.passed:
            evidence_checks = tuple(evidence.checks)
    if args.prompt:
        prompt = render_task_prompt(graph, spec_id, max_attempts=ws.config.max_attempts, results=evidence_checks)
        ctx.out(prompt.rstrip())
        return EXIT_OK
    if args.format == "json":
        data = next(s for s in graph_to_dict(graph, statuses)["specs"] if s["id"] == spec_id)
        ctx.out(to_json(data))
        return EXIT_OK
    ctx.out(render_show(graph, spec_id, ctx.style, statuses=statuses, fingerprints=fingerprints, files=graph.files))
    return EXIT_OK


def cmd_status(ctx: Context, args: argparse.Namespace) -> int:
    ws = ctx.workspace(args.spec)
    graph = ctx.valid_graph(ws)
    if graph is None:
        return EXIT_FAIL
    target = ctx.resolve(graph, args.spec)
    state, fingerprints, statuses = _statuses(ws, graph)
    for warning in state.warnings:
        ctx.warn(warning)
    if args.format == "json":
        specs = {}
        for spec_id in graph.hierarchy.subtree(target):
            status = statuses[spec_id]
            specs[spec_id] = {
                "status": status.status.value,
                "reason": status.reason,
                "fingerprint": fingerprints[spec_id].value,
                "verified_at": status.evidence.verified_at if status.evidence is not None else None,
            }
        ctx.out(to_json({"target": target, "satisfied": statuses[target].satisfied, "specs": specs}))
        return EXIT_OK
    ctx.out(render_status(graph, statuses, ctx.style, target))
    return EXIT_OK


def _execute_options(ws: Workspace, args: argparse.Namespace) -> ExecuteOptions:
    config = ws.config
    return ExecuteOptions(
        max_attempts=args.max_attempts or config.max_attempts,
        verify_timeout=args.verify_timeout or config.verify_timeout,
        agent_timeout=args.agent_timeout or config.agent_timeout,
        force=getattr(args, "force", False),
        verify_only=getattr(args, "verify_only", False),
        strict_scope=args.strict_scope or config.strict_scope,
    )


def _adapter(ctx: Context, ws: Workspace, args: argparse.Namespace, *, needed: bool) -> AgentAdapter:
    name = args.agent or ws.config.agent
    try:
        adapter = create_adapter(name, ws.config, ws.root)
    except AgentConfigError as exc:
        raise UsageError(str(exc)) from exc
    if needed and adapter.implements:
        reason = adapter.unavailable_reason()
        if reason:
            raise UsageError(f"agent `{adapter.name}` is not available: {reason}")
    if needed and not adapter.implements and not getattr(args, "verify_only", False):
        ctx.err(ctx.err_style.dim("note: no coding agent selected, so this run only verifies "
                                  "(choose one with --agent claude|codex|manual|replay|… or [execute].agent)"))
    return adapter


def cmd_execute(ctx: Context, args: argparse.Namespace) -> int:
    ws = ctx.workspace(args.spec)
    graph = ctx.valid_graph(ws)
    if graph is None:
        return EXIT_FAIL
    target = ctx.resolve(graph, args.spec)
    adapter = _adapter(ctx, ws, args, needed=not args.plan)
    json_mode = args.format == "json"
    events = None if json_mode else CliEvents(graph, ctx.style, agent_output=not args.quiet)
    executor = Executor(graph, ws, adapter, options=_execute_options(ws, args), events=events)
    for warning in executor.state.warnings:
        ctx.warn(warning)
    if args.plan:
        plan = executor.plan(target)
        ctx.out(to_json(plan.to_dict()) if json_mode else render_plan(plan, ctx.style, adapter.name))
        return EXIT_OK
    report = executor.execute(target)
    if json_mode:
        ctx.out(to_json(report.to_dict()))
    else:
        ctx.out(render_execution_summary(report, graph, ctx.style))
    if report.aborted == "interrupted":
        return EXIT_INTERRUPTED
    return EXIT_OK if report.satisfied else EXIT_FAIL


def _git_changes(root: Path, revision: str) -> list[str]:
    def git(*argv: str) -> list[str]:
        try:
            proc = subprocess.run(["git", "-C", str(root), *argv], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise UsageError(f"cannot run git: {exc}") from exc
        if proc.returncode != 0:
            raise UsageError(f"git {' '.join(argv)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return [line for line in proc.stdout.splitlines() if line.strip()]

    changed = git("diff", "--name-only", "--relative", revision, "--")
    untracked = git("ls-files", "--others", "--exclude-standard")
    return sorted({*changed, *untracked})


def cmd_affected(ctx: Context, args: argparse.Namespace) -> int:
    if not args.targets and not args.since:
        raise UsageError("name at least one changed spec or file, or use --since <git-revision>")
    ws = ctx.workspace(args.targets[0] if args.targets else None)
    graph = ctx.valid_graph(ws)
    if graph is None:
        return EXIT_FAIL
    seeds: dict[str, list[str]] = {}
    unowned: list[str] = []
    for item in args.targets:
        try:
            seeds.setdefault(graph.resolve(item, ctx.cwd), [])
            continue
        except SpecSelectorError as exc:
            problem = str(exc)
        rel = ws.relative(ctx.cwd / item)
        owners = owners_of(graph, rel) if rel else []
        if not owners:
            raise UsageError(f"{problem}, and no spec owns a file at that path")
        for owner in owners:
            seeds.setdefault(owner, []).append(f"owns {rel}")
    if args.since:
        by_spec, unowned = specs_for_paths(graph, _git_changes(ws.root, args.since))
        for spec_id, paths in by_spec.items():
            seeds.setdefault(spec_id, []).extend(paths)
    impacts = affected(graph, list(seeds))
    if args.format == "json":
        ctx.out(
            to_json(
                {
                    "changed": seeds,
                    "unowned": unowned,
                    "affected": [
                        {"spec": i.spec_id, "kind": i.kind, "reasons": list(i.reasons), "distance": i.distance}
                        for i in impacts
                    ],
                }
            )
        )
        return EXIT_OK
    if not seeds:
        ctx.out("No spec is affected" + (f" ({len(unowned)} changed file(s) are not owned by any spec)" if unowned else "."))
        return EXIT_OK
    ctx.out(render_affected(impacts, ctx.style, seeds, unowned))
    return EXIT_OK


def cmd_reconcile(ctx: Context, args: argparse.Namespace) -> int:
    ws = ctx.workspace()
    graph = ctx.valid_graph(ws)
    if graph is None:
        return EXIT_FAIL
    adapter = _adapter(ctx, ws, args, needed=not args.dry_run)
    json_mode = args.format == "json"
    events = None if json_mode else CliEvents(graph, ctx.style, agent_output=not args.quiet)
    if events is not None:
        events.write(ctx.style.bold("Auditing the repository against the spec graph") + ctx.style.dim(" (cached evidence ignored)"))
    reconciler = Reconciler(graph, ws, adapter, options=_execute_options(ws, args), events=events)
    report = reconciler.run(dry_run=args.dry_run)
    if json_mode:
        ctx.out(to_json(report.to_dict()))
    else:
        ctx.out(render_reconcile(report, graph, ctx.style))
    advisory = any(d.kind in ("unowned-files", "shared-file") for d in report.after)
    if report.execution is not None and report.execution.aborted == "interrupted":
        return EXIT_INTERRUPTED
    return EXIT_OK if report.conformant and not (args.strict and advisory) else EXIT_FAIL


def cmd_agents(ctx: Context, args: argparse.Namespace) -> int:
    ws = ctx.workspace()
    width = 0
    rows = []
    for name, description in sorted(available_agents(ws.config).items()):
        try:
            adapter = create_adapter(name, ws.config, ws.root)
            reason = adapter.unavailable_reason()
            marker = ctx.style.ok(reason is None)
        except AgentConfigError:
            reason = "needs configuration in rsde.toml (see README)"
            marker = ctx.style.dim("·")
        default = ctx.style.cyan(" (default)") if name == ws.config.agent else ""
        rows.append((marker, name, description + default, reason))
        width = max(width, len(name))
    for marker, name, description, reason in rows:
        ctx.out(f"{marker} {name.ljust(width)}  {description}" + (ctx.style.dim(f"  — {reason}") if reason else ""))
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #

DESCRIPTION = """\
RSDE — Recursive Spec-Driven Engineering.

A repository is described by Markdown specs rooted at master.md. RSDE parses
them into a typed spec graph, validates it, and drives a coding agent until
every spec is backed by passing verification evidence."""

EPILOG = """\
examples:
  rsde check                         validate the whole spec graph
  rsde graph --format mermaid        hierarchy + dependencies as a diagram
  rsde execute master.md --plan      what would run, and why
  rsde execute --agent claude        implement and verify everything
  rsde execute specs/storage.spec.md one spec, plus what it depends on
  rsde affected specs/model.spec.md  what a change could impact
  rsde reconcile --dry-run           audit the repository for drift
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rsde",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"rsde {__version__}")
    parser.add_argument("-C", "--workspace", type=Path, metavar="DIR", help="workspace directory (default: discovered)")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto", help="colour output")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-C", "--workspace", type=Path, metavar="DIR", default=argparse.SUPPRESS, help="workspace directory")
    common.add_argument("--color", choices=("auto", "always", "never"), default=argparse.SUPPRESS, help="colour output")

    def fmt(p: argparse.ArgumentParser, *choices: str) -> None:
        p.add_argument("--format", choices=choices, default=choices[0], help="output format")

    def agent_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("--agent", metavar="NAME", help="coding agent (default: [execute].agent, else none)")
        p.add_argument("--max-attempts", type=int, metavar="N", help="agent attempts per spec (default 3)")
        p.add_argument("--verify-timeout", type=float, metavar="SEC", help="timeout per @verify command")
        p.add_argument("--agent-timeout", type=float, metavar="SEC", help="timeout per agent invocation")
        p.add_argument("--strict-scope", action="store_true", help="fail specs whose agent edits files outside @implement")
        p.add_argument("-q", "--quiet", action="store_true", help="do not stream agent output")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def command(name: str, handler: Callable[[Context, argparse.Namespace], int], help: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, parents=[common], help=help, description=help)
        p.set_defaults(handler=handler)
        return p

    p = command("init", cmd_init, "create master.md and rsde.toml in a directory")
    p.add_argument("directory", nargs="?", help="target directory (default: current)")
    p.add_argument("--force", action="store_true", help="overwrite existing files")

    p = command("check", cmd_check, "validate the complete spec graph without changing anything")
    p.add_argument("root", nargs="?", help=f"root spec (default: {DEFAULT_ROOT_SPEC} or [project].root)")
    p.add_argument("--strict", action="store_true", help="treat warnings as errors")
    fmt(p, "text", "json")

    p = command("graph", cmd_graph, "display the spec hierarchy and dependency graph")
    p.add_argument("root", nargs="?", help="root spec")
    p.add_argument("--no-status", action="store_true", help="do not annotate specs with their status")
    fmt(p, "text", "mermaid", "dot", "json")

    p = command("show", cmd_show, "show one spec with its inherited intent, interfaces and evidence")
    p.add_argument("spec", help="spec path, id or [[name]]")
    p.add_argument("--prompt", action="store_true", help="print the exact task prompt an agent would receive")
    fmt(p, "text", "json")

    p = command("status", cmd_status, "show satisfaction status from recorded evidence (runs nothing)")
    p.add_argument("spec", nargs="?", help="spec to report on (default: the root)")
    fmt(p, "text", "json")

    p = command("execute", cmd_execute, "resolve, plan, implement and verify a spec and everything it needs")
    p.add_argument("spec", nargs="?", help="target spec (default: the root, e.g. master.md)")
    p.add_argument("--plan", "--dry-run", dest="plan", action="store_true", help="show the plan and exit")
    p.add_argument("--force", action="store_true", help="re-verify specs even if their evidence is fresh")
    p.add_argument("--verify-only", action="store_true", help="run checks but never invoke the agent")
    agent_options(p)
    fmt(p, "text", "json")

    p = command("affected", cmd_affected, "list the specs a change may impact")
    p.add_argument("targets", nargs="*", metavar="SPEC_OR_FILE", help="changed specs or implementation files")
    p.add_argument("--since", metavar="REV", help="use files changed since a git revision (plus untracked files)")
    fmt(p, "text", "json")

    p = command("reconcile", cmd_reconcile, "audit the repository against the specs and make it conform")
    p.add_argument("--dry-run", action="store_true", help="report drift without invoking the agent")
    p.add_argument("--strict", action="store_true", help="also fail on unowned or shared files")
    agent_options(p)
    fmt(p, "text", "json")

    command("agents", cmd_agents, "list available coding-agent adapters")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return EXIT_USAGE
    ctx = Context(args)
    try:
        return args.handler(ctx, args)
    except (UsageError, WorkspaceError, ConfigError, AgentConfigError) as exc:
        ctx.err(ctx.err_style.red(ctx.err_style.bold("error")) + f": {exc}")
        return EXIT_USAGE
    except KeyboardInterrupt:
        ctx.err("interrupted")
        return EXIT_INTERRUPTED
