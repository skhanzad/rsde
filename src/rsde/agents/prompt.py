"""Compile a spec into a deterministic agent task prompt.

The prompt is generated from the typed graph, never by pasting the spec file
through: the same graph, files and evidence always yield the same prompt.
It states the scope the agent may touch, the effective intent (own plus
inherited constraints and invariants), the interfaces to other specs, and
exactly which checks are failing and why.
"""

from __future__ import annotations

from typing import Sequence

from rsde.graph.intent import resolve_intent
from rsde.graph.model import SpecGraph
from rsde.repository.files import matching_files
from rsde.repository.state import CheckResult
from rsde.syntax.ast import Text

PREAMBLE = (
    "You are a coding agent invoked by RSDE (Recursive Spec-Driven Engineering). "
    "Change the repository so that it satisfies the specification below. Success is decided "
    "deterministically, not by you: the spec counts as satisfied only when every file in "
    "**Scope** exists and every command under **Verification** exits with status 0. "
    "Work autonomously; do not ask questions, make reasonable choices within the constraints."
)


def _items(texts: Sequence[Text], numbered: bool = False) -> list[str]:
    return [f"{i}. {t.text}" if numbered else f"- {t.text}" for i, t in enumerate(texts, 1)]


def render_task_prompt(
    graph: SpecGraph,
    spec_id: str,
    *,
    attempt: int = 1,
    max_attempts: int = 1,
    results: Sequence[CheckResult] = (),
    files: Sequence[str] | None = None,
    scope_violations: Sequence[str] = (),
) -> str:
    doc = graph.specs[spec_id]
    files = graph.files if files is None else files
    intent = resolve_intent(graph, spec_id)
    spec_files = sorted(graph.spec_paths())
    out: list[str] = []
    w = out.append

    w(f"# RSDE task: satisfy spec `{spec_id}`")
    w("")
    w(PREAMBLE)
    w("")
    w(f"Attempt {attempt} of {max_attempts}.")
    w("")

    w("## Rules")
    w("1. Only create, modify or delete files inside this spec's **Scope**. Changes anywhere else are "
      "recorded as scope violations.")
    w("2. Never edit specification files; they are the source of truth: "
      + ", ".join(f"`{p}`" for p in spec_files) + ".")
    w("3. Honour every constraint and invariant below, including the inherited ones.")
    w("4. If you can run shell commands, run the verification commands yourself before you finish.")
    w("")

    w("## Scope")
    if doc.implement:
        for target in doc.implement:
            present = matching_files(files, [target.pattern])
            state = f"exists ({len(present)} file{'s' if len(present) != 1 else ''})" if present else "missing"
            w(f"- `{target.pattern}` — {state}")
    else:
        w("- (this spec declares no @implement paths; keep changes minimal and focused on the checks)")
    w("")

    w(f"## Specification: {doc.title} (`{doc.path}`)")
    w("")
    if doc.goals:
        w("### Goal")
        out.extend(g.text for g in doc.goals)
        w("")
    inherited = [f for f in intent.inherited if f.goals]
    if inherited:
        w("### Context (intent inherited from ancestors)")
        for frame in inherited:
            w(f"- **{frame.title}** (`{frame.spec_id}`): " + " ".join(g.one_line for g in frame.goals))
        w("")
    if doc.behaviors:
        w("### Behaviors to implement")
        out.extend(_items(doc.behaviors, numbered=True))
        w("")
    constraints = intent.constraints()
    if constraints:
        w("### Constraints")
        for origin, text in constraints:
            w(f"- {text.text}" + ("" if origin == spec_id else f" _(inherited from `{origin}`)_"))
        w("")
    invariants = intent.invariants()
    if invariants:
        w("### Invariants (must always hold)")
        for origin, text in invariants:
            w(f"- {text.text}" + ("" if origin == spec_id else f" _(inherited from `{origin}`)_"))
        w("")
    if doc.done:
        w("### Definition of done")
        out.extend(_items(doc.done))
        w("")

    interface = _interfaces(graph, spec_id)
    if interface:
        w("### Interfaces")
        out.extend(interface)
        w("")

    children = graph.hierarchy.children(spec_id)
    if children:
        w("### Child specs (already satisfied — integrate them, do not reimplement them)")
        for child in children:
            cdoc = graph.specs[child]
            owned = ", ".join(f"`{p}`" for p in cdoc.implement_patterns) or "no files"
            w(f"- `{child}` — {cdoc.title}: {cdoc.goal or 'no goal'} (owns {owned})")
        w("")

    if doc.notes.strip():
        w("### Notes from the spec")
        w(doc.notes.strip())
        w("")

    w("## Verification")
    if doc.verify:
        w("Run from the workspace root; every command must exit with status 0:")
        for i, check in enumerate(doc.verify, 1):
            if "\n" in check.command:
                w(f"{i}. script:")
                w("```sh")
                w(check.command)
                w("```")
            else:
                w(f"{i}. `{check.command}`")
    else:
        w("This spec declares no @verify commands; only the presence of its files is checked.")
    w("")

    failing = [r for r in results if not r.passed]
    if failing or scope_violations:
        w("## What is currently unsatisfied")
        for result in failing:
            if result.builtin:
                w(f"- Missing implementation — {result.output}")
                continue
            why = "timed out" if result.timed_out else f"exited with status {result.exit_code}"
            w(f"- `{result.command}` {why}. Output (tail):")
            w("```")
            w(result.output.strip() or "(no output)")
            w("```")
        if scope_violations:
            w("- Your previous attempt changed files outside the scope: "
              + ", ".join(f"`{p}`" for p in scope_violations)
              + ". Revert those changes unless they are strictly required.")
        w("")
    return "\n".join(out).rstrip() + "\n"


def _interfaces(graph: SpecGraph, spec_id: str) -> list[str]:
    doc = graph.specs[spec_id]
    lines: list[str] = []
    for cap in doc.provides:
        users = graph.dependencies.consumers(cap.name)
        desc = f" — {cap.description}" if cap.description else ""
        used = f" (used by {', '.join(f'`{u}`' for u in users)})" if users else ""
        lines.append(f"- Provides `{cap.name}`{desc}{used}")
    for edge in graph.dependencies.edges_from(spec_id):
        target = graph.specs[edge.target]
        where = ", ".join(f"`{p}`" for p in target.implement_patterns) or "no declared files"
        if edge.kind == "requires":
            lines.append(f"- Requires `{edge.capability}` from `{edge.target}` ({target.title}; implemented in {where})")
        else:
            lines.append(f"- Depends on `{edge.target}` ({target.title}; implemented in {where})")
    for ext in doc.externals:
        lines.append(f"- External: `{ext}`")
    return lines
