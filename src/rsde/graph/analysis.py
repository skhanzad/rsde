"""Queries over a validated spec graph: ordering, scope, impact and ownership."""

from __future__ import annotations

import heapq
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Iterable, Sequence

from rsde.graph.model import SpecGraph
from rsde.repository.files import GlobSet


def execution_order(graph: SpecGraph, subset: Iterable[str] | None = None) -> list[str]:
    """Topological order in which every spec follows its dependencies and its children.

    Ties are broken by hierarchy post-order, so the result reads like a
    bottom-up walk of the tree, adjusted only where dependencies demand it.
    """
    nodes = set(subset) if subset is not None else set(graph.specs)
    post = {spec_id: i for i, spec_id in enumerate(graph.hierarchy.postorder())}
    prerequisites = {
        n: {p for p in (*graph.dependencies.dependencies(n), *graph.hierarchy.children(n)) if p in nodes}
        for n in nodes
    }
    waiting = {n: len(ps) for n, ps in prerequisites.items()}
    unblocks: dict[str, list[str]] = defaultdict(list)
    for node, prereqs in prerequisites.items():
        for p in prereqs:
            unblocks[p].append(node)
    ready = [(post.get(n, len(post)), n) for n in nodes if waiting[n] == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        _, node = heapq.heappop(ready)
        order.append(node)
        for nxt in unblocks[node]:
            waiting[nxt] -= 1
            if waiting[nxt] == 0:
                heapq.heappush(ready, (post.get(nxt, len(post)), nxt))
    if len(order) < len(nodes):  # only possible in an invalid (cyclic) graph
        order.extend(sorted(nodes - set(order), key=lambda n: post.get(n, len(post))))
    return order


def scope(graph: SpecGraph, target: str) -> list[str]:
    """Every spec that must be satisfied for ``target`` to be satisfied, in execution order."""
    seen: set[str] = set()
    stack = [target]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.hierarchy.children(node))
        stack.extend(graph.dependencies.dependencies(node))
    return execution_order(graph, seen)


@dataclass(frozen=True)
class Impact:
    """Why a spec may be affected by a change."""

    spec_id: str
    kind: str  # "changed" | "intent" | "evidence" | "dependency"
    reasons: tuple[str, ...]
    distance: int


def affected(graph: SpecGraph, changed: Sequence[str]) -> list[Impact]:
    """Specs that may be impacted when the ``changed`` specs change.

    * intent flows down: descendants of a changed spec inherit its intent;
    * evidence flows up: the parent of any affected spec must be re-verified;
    * dependencies propagate: specs that depend on an affected spec are affected.

    The result is in execution order, i.e. the order to re-verify in.
    """
    reasons: dict[str, list[str]] = {}
    kinds: dict[str, str] = {}
    distance: dict[str, int] = {}
    queue: deque[str] = deque()

    def mark(spec_id: str, kind: str, reason: str, dist: int) -> None:
        if spec_id not in reasons:
            reasons[spec_id], kinds[spec_id], distance[spec_id] = [reason], kind, dist
            queue.append(spec_id)
        elif reason not in reasons[spec_id]:
            reasons[spec_id].append(reason)

    for spec_id in changed:
        mark(spec_id, "changed", "changed", 0)
    for spec_id in changed:
        base = graph.hierarchy.depth(spec_id)
        for descendant in graph.hierarchy.descendants(spec_id):
            mark(descendant, "intent", f"inherits intent from `{spec_id}`", graph.hierarchy.depth(descendant) - base)
    while queue:
        spec_id = queue.popleft()
        parent = graph.hierarchy.parent(spec_id)
        if parent is not None:
            mark(parent, "evidence", f"contains `{spec_id}`", distance[spec_id] + 1)
        for edge in graph.dependencies.edges_to(spec_id):
            why = (
                f"requires `{edge.capability}` from `{spec_id}`"
                if edge.kind == "requires"
                else f"depends on `{spec_id}`"
            )
            mark(edge.source, "dependency", why, distance[spec_id] + 1)
    return [Impact(s, kinds[s], tuple(reasons[s]), distance[s]) for s in execution_order(graph, reasons)]


def owners_of(graph: SpecGraph, path: str) -> list[str]:
    """Specs whose ``@implement`` patterns claim ``path``."""
    return [spec_id for spec_id, doc in graph.specs.items() if doc.implement and GlobSet(doc.implement_patterns).matches(path)]


def specs_for_paths(graph: SpecGraph, paths: Iterable[str]) -> tuple[dict[str, list[str]], list[str]]:
    """Map changed workspace paths to the specs they belong to.

    Returns ``({spec_id: [paths…]}, unowned_paths)``. A spec file maps to its
    own spec; any other file maps to the specs that ``@implement`` it.
    """
    by_spec: dict[str, list[str]] = defaultdict(list)
    unowned: list[str] = []
    for path in paths:
        doc = graph.by_path(path)
        owners = [doc.id] if doc is not None else owners_of(graph, path)
        if not owners:
            unowned.append(path)
        for owner in owners:
            by_spec[owner].append(path)
    return dict(by_spec), unowned
