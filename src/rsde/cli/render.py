"""Human-readable rendering of diagnostics, graphs, plans and reports.

Every function here is pure: data in, text out. Machine-readable output
(``--format json``) is produced by the ``*_to_dict`` helpers.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable, Mapping, Sequence

from rsde.cli.style import Style
from rsde.diagnostics import Diagnostic
from rsde.engine.executor import ExecutionReport
from rsde.engine.reconcile import ReconcileReport
from rsde.graph.analysis import Impact
from rsde.graph.intent import resolve_intent
from rsde.graph.model import SpecGraph
from rsde.planning.fingerprint import Fingerprint
from rsde.planning.planner import Action, Plan
from rsde.planning.status import SpecStatus, Status
from rsde.repository.files import matching_files


def plural(count: int, word: str, suffix: str = "s") -> str:
    return f"{count} {word}{'' if count == 1 else suffix}"


def one_line(text: str, width: int = 0) -> str:
    """Join continuation lines without touching spacing inside a line (it may be significant)."""
    flat = " ".join(line.strip() for line in text.splitlines() if line.strip())
    if width and len(flat) > width:
        return flat[: width - 1] + "…"
    return flat


# --------------------------------------------------------------------------- #
# Diagnostics (rustc style)
# --------------------------------------------------------------------------- #


def render_diagnostic(diag: Diagnostic, sources: Mapping[str, str], style: Style) -> str:
    colour = style.red if diag.is_error else style.yellow
    head = colour(style.bold(f"{diag.severity.value}[{diag.code}]")) + style.bold(f": {diag.message}")
    lines = [head]
    span = diag.span
    gutter_width = len(str(span.line)) if span else 1
    pad = " " * gutter_width
    bar = style.blue("|")
    if span is not None:
        lines.append(f"{pad}{style.blue('-->')} {span}")
        source_lines = sources.get(span.path, "").splitlines()
        if 0 < span.line <= len(source_lines) and span.length:
            text = source_lines[span.line - 1].expandtabs(4)
            start = max(span.column - 1, 0)
            length = max(min(span.length, len(text) - start), 1)
            marker = " " * start + "^" * length
            lines.append(f"{pad} {bar}")
            lines.append(f"{style.blue(str(span.line).rjust(gutter_width))} {bar} {text}")
            lines.append(f"{pad} {bar} {colour(marker)}" + (f" {colour(diag.label)}" if diag.label else ""))
            lines.append(f"{pad} {bar}")
    for related in diag.related:
        lines.append(f"{pad} {style.blue('=')} {style.bold('note')}: {related.message} at {related.span}")
    for note in diag.notes:
        lines.append(f"{pad} {style.blue('=')} {style.bold('note')}: {note}")
    if diag.help:
        lines.append(f"{pad} {style.blue('=')} {style.bold('help')}: {diag.help}")
    return "\n".join(lines)


def render_diagnostics(diags: Iterable[Diagnostic], sources: Mapping[str, str], style: Style) -> str:
    return "\n\n".join(render_diagnostic(d, sources, style) for d in diags)


def render_check_summary(graph: SpecGraph, style: Style, *, strict: bool = False) -> str:
    errors, warnings = len(graph.errors), len(graph.warnings)
    shape = (
        f"{plural(len(graph.specs), 'spec')}, "
        f"{plural(len(graph.dependencies.edges), 'dependency edge')}, "
        f"depth {max((graph.hierarchy.depth(s) for s in graph.specs), default=0)}"
    )
    if errors or (strict and warnings):
        counts = [plural(errors, "error")] + ([plural(warnings, "warning")] if warnings else [])
        return style.red(style.bold("✗ check failed" if style.unicode else "x check failed")) + f": {', '.join(counts)} ({shape})"
    tail = f" with {plural(warnings, 'warning')}" if warnings else ""
    return style.green(style.bold("✓ spec graph is valid" if style.unicode else "+ spec graph is valid")) + f"{tail} ({shape})"


# --------------------------------------------------------------------------- #
# Hierarchy and dependency graph
# --------------------------------------------------------------------------- #


def render_tree(
    graph: SpecGraph,
    style: Style,
    *,
    root: str | None = None,
    statuses: Mapping[str, SpecStatus] | None = None,
    annotate: Callable[[str], str] | None = None,
) -> list[str]:
    root = root or graph.root_id

    def label(spec_id: str) -> str:
        doc = graph.specs[spec_id]
        parts = []
        if statuses is not None:
            parts.append(style.glyph(statuses[spec_id].status))
        parts.append(style.bold(spec_id))
        parts.append(style.dim(f"{style.dash} {doc.title}"))
        text = " ".join(parts)
        if statuses is not None and not statuses[spec_id].satisfied:
            text += " " + style.dim(f"({statuses[spec_id].reason})")
        if annotate is not None:
            extra = annotate(spec_id)
            if extra:
                text += " " + extra
        return text

    lines = [label(root)]

    def walk(node: str, prefix: str) -> None:
        children = graph.hierarchy.children(node)
        for i, child in enumerate(children):
            last = i == len(children) - 1
            lines.append(prefix + style.dim(style.elbow if last else style.tee) + label(child))
            walk(child, prefix + ("    " if last else style.dim(style.pipe)))

    walk(root, "")
    return lines


def _capability_annotation(graph: SpecGraph, style: Style) -> Callable[[str], str]:
    def annotate(spec_id: str) -> str:
        provides = [c.name for c in graph.specs[spec_id].provides]
        return style.cyan("[provides " + ", ".join(provides) + "]") if provides else ""

    return annotate


def render_dependencies(graph: SpecGraph, style: Style) -> list[str]:
    edges = graph.dependencies.edges
    externals = [(s, e) for s, d in graph.specs.items() for e in d.externals]
    if not edges and not externals:
        return [style.dim("(no dependencies)")]
    lines = []
    width = max((len(e.source) for e in edges), default=0)
    for edge in edges:
        via = f"requires {edge.capability}" if edge.kind == "requires" else "depends on"
        lines.append(f"{edge.source.ljust(width)}  {style.dim(via)} {style.arrow} {style.bold(edge.target)}")
    for spec_id, ext in externals:
        lines.append(f"{spec_id.ljust(width)}  {style.dim('external')} {style.arrow} {style.magenta(str(ext))}")
    return lines


def render_graph_text(graph: SpecGraph, style: Style, statuses: Mapping[str, SpecStatus] | None = None) -> str:
    out = [style.bold("Hierarchy") + style.dim("  (@spec — intent flows down, evidence flows up)")]
    out.extend(render_tree(graph, style, statuses=statuses, annotate=_capability_annotation(graph, style)))
    out.append("")
    out.append(style.bold("Dependencies") + style.dim("  (@requires → @provides, @depends)"))
    out.extend(render_dependencies(graph, style))
    return "\n".join(out)


_MERMAID_CLASSES = {
    Status.SATISFIED: "fill:#d1f4dd,stroke:#1a7f37",
    Status.FAILED: "fill:#ffd8d3,stroke:#cf222e",
    Status.STALE: "fill:#fff4c2,stroke:#9a6700",
    Status.PENDING: "fill:#eef1f4,stroke:#6e7781",
    Status.BLOCKED: "fill:#f1e4ff,stroke:#8250df",
    Status.INCOMPLETE: "fill:#fff4c2,stroke:#9a6700",
    Status.UNVERIFIABLE: "fill:#ffd8d3,stroke:#cf222e",
}


def render_mermaid(graph: SpecGraph, statuses: Mapping[str, SpecStatus] | None = None) -> str:
    ids = {spec_id: f"s{i}" for i, spec_id in enumerate(graph.specs)}

    def quote(text: str) -> str:
        return text.replace('"', "#quot;")

    lines = ["flowchart TD"]
    for spec_id, doc in graph.specs.items():
        lines.append(f'    {ids[spec_id]}["{quote(spec_id)}<br/><small>{quote(doc.title)}</small>"]')
    for spec_id in graph.specs:
        for child in graph.hierarchy.children(spec_id):
            lines.append(f"    {ids[spec_id]} --> {ids[child]}")
    for edge in graph.dependencies.edges:
        label = edge.capability if edge.kind == "requires" else "depends"
        lines.append(f'    {ids[edge.source]} -. "{quote(label or "")}" .-> {ids[edge.target]}')
    if statuses:
        used = sorted({statuses[s].status for s in graph.specs}, key=lambda s: s.value)
        for status in used:
            lines.append(f"    classDef {status.value} {_MERMAID_CLASSES[status]}")
        for spec_id in graph.specs:
            lines.append(f"    class {ids[spec_id]} {statuses[spec_id].status.value}")
    return "\n".join(lines)


def render_dot(graph: SpecGraph, statuses: Mapping[str, SpecStatus] | None = None) -> str:
    colours = {Status.SATISFIED: "#1a7f37", Status.FAILED: "#cf222e", Status.STALE: "#9a6700"}

    def q(text: str) -> str:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = ["digraph rsde {", "    rankdir=TB;", '    node [shape=box, style="rounded", fontname="Helvetica"];']
    for spec_id, doc in graph.specs.items():
        attrs = [f"label={q(spec_id + chr(10) + doc.title)}"]
        if statuses and statuses[spec_id].status in colours:
            attrs.append(f'color="{colours[statuses[spec_id].status]}"')
        lines.append(f"    {q(spec_id)} [{', '.join(attrs)}];")
    for spec_id in graph.specs:
        for child in graph.hierarchy.children(spec_id):
            lines.append(f"    {q(spec_id)} -> {q(child)};")
    for edge in graph.dependencies.edges:
        label = edge.capability if edge.kind == "requires" else "depends"
        lines.append(f"    {q(edge.source)} -> {q(edge.target)} [style=dashed, color=gray40, label={q(label or '')}];")
    lines.append("}")
    return "\n".join(lines)


def graph_to_dict(graph: SpecGraph, statuses: Mapping[str, SpecStatus] | None = None) -> dict[str, Any]:
    specs = []
    for spec_id, doc in graph.specs.items():
        entry: dict[str, Any] = {
            "id": spec_id,
            "path": doc.path,
            "title": doc.title,
            "goal": doc.goal,
            "parent": graph.hierarchy.parent(spec_id),
            "children": list(graph.hierarchy.children(spec_id)),
            "provides": [c.name for c in doc.provides],
            "requires": [c.name for c in doc.requires],
            "depends": [graph.specs[e.target].id for e in graph.dependencies.edges_from(spec_id) if e.kind == "depends"],
            "externals": [str(e) for e in doc.externals],
            "implement": list(doc.implement_patterns),
            "verify": list(doc.verify_commands),
        }
        if statuses is not None:
            entry["status"] = statuses[spec_id].status.value
        specs.append(entry)
    return {
        "root": graph.root_id,
        "specs": specs,
        "dependencies": [
            {"from": e.source, "to": e.target, "kind": e.kind, "capability": e.capability}
            for e in graph.dependencies.edges
        ],
    }


# --------------------------------------------------------------------------- #
# Status and show
# --------------------------------------------------------------------------- #


def status_counts(statuses: Iterable[SpecStatus]) -> dict[Status, int]:
    counts: dict[Status, int] = {}
    for status in statuses:
        counts[status.status] = counts.get(status.status, 0) + 1
    return counts


def render_counts(statuses: Iterable[SpecStatus], style: Style) -> str:
    counts = status_counts(statuses)
    order = [Status.SATISFIED, Status.FAILED, Status.STALE, Status.PENDING, Status.BLOCKED, Status.INCOMPLETE, Status.UNVERIFIABLE]
    return " · ".join(f"{style.glyph(s)} {counts[s]} {s.value}" for s in order if counts.get(s))


def render_status(graph: SpecGraph, statuses: Mapping[str, SpecStatus], style: Style, target: str) -> str:
    out = render_tree(graph, style, root=target, statuses=statuses)
    subtree = graph.hierarchy.subtree(target)
    out.append("")
    out.append(render_counts((statuses[s] for s in subtree), style))
    verdict = statuses[target]
    word = "satisfied" if verdict.satisfied else f"not satisfied ({verdict.status.value}: {verdict.reason})"
    out.append(f"{style.glyph(verdict.status)} {style.bold(target)} is {word}")
    return "\n".join(out)


def render_show(
    graph: SpecGraph,
    spec_id: str,
    style: Style,
    *,
    statuses: Mapping[str, SpecStatus] | None = None,
    fingerprints: Mapping[str, Fingerprint] | None = None,
    files: Sequence[str] = (),
) -> str:
    doc = graph.specs[spec_id]
    intent = resolve_intent(graph, spec_id)
    out = [style.bold(spec_id) + " " + style.dim(f"{style.dash} {doc.title}")]

    def field(name: str, value: str) -> None:
        out.append(f"  {style.dim(name.ljust(9))} {value}")

    field("file", doc.path)
    field("parent", graph.hierarchy.parent(spec_id) or "(root)")
    field("children", ", ".join(graph.hierarchy.children(spec_id)) or "—")
    if statuses is not None:
        status = statuses[spec_id]
        detail = status.reason
        if fingerprints is not None:
            detail += f" · fingerprint {fingerprints[spec_id].short}"
        field("status", f"{style.status(status.status)} {style.dim('· ' + detail)}")

    def section(title: str) -> None:
        out.append("")
        out.append(style.bold(title))

    if doc.goals:
        section("Goal")
        out.extend(f"  {one_line(g.text)}" for g in doc.goals)
    if intent.inherited and any(not f.empty for f in intent.inherited):
        section("Inherited intent")
        for frame in intent.inherited:
            if frame.empty:
                continue
            out.append(f"  {style.bold(frame.spec_id)} {style.dim(style.dash + ' ' + frame.title)}")
            for kind, items in (("goal", frame.goals), ("constraint", frame.constraints), ("invariant", frame.invariants)):
                for item in items:
                    out.append(f"    {style.dim(kind.ljust(10))} {one_line(item.text)}")
    interfaces = []
    for cap in doc.provides:
        users = graph.dependencies.consumers(cap.name)
        used = style.dim(f"  used by {', '.join(users)}") if users else ""
        interfaces.append(f"  {style.dim('provides'.ljust(9))} {style.cyan(cap.name)}{used}")
    for edge in graph.dependencies.edges_from(spec_id):
        what = f"{style.cyan(edge.capability or '')} from" if edge.kind == "requires" else "on"
        interfaces.append(f"  {style.dim(edge.kind.ljust(9))} {what} {style.bold(edge.target)}")
    for ext in doc.externals:
        interfaces.append(f"  {style.dim('external'.ljust(9))} {style.magenta(str(ext))}")
    if interfaces:
        section("Interfaces")
        out.extend(interfaces)
    for title, items in (("Behaviors", doc.behaviors), ("Constraints", doc.constraints), ("Invariants", doc.invariants)):
        if items:
            section(title)
            out.extend(f"  - {one_line(t.text)}" for t in items)
    if doc.implement:
        section("Implementation")
        for target in doc.implement:
            present = matching_files(files, [target.pattern])
            state = style.green(plural(len(present), "file")) if present else style.red("missing")
            out.append(f"  {target.pattern}  {state}")
    if doc.verify:
        section("Verification")
        for check in doc.verify:
            out.extend(f"  $ {line}" if i == 0 else f"    {line}" for i, line in enumerate(check.command.splitlines()))
    if doc.done:
        section("Definition of done")
        out.extend(f"  - {one_line(t.text)}" for t in doc.done)
    if statuses is not None and statuses[spec_id].evidence is not None:
        evidence = statuses[spec_id].evidence
        assert evidence is not None
        section("Evidence")
        out.append(f"  {style.dim('recorded')} {evidence.verified_at} · run {evidence.run_id}")
        for check in evidence.checks:
            out.append(f"  {style.ok(check.passed)} {check.command}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Plans and execution
# --------------------------------------------------------------------------- #

_ACTION_TEXT = {
    Action.SKIP: "skip",
    Action.VERIFY: "verify → implement on failure",
    Action.CHECK: "verify only",
    Action.ROLLUP: "roll up children",
    Action.UNVERIFIABLE: "cannot verify",
}


def render_plan(plan: Plan, style: Style, agent: str) -> str:
    work = len(plan.work)
    out = [
        style.bold(f"Plan for `{plan.target}`")
        + style.dim(f" — {plural(len(plan.steps), 'spec')} in scope, {work} to verify, agent: {agent}")
    ]
    width = max((len(s.spec_id) for s in plan.steps), default=4)
    for index, step in enumerate(plan.steps, 1):
        text = _ACTION_TEXT[step.action].ljust(30)
        action = style.bold(text) if step.action in (Action.VERIFY, Action.CHECK) else style.dim(text)
        out.append(
            f"  {str(index).rjust(2)}. {style.glyph(step.before.status)} {step.spec_id.ljust(width)}  {action}"
            f" {style.dim(step.reason)}"
        )
    return "\n".join(out)


def render_execution_summary(report: ExecutionReport, graph: SpecGraph, style: Style) -> str:
    statuses = report.statuses
    out = ["", style.bold("Result")]
    out.extend(render_tree(graph, style, root=report.target, statuses=statuses))
    subtree = set(graph.hierarchy.subtree(report.target))
    outside = [s for s in report.scope if s not in subtree]
    if outside:
        out.append(style.dim("dependencies outside this subtree:"))
        for spec_id in outside:
            out.append(f"  {style.glyph(statuses[spec_id].status)} {spec_id}")

    failures = [s for s in report.scope if statuses[s].status is Status.FAILED]
    for spec_id in failures:
        evidence = statuses[spec_id].evidence
        if evidence is None:
            continue
        out.append("")
        out.append(f"{style.glyph(Status.FAILED)} {style.bold(spec_id)}: {statuses[spec_id].reason}")
        for check in evidence.failed_checks:
            if check.builtin:
                out.append(style.dim(f"  - {check.output}"))
                continue
            out.append(style.dim(f"  $ {one_line(check.command, 100)}"))
            for line in check.output.strip().splitlines()[-12:]:
                out.append(style.dim(f"    {line}"))

    out.append("")
    in_scope = [statuses[s] for s in report.scope]
    satisfied = sum(1 for s in in_scope if s.satisfied)
    facts = (
        f"{satisfied}/{len(in_scope)} specs satisfied · {plural(report.agent_runs, 'agent run')} · "
        f"{plural(report.checks_run, 'check')} · run {report.run_id}"
    )
    if report.aborted:
        out.append(style.red(style.bold(f"Run aborted: {report.aborted}")))
    if report.satisfied:
        out.append(f"{style.glyph(Status.SATISFIED)} {style.bold(report.target)} is {style.green('satisfied')} {style.dim('— ' + facts)}")
    else:
        verdict = statuses[report.target]
        out.append(
            f"{style.glyph(verdict.status)} {style.bold(report.target)} is {style.red('not satisfied')} "
            f"({verdict.status.value}) {style.dim('— ' + facts)}"
        )
    if report.run_dir is not None:
        out.append(style.dim(f"  prompts, agent transcripts and check logs: {report.run_dir}"))
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Affected and reconcile
# --------------------------------------------------------------------------- #

_IMPACT_STYLE = {"changed": "bold", "intent": "cyan", "evidence": "yellow", "dependency": "magenta"}


def render_affected(impacts: Sequence[Impact], style: Style, seeds: Mapping[str, Sequence[str]], unowned: Sequence[str]) -> str:
    out = []
    for spec_id, why in seeds.items():
        out.append(f"{style.bold('changed')} {spec_id}" + (style.dim(f"  ({', '.join(why)})") if why else ""))
    if unowned:
        out.append(style.dim("not owned by any spec: " + ", ".join(unowned)))
    out.append("")
    out.append(style.bold(f"{plural(len(impacts), 'spec')} may be affected") + style.dim(" (in re-verification order)"))
    width = max((len(i.spec_id) for i in impacts), default=4)
    for impact in impacts:
        kind = style.paint(impact.kind.ljust(10), _IMPACT_STYLE[impact.kind])
        out.append(f"  {impact.spec_id.ljust(width)}  {kind} {style.dim('; '.join(impact.reasons))}")
    return "\n".join(out)


_DRIFT_TITLES = {
    "missing-files": "declared implementation missing",
    "failing-checks": "failing checks",
    "unverifiable": "unverifiable specs",
    "shared-file": "files claimed by several specs",
    "unowned-files": "files no spec owns (never deleted; adopt them with @implement or ignore them in rsde.toml)",
}


def render_drift(drift: Sequence[Any], style: Style) -> list[str]:
    if not drift:
        return [f"  {style.green('no drift')}"]
    out = []
    for kind, title in _DRIFT_TITLES.items():
        items = [d for d in drift if d.kind == kind]
        if not items:
            continue
        colour = style.red if items[0].blocking else style.yellow
        out.append(f"  {colour(title)}")
        for item in items:
            out.append(f"    - {item.message}")
            paths = item.paths if kind != "unowned-files" else item.paths[:10]
            if paths and kind in ("missing-files", "unowned-files"):
                out.append(style.dim("      " + ", ".join(paths) + (" …" if len(item.paths) > len(paths) else "")))
    return out


def render_reconcile(report: ReconcileReport, graph: SpecGraph, style: Style) -> str:
    out = ["", style.bold("Drift found by the audit")]
    out.extend(render_drift(report.before, style))
    if report.execution is not None:
        out.append(render_execution_summary(report.execution, graph, style))
        out.append("")
        out.append(style.bold("Drift remaining"))
        out.extend(render_drift(report.after, style))
    elif report.dry_run:
        out.append("")
        out.append(style.dim("dry run: no agent was invoked"))
    out.append("")
    satisfied = len(report.conformant_specs)
    if report.conformant:
        out.append(f"{style.glyph(Status.SATISFIED)} repository {style.green('conforms')} to `{report.root_id}` "
                   + style.dim(f"({satisfied}/{len(graph.specs)} specs satisfied)"))
    else:
        out.append(f"{style.glyph(Status.FAILED)} repository {style.red('does not conform')} to `{report.root_id}` "
                   + style.dim(f"({satisfied}/{len(graph.specs)} specs satisfied)"))
    return "\n".join(out)


def to_json(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)
