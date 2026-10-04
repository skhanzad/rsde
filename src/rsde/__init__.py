"""RSDE — Recursive Spec-Driven Engineering.

A repository is described by a tree of Markdown specification files rooted at
``master.md``. RSDE parses them into a typed AST, links them into a spec graph
(a containment hierarchy plus a separate dependency graph), validates the
graph deterministically, and drives interchangeable coding agents until every
spec is backed by passing verification evidence.
"""

__version__ = "0.1.0"
