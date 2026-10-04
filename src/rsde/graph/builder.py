"""Graph construction: discover, parse, link and validate a workspace's specs.

:func:`build_graph` is the single entry point. It is deterministic: the same
files always produce the same graph and the same diagnostics, in the same
order. It never modifies the workspace.

Pipeline::

    master.md ──parse──▶ SpecDocument ──@spec──▶ children (recursively)
                                       ──@depends / @requires──▶ dependency edges
    then: capability resolution → cycle detection → completeness checks
"""

from __future__ import annotations

import difflib
import os
import posixpath
import shutil
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from rsde.diagnostics import Diagnostics, Related, SourceSpan
from rsde.graph.model import DependencyEdge, DependencyGraph, Hierarchy, SpecGraph
from rsde.repository.config import DEFAULT_ROOT_SPEC
from rsde.repository.files import GlobSet, list_workspace_files
from rsde.syntax.ast import RefStyle, SpecDocument, SpecRef
from rsde.syntax.parser import parse_document, spec_id_for


def build_graph(
    root: Path,
    root_spec: str = DEFAULT_ROOT_SPEC,
    *,
    ignore: Sequence[str] = (),
    probe_environment: bool = True,
    files: list[str] | None = None,
) -> SpecGraph:
    """Load the spec graph rooted at ``root/root_spec`` and validate it."""
    root = root.resolve()
    if files is None:
        files = list_workspace_files(root, ignore)
    diags = Diagnostics()
    loader = _Loader(root, files, diags)
    root_rel = posixpath.normpath(root_spec.replace("\\", "/"))

    root_exists = (root / root_rel).is_file()
    if not root_exists:
        diags.error(
            "E200",
            f"root spec `{root_rel}` not found in {root}",
            help="create it (`rsde init`) or point [project].root in rsde.toml at your root spec",
        )
    if not root_exists or loader.read(root_rel) is None:  # an unreadable root reports E206
        empty = Hierarchy(spec_id_for(root_rel), {}, {})
        return SpecGraph(root, spec_id_for(root_rel), {}, empty, DependencyGraph((), {}), diags.sorted(), {}, files)

    loader.walk(root_rel, ())
    depends = loader.link_dependencies()
    loader.report_orphans()

    specs, hierarchy = loader.assemble(root_rel)
    path_to_id = {doc.path: doc.id for doc in specs.values()}
    dep_edges = [
        DependencyEdge(path_to_id[src], path_to_id[dst], "depends", ref.span)
        for src, dst, ref in depends
        if src in path_to_id and dst in path_to_id
    ]
    dependencies = _link_capabilities(specs, hierarchy, dep_edges, diags)
    _check_cycles(specs, hierarchy, dependencies, diags)
    _check_completeness(specs, hierarchy, files, diags)
    if probe_environment:
        _probe_externals(specs, diags)

    return SpecGraph(root, hierarchy.root, specs, hierarchy, dependencies, diags.sorted(), loader.sources, files)


# --------------------------------------------------------------------------- #
# Reference resolution
# --------------------------------------------------------------------------- #


@dataclass
class Resolution:
    path: str | None
    tried: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)


class SpecIndex:
    """Every Markdown file in the workspace, indexed for reference resolution."""

    def __init__(self, root: Path, files: Sequence[str]) -> None:
        self.root = root
        self.paths = {f for f in files if f.endswith(".md")}
        self.ids: dict[str, list[str]] = defaultdict(list)
        for path in sorted(self.paths):
            self.ids[spec_id_for(path)].append(path)

    def resolve(self, ref: SpecRef, from_path: str) -> Resolution:
        if ref.style in (RefStyle.PATH, RefStyle.MARKDOWN):
            return self._resolve_path(ref.target, from_path)
        return self._resolve_name(ref.target)

    def _exists(self, rel: str) -> bool:
        return rel in self.paths or (self.root / rel).is_file()

    def _resolve_path(self, target: str, from_path: str) -> Resolution:
        target = target.replace("\\", "/")
        tried: list[str] = []
        if target.startswith("/"):
            bases = [""]
            target = target.lstrip("/")
        else:
            here = posixpath.dirname(from_path)
            bases = [here, ""] if here else [""]
        for base in bases:
            joined = posixpath.normpath(posixpath.join(base, target))
            if joined == ".." or joined.startswith("../"):
                tried.append(f"{joined} (outside the workspace)")
                continue
            names = [joined] if joined.endswith(".md") else [f"{joined}.spec.md", f"{joined}.md"]
            for name in names:
                if name in tried:
                    continue
                tried.append(name)
                if self._exists(name):
                    return Resolution(name, tried)
        if not target.endswith(".md"):
            by_name = self._resolve_name(target)
            if by_name.path or by_name.ambiguous:
                return by_name
        return Resolution(None, tried, suggestions=self._suggest(target))

    def _resolve_name(self, name: str) -> Resolution:
        name = name.strip().removesuffix(".spec.md").removesuffix(".md")
        for fold in (False, True):
            key = name.lower() if fold else name
            matches = sorted(
                {
                    path
                    for spec_id, paths in self.ids.items()
                    for path in paths
                    if (spec_id.lower() if fold else spec_id) == key
                    or (spec_id.lower() if fold else spec_id).endswith("/" + key)
                }
            )
            exact = [p for p in matches if spec_id_for(p) == name]
            if len(exact) == 1:
                return Resolution(exact[0], [name])
            if len(matches) == 1:
                return Resolution(matches[0], [name])
            if len(matches) > 1:
                return Resolution(None, [name], ambiguous=matches)
        return Resolution(None, [name], suggestions=self._suggest(name))

    def _suggest(self, target: str) -> list[str]:
        stem = posixpath.basename(target.removesuffix(".spec.md").removesuffix(".md"))
        pool = {posixpath.basename(i): i for i in self.ids}
        close = difflib.get_close_matches(stem, list(pool), n=3, cutoff=0.6)
        return [pool[c] for c in close]


# --------------------------------------------------------------------------- #
# Loading the hierarchy
# --------------------------------------------------------------------------- #


class _Loader:
    def __init__(self, root: Path, files: Sequence[str], diags: Diagnostics) -> None:
        self.root = root
        self.diags = diags
        self.index = SpecIndex(root, files)
        self.docs: dict[str, SpecDocument] = {}
        self.sources: dict[str, str] = {}
        self.parent: dict[str, str] = {}
        self.inclusion: dict[str, SpecRef] = {}
        self.children: dict[str, list[str]] = {}
        self.order: list[str] = []
        self.detached: set[str] = set()

    def read(self, rel: str, span: SourceSpan | None = None) -> SpecDocument | None:
        if rel in self.docs:
            return self.docs[rel]
        try:
            text = (self.root / rel).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            self.diags.error("E206", f"`{rel}` is not valid UTF-8 text", span)
            return None
        except OSError as exc:
            self.diags.error("E206", f"cannot read `{rel}`: {exc.strerror or exc}", span)
            return None
        doc, diagnostics = parse_document(text, rel)
        self.diags.extend(diagnostics)
        self.docs[rel] = doc
        self.sources[rel] = text
        return doc

    def resolve(self, ref: SpecRef, from_path: str) -> str | None:
        result = self.index.resolve(ref, from_path)
        if result.path is not None:
            return result.path
        written = ref.raw or ref.target
        if result.ambiguous:
            self.diags.error(
                "E202",
                f"`{written}` is ambiguous: it matches {', '.join(f'`{p}`' for p in result.ambiguous)}",
                ref.span,
                label="ambiguous reference",
                help="use a longer name such as `[[specs/storage]]` or an explicit path",
            )
        else:
            hint = (
                "did you mean " + ", ".join(f"`[[{s}]]`" for s in result.suggestions) + "?"
                if result.suggestions
                else "create the spec file, or fix the path (paths are relative to the referencing spec)"
            )
            self.diags.error(
                "E201",
                f"cannot find spec `{ref.target}`",
                ref.span,
                label="no such spec",
                help=hint,
                notes=[f"looked for: {', '.join(result.tried)}"] if result.tried else [],
            )
        return None

    def walk(self, rel: str, ancestors: tuple[str, ...]) -> None:
        """Depth-first, declaration-ordered traversal of ``@spec`` links."""
        self.order.append(rel)
        self.children[rel] = []
        doc = self.docs[rel]
        for ref in doc.children:
            target = self.resolve(ref, rel)
            if target is None:
                continue
            if target == rel:
                self.diags.error(
                    "E204",
                    f"`{doc.id}` includes itself",
                    ref.span,
                    label="self-inclusion",
                    help="remove this @spec line",
                )
                continue
            if target in ancestors:
                chain = [*ancestors[ancestors.index(target) :], rel, target]
                self.diags.error(
                    "E204",
                    "hierarchy cycle: " + " ⊃ ".join(f"`{spec_id_for(p)}`" for p in chain),
                    ref.span,
                    label=f"`{spec_id_for(target)}` is an ancestor of `{doc.id}`",
                    help="@spec builds a tree; to link to an ancestor use `@depends`, or move the shared part into its own spec",
                )
                continue
            if target in self.parent:
                owner = self.parent[target]
                first = self.inclusion[target]
                again = "again " if owner == rel else ""
                self.diags.error(
                    "E203",
                    f"`{spec_id_for(target)}` is {again}included here, but `{spec_id_for(owner)}` already includes it",
                    ref.span,
                    label="second parent",
                    help="every spec has exactly one parent; to use it from here, `@depends` on it "
                    "or `@requires` a capability it provides",
                    related=(Related(first.span, "first included here"),),
                )
                continue
            if self.read(target, ref.span) is None:
                continue
            self.parent[target] = rel
            self.inclusion[target] = ref
            self.children[rel].append(target)
            self.walk(target, (*ancestors, rel))

    def link_dependencies(self) -> list[tuple[str, str, SpecRef]]:
        """Resolve every ``@depends`` spec reference to a spec in the hierarchy."""
        in_tree = set(self.order)
        edges: list[tuple[str, str, SpecRef]] = []
        for rel in self.order:
            for ref in self.docs[rel].depends:
                target = self.resolve(ref, rel)
                if target is None:
                    continue
                if target not in in_tree:
                    self.detached.add(target)
                    self.diags.error(
                        "E205",
                        f"`{spec_id_for(target)}` is a dependency of `{self.docs[rel].id}` "
                        "but is not part of the spec hierarchy",
                        ref.span,
                        label="detached spec",
                        help=f"add `@spec {target}` to the spec that owns it — every spec needs exactly one parent",
                    )
                    continue
                edges.append((rel, target, ref))
        return edges

    def report_orphans(self) -> None:
        in_tree = set(self.order)
        for path in sorted(self.index.paths):
            if path.endswith(".spec.md") and path not in in_tree and path not in self.detached:
                self.diags.warning(
                    "W201",
                    f"`{path}` is not reachable from the root spec",
                    SourceSpan(path, 1, 1, 0),
                    help=f"link it from its parent with `@spec {path}`, or delete it",
                )

    def assemble(self, root_rel: str) -> tuple[dict[str, SpecDocument], Hierarchy]:
        specs: dict[str, SpecDocument] = {}
        owner: dict[str, str] = {}
        for rel in self.order:
            doc = self.docs[rel]
            if doc.id in specs:
                self.diags.error(
                    "E207",
                    f"`{rel}` and `{owner[doc.id]}` both have spec id `{doc.id}`",
                    SourceSpan(rel, 1, 1, 0),
                    help="rename one of the files; a spec id is its path without `.spec.md`/`.md`",
                )
                continue
            specs[doc.id] = doc
            owner[doc.id] = rel
        root_id = self.docs[root_rel].id
        parents: dict[str, str | None] = {root_id: None}
        children: dict[str, list[str]] = {}
        for rel in self.order:
            doc_id = self.docs[rel].id
            if owner.get(doc_id) != rel:
                continue
            children[doc_id] = [self.docs[c].id for c in self.children.get(rel, []) if owner.get(self.docs[c].id) == c]
            for child in children[doc_id]:
                parents[child] = doc_id
        ordered = {spec_id: specs[spec_id] for spec_id in Hierarchy(root_id, parents, children).preorder()}
        return ordered, Hierarchy(root_id, parents, children)


# --------------------------------------------------------------------------- #
# Dependency graph validation
# --------------------------------------------------------------------------- #


def _link_capabilities(
    specs: dict[str, SpecDocument],
    hierarchy: Hierarchy,
    depends: list[DependencyEdge],
    diags: Diagnostics,
) -> DependencyGraph:
    providers: dict[str, tuple[str, SourceSpan]] = {}
    for spec_id, doc in specs.items():
        for cap in doc.provides:
            first = providers.get(cap.name)
            if first is None:
                providers[cap.name] = (spec_id, cap.span)
                continue
            if first[0] == spec_id:
                continue  # repeated in one spec: already reported as W101
            diags.error(
                "E302",
                f"capability `{cap.name}` is provided by both `{first[0]}` and `{spec_id}`",
                cap.span,
                label="duplicate provider",
                help="each capability has exactly one provider; rename one of them "
                "(e.g. `db.users.read` vs `db.users.write`) or merge the specs",
                related=(Related(first[1], f"`{first[0]}` provides it here"),),
            )

    edges: list[DependencyEdge] = []
    provided = sorted(providers)
    for spec_id, doc in specs.items():
        for req in doc.requires:
            provider = providers.get(req.name)
            if provider is None:
                close = difflib.get_close_matches(req.name, provided, n=1)
                hint = (
                    f"did you mean `{close[0]}` (provided by `{providers[close[0]][0]}`)?"
                    if close
                    else f"add `@provides {req.name}` to the spec that implements it, or remove this requirement"
                )
                diags.error(
                    "E301",
                    f"no spec provides capability `{req.name}`",
                    req.span,
                    label="missing dependency",
                    help=hint,
                )
            elif provider[0] == spec_id:
                diags.warning(
                    "W301",
                    f"`{spec_id}` requires `{req.name}`, which it provides itself",
                    req.span,
                    help="remove the @requires line",
                )
            else:
                edges.append(DependencyEdge(spec_id, provider[0], "requires", req.span, req.name))
        edges.extend(e for e in depends if e.source == spec_id)
    return DependencyGraph(edges, {cap: spec for cap, (spec, _span) in providers.items()})


def _strongly_connected(nodes: Sequence[str], succ: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan's algorithm, iterative so deep graphs cannot overflow the stack."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0
    for start in nodes:
        if start in index:
            continue
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        work = [(start, iter(succ.get(start, ())))]
        while work:
            node, successors = work[-1]
            advanced = False
            for nxt in successors:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(succ.get(nxt, ()))))
                    advanced = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(component)
    return components


def _shortest_cycle(start: str, succ: dict[str, list[str]], members: set[str]) -> list[str]:
    """Shortest path start → … → start inside one strongly connected component."""
    previous: dict[str, str] = {}
    queue: deque[str] = deque()
    for nxt in succ.get(start, ()):
        if nxt == start:
            return [start, start]
        if nxt in members and nxt not in previous:
            previous[nxt] = start
            queue.append(nxt)
    while queue:
        node = queue.popleft()
        for nxt in succ.get(node, ()):
            if nxt == start:
                path = [start, node]
                while path[-1] != start:
                    path.append(previous[path[-1]])
                return list(reversed(path))
            if nxt in members and nxt not in previous:
                previous[nxt] = node
                queue.append(nxt)
    return [start, start]


def _check_cycles(
    specs: dict[str, SpecDocument], hierarchy: Hierarchy, deps: DependencyGraph, diags: Diagnostics
) -> None:
    """Find cycles in the *satisfaction order*.

    A spec can only be satisfied after its dependencies and after its children
    (evidence flows up), so the order graph has an edge ``a → b`` for every
    dependency and for every parent → child link. Any cycle in it means some
    spec would have to be satisfied before itself.
    """
    cut: set[tuple[str, str]] = set()

    # A descendant that depends on its own ancestor is the most common cycle; name it plainly.
    for edge in deps.edges:
        if (edge.source, edge.target) not in cut and edge.target in hierarchy.ancestors(edge.source):
            cut.add((edge.source, edge.target))
            what = f"`{edge.capability}` from it" if edge.kind == "requires" else "it directly"
            diags.error(
                "E303",
                f"`{edge.source}` depends on its ancestor `{edge.target}`",
                edge.span,
                label=f"needs {what}",
                help=f"`{edge.target}` contains `{edge.source}`, so it is satisfied only after `{edge.source}` is; "
                "move the needed capability into a sibling spec that both can rely on",
            )

    # Report one cycle per strongly connected component, cut one of its dependency
    # edges, and repeat until the order graph is acyclic: every independent cycle
    # is reported exactly once. Hierarchy edges alone never form a cycle.
    order = sorted(specs, key=hierarchy.rank)
    while True:
        succ = {
            s: list(
                dict.fromkeys(
                    [*(t for t in deps.dependencies(s) if (s, t) not in cut), *hierarchy.children(s)]
                )
            )
            for s in specs
        }
        found = False
        for component in _strongly_connected(order, succ):
            start = min(component, key=hierarchy.rank)
            if len(component) == 1 and start not in succ[start]:
                continue
            cycle = _shortest_cycle(start, succ, set(component))
            steps = list(zip(cycle, cycle[1:]))
            notes = []
            first_edge: DependencyEdge | None = None
            for a, b in steps:
                edge = next((e for e in deps.edges_from(a) if e.target == b and (a, b) not in cut), None)
                if edge is None:
                    notes.append(f"`{a}` contains `{b}` (a parent is satisfied only after its children)")
                    continue
                first_edge = first_edge or edge
                if edge.kind == "requires":
                    notes.append(f"`{a}` requires `{edge.capability}`, provided by `{b}` ({edge.span})")
                else:
                    notes.append(f"`{a}` depends on `{b}` ({edge.span})")
            if first_edge is None:  # defensive: cannot happen for a valid hierarchy
                continue
            found = True
            cut.add((first_edge.source, first_edge.target))
            diags.error(
                "E303",
                "dependency cycle: " + " → ".join(f"`{s}`" for s in cycle),
                first_edge.span,
                label="part of a cycle",
                help="break the cycle: move the shared functionality into a new spec that both sides can depend on",
                notes=notes,
            )
        if not found:
            return


# --------------------------------------------------------------------------- #
# Completeness and environment
# --------------------------------------------------------------------------- #


def _check_completeness(
    specs: dict[str, SpecDocument], hierarchy: Hierarchy, files: Sequence[str], diags: Diagnostics
) -> None:
    for spec_id, doc in specs.items():
        if not doc.goals:
            diags.warning(
                "W401",
                f"`{spec_id}` has no @goal",
                doc.title_span,
                help="add `@goal <one sentence>` so agents and readers know why this spec exists",
            )
        leaf = hierarchy.is_leaf(spec_id)
        if leaf and not doc.verify:
            if doc.implement:
                message = f"`{spec_id}` has no @verify, so only the existence of its files is checked"
            else:
                message = f"`{spec_id}` can never be satisfied: it has no @verify and no child specs"
            diags.warning(
                "W402",
                message,
                doc.title_span,
                help="add `@verify <command>`; a spec is satisfied only by passing checks",
            )
        if leaf and doc.verify and not doc.implement:
            diags.warning(
                "W403",
                f"`{spec_id}` is verified but owns no files",
                doc.verify[0].span,
                help="add `@implement <paths>` so RSDE can invalidate this spec's evidence when its code changes "
                "and keep agents inside its scope",
            )

    owners: dict[str, list[str]] = defaultdict(list)
    first_span: dict[str, SourceSpan] = {}
    for spec_id, doc in specs.items():
        if not doc.implement:
            continue
        first_span[spec_id] = doc.implement[0].span
        claimed = set(GlobSet(doc.implement_patterns).filter(files))
        claimed.update(t.pattern.rstrip("/") for t in doc.implement)  # identical patterns overlap even before files exist
        for path in claimed:
            owners[path].append(spec_id)
    overlaps: dict[tuple[str, str], list[str]] = defaultdict(list)
    for path, claimants in owners.items():
        unique = list(dict.fromkeys(claimants))
        for i, a in enumerate(unique):
            for b in unique[i + 1 :]:
                overlaps[(a, b)].append(path)
    for (a, b), paths in sorted(overlaps.items(), key=lambda kv: (hierarchy.rank(kv[0][0]), hierarchy.rank(kv[0][1]))):
        shown = ", ".join(f"`{p}`" for p in sorted(paths)[:3]) + (" …" if len(paths) > 3 else "")
        diags.warning(
            "W404",
            f"`{a}` and `{b}` both claim {shown}",
            first_span[b],
            help="give every file exactly one owning spec so scope checks and change tracking stay precise",
            related=(Related(first_span[a], f"claimed by `{a}` here"),),
        )


def _probe_externals(specs: dict[str, SpecDocument], diags: Diagnostics) -> None:
    for spec_id, doc in specs.items():
        for dep in doc.externals:
            if dep.kind == "tool" and shutil.which(dep.name) is None:
                diags.warning(
                    "W302",
                    f"tool `{dep.name}` required by `{spec_id}` is not on PATH",
                    dep.span,
                    help="install it before running `rsde execute`; specs that need it will be blocked",
                )
            elif dep.kind == "env" and dep.name not in os.environ:
                diags.warning(
                    "W302",
                    f"environment variable `{dep.name}` required by `{spec_id}` is not set",
                    dep.span,
                    help="export it before running `rsde execute`; specs that need it will be blocked",
                )


def missing_externals(doc: SpecDocument) -> list[str]:
    """Probed external dependencies of ``doc`` that are unavailable right now."""
    missing = []
    for dep in doc.externals:
        if dep.kind == "tool" and shutil.which(dep.name) is None:
            missing.append(f"tool `{dep.name}` is not on PATH")
        elif dep.kind == "env" and dep.name not in os.environ:
            missing.append(f"environment variable `{dep.name}` is not set")
    return missing


__all__ = ["SpecIndex", "build_graph", "missing_externals"]
