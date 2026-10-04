"""The Spec Graph: a containment hierarchy plus a separate dependency graph.

The two structures answer different questions and are kept apart on purpose:

* :class:`Hierarchy` (built from ``@spec``) is a tree. It says which spec
  *owns* which part of the desired state. Intent (goals, constraints,
  invariants) flows down it; verification evidence flows back up it.
* :class:`DependencyGraph` (built from ``@requires``/``@provides`` and
  ``@depends``) is a DAG over the same specs. It says which spec must be
  satisfied before another can be built and trusted.
"""

from __future__ import annotations

import difflib
import posixpath
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

from rsde.diagnostics import Diagnostic, SourceSpan
from rsde.syntax.ast import SpecDocument


@dataclass(frozen=True)
class DependencyEdge:
    """``source`` needs ``target`` to be satisfied first."""

    source: str
    target: str
    kind: str  # "requires" | "depends"
    span: SourceSpan
    capability: str | None = None

    def describe(self) -> str:
        if self.kind == "requires":
            return f"requires `{self.capability}` from"
        return "depends on"


class Hierarchy:
    """The containment tree built from ``@spec``. Every spec has one parent."""

    def __init__(self, root: str, parents: Mapping[str, str | None], children: Mapping[str, Iterable[str]]) -> None:
        self.root = root
        self._parent = dict(parents)
        self._children = {k: tuple(v) for k, v in children.items()}
        self._preorder: list[str] = []
        stack = [root]
        while stack:
            node = stack.pop()
            self._preorder.append(node)
            stack.extend(reversed(self._children.get(node, ())))
        self._rank = {node: i for i, node in enumerate(self._preorder)}

    def __contains__(self, spec_id: object) -> bool:
        return spec_id in self._rank

    def parent(self, spec_id: str) -> str | None:
        return self._parent.get(spec_id)

    def children(self, spec_id: str) -> tuple[str, ...]:
        return self._children.get(spec_id, ())

    def is_leaf(self, spec_id: str) -> bool:
        return not self._children.get(spec_id)

    def ancestors(self, spec_id: str) -> list[str]:
        """Ancestors, nearest first."""
        out = []
        node = self._parent.get(spec_id)
        while node is not None:
            out.append(node)
            node = self._parent.get(node)
        return out

    def lineage(self, spec_id: str) -> list[str]:
        """The path from the root down to ``spec_id`` (inclusive)."""
        return [*reversed(self.ancestors(spec_id)), spec_id]

    def depth(self, spec_id: str) -> int:
        return len(self.ancestors(spec_id))

    def descendants(self, spec_id: str) -> list[str]:
        """All descendants in pre-order, excluding ``spec_id``."""
        return self.subtree(spec_id)[1:]

    def subtree(self, spec_id: str) -> list[str]:
        out: list[str] = []
        stack = [spec_id]
        while stack:
            node = stack.pop()
            out.append(node)
            stack.extend(reversed(self._children.get(node, ())))
        return out

    def preorder(self) -> list[str]:
        return list(self._preorder)

    def postorder(self) -> list[str]:
        out: list[str] = []

        def visit(node: str) -> None:
            for child in self._children.get(node, ()):
                visit(child)
            out.append(node)

        visit(self.root)
        return out

    def rank(self, spec_id: str) -> int:
        return self._rank.get(spec_id, len(self._rank))


class DependencyGraph:
    """Edges from ``@requires`` (resolved through ``@provides``) and ``@depends``."""

    def __init__(self, edges: Iterable[DependencyEdge], providers: dict[str, str]) -> None:
        self.edges = tuple(edges)
        self.providers = dict(providers)
        self._out: dict[str, list[DependencyEdge]] = defaultdict(list)
        self._in: dict[str, list[DependencyEdge]] = defaultdict(list)
        for edge in self.edges:
            self._out[edge.source].append(edge)
            self._in[edge.target].append(edge)

    def edges_from(self, spec_id: str) -> list[DependencyEdge]:
        return list(self._out.get(spec_id, ()))

    def edges_to(self, spec_id: str) -> list[DependencyEdge]:
        return list(self._in.get(spec_id, ()))

    def dependencies(self, spec_id: str) -> list[str]:
        return list(dict.fromkeys(e.target for e in self._out.get(spec_id, ())))

    def dependents(self, spec_id: str) -> list[str]:
        return list(dict.fromkeys(e.source for e in self._in.get(spec_id, ())))

    def provider(self, capability: str) -> str | None:
        return self.providers.get(capability)

    def consumers(self, capability: str) -> list[str]:
        return list(dict.fromkeys(e.source for e in self.edges if e.capability == capability))


class SpecSelectorError(LookupError):
    """A spec named on the command line does not exist."""


@dataclass
class SpecGraph:
    """Everything RSDE knows about a workspace's specifications."""

    root: Path
    root_id: str
    specs: dict[str, SpecDocument]
    hierarchy: Hierarchy
    dependencies: DependencyGraph
    diagnostics: list[Diagnostic] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.is_error]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if not d.is_error]

    @property
    def ok(self) -> bool:
        return not self.errors

    def spec(self, spec_id: str) -> SpecDocument:
        return self.specs[spec_id]

    def spec_paths(self) -> set[str]:
        return {doc.path for doc in self.specs.values()}

    def by_path(self, rel: str) -> SpecDocument | None:
        for doc in self.specs.values():
            if doc.path == rel:
                return doc
        return None

    def resolve(self, selector: str, cwd: Path | None = None) -> str:
        """Turn a command-line spec selector into a spec id.

        Accepts a file path (relative to ``cwd`` or the workspace), a spec id,
        a wikilink such as ``[[storage]]``, or a unique trailing name.
        """
        raw = selector.strip()
        name = raw[2:-2].split("|")[0].strip() if raw.startswith("[[") and raw.endswith("]]") else raw
        candidates: list[str] = []
        if cwd is not None:
            path = (cwd / name).resolve()
            try:
                candidates.append(path.relative_to(self.root.resolve()).as_posix())
            except ValueError:
                pass
        candidates.append(posixpath.normpath(name.replace("\\", "/")))
        for rel in candidates:
            doc = self.by_path(rel)
            if doc is not None:
                return doc.id
        stem = name.removesuffix(".spec.md").removesuffix(".md")
        if stem in self.specs:
            return stem
        tails = [spec_id for spec_id in self.specs if spec_id.endswith("/" + stem)]
        if len(tails) == 1:
            return tails[0]
        if len(tails) > 1:
            raise SpecSelectorError(f"`{selector}` is ambiguous; it matches {', '.join(sorted(tails))}")
        pool = [*self.specs, *(posixpath.basename(s) for s in self.specs)]
        close = difflib.get_close_matches(stem, pool, n=3)
        hint = f" (did you mean {', '.join(f'`{c}`' for c in close)}?)" if close else ""
        raise SpecSelectorError(f"no spec named `{selector}` in this workspace{hint}")
