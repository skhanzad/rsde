"""Fingerprints: a content-addressed identity for everything a spec's evidence depends on.

::

    fp(S) = H( text of S
             , intent S inherits from its ancestors
             , contents of the files S owns (@implement)
             , fp(c) for every child c          — evidence flows up
             , fp(d) for every dependency d )   — and across dependencies

Evidence records the fingerprint it was produced for. When any input changes,
the fingerprint changes and the evidence goes stale, exactly for the specs
that could be affected and for no others. This is a Merkle hash over the
spec graph, so computing it is a single bottom-up pass.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from rsde.graph.analysis import execution_order
from rsde.graph.intent import resolve_intent
from rsde.graph.model import SpecGraph
from rsde.repository.files import FileHasher, list_workspace_files, matching_files


@dataclass(frozen=True)
class Fingerprint:
    spec_id: str
    value: str
    spec_hash: str
    intent_hash: str
    files_hash: str
    files: tuple[str, ...]

    @property
    def short(self) -> str:
        return self.value[:12]


def compute_fingerprints(graph: SpecGraph, files: Sequence[str], hasher: FileHasher) -> dict[str, Fingerprint]:
    fingerprints: dict[str, Fingerprint] = {}
    for spec_id in execution_order(graph):
        doc = graph.specs[spec_id]
        owned = tuple(matching_files(files, doc.implement_patterns))
        files_hash = hasher.digest(owned)
        intent_hash = resolve_intent(graph, spec_id).inherited_digest()
        h = hashlib.sha256()
        h.update(f"spec:{doc.content_hash}\nintent:{intent_hash}\nfiles:{files_hash}\n".encode())
        for child in graph.hierarchy.children(spec_id):
            h.update(f"child:{child}={_value(fingerprints, child)}\n".encode())
        for dep in graph.dependencies.dependencies(spec_id):
            h.update(f"dep:{dep}={_value(fingerprints, dep)}\n".encode())
        fingerprints[spec_id] = Fingerprint(spec_id, h.hexdigest(), doc.content_hash, intent_hash, files_hash, owned)
    return fingerprints


def _value(fingerprints: dict[str, Fingerprint], spec_id: str) -> str:
    fp = fingerprints.get(spec_id)
    return fp.value if fp is not None else "unresolved"  # only in cyclic (invalid) graphs


class Fingerprinter:
    """Recomputes fingerprints on demand, reusing file hashes between calls."""

    def __init__(self, graph: SpecGraph, ignore: Sequence[str] = ()) -> None:
        self.graph = graph
        self.ignore = tuple(ignore)
        self.hasher = FileHasher(graph.root)
        self.files: list[str] = list(graph.files)

    def refresh_files(self) -> list[str]:
        self.files = list_workspace_files(Path(self.graph.root), self.ignore)
        return self.files

    def compute(self, refresh: bool = True) -> dict[str, Fingerprint]:
        if refresh:
            self.refresh_files()
        return compute_fingerprints(self.graph, self.files, self.hasher)
