"""Workspace files: listing, ownership globs, content hashing and snapshots.

Everything here is deterministic: listings are sorted, hashes are SHA-256 of
file contents, and glob semantics are defined by :func:`compile_pattern`
rather than by the platform.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

#: Directories that never belong to a workspace's implementation.
IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".rsde",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".idea",
        ".eggs",
    }
)
IGNORED_GLOBS = ("**/*.pyc", "**/*.pyo", "**/*.egg-info", "**/.DS_Store")

_HASH_LIMIT = 64 * 1024 * 1024  # larger files are fingerprinted by size and mtime


def glob_to_regex(pattern: str) -> str:
    """Translate a workspace glob into a regular expression (without anchors).

    ``**`` crosses directory boundaries, ``*`` and ``?`` stay inside one path
    segment, and ``[...]`` is a character class (``[!...]`` negates).
    """
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
            elif pattern.startswith("**", i):
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j < 0:
                out.append(re.escape(c))
                i += 1
            else:
                body = pattern[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Compile an ownership pattern.

    A pattern matches a file when it matches the file's path *or any of its
    parent directories*, so ``src/auth/``, ``src/auth`` and ``src/*`` all claim
    everything below ``src/auth``.
    """
    body = glob_to_regex(pattern.rstrip("/"))
    return re.compile(f"^{body}(?:/.*)?$")


class GlobSet:
    """A set of ownership patterns that can be tested against paths."""

    def __init__(self, patterns: Iterable[str]) -> None:
        self.patterns = tuple(patterns)
        self._compiled = [compile_pattern(p) for p in self.patterns]

    def matches(self, path: str) -> bool:
        return any(rx.match(path) for rx in self._compiled)

    def filter(self, paths: Iterable[str]) -> list[str]:
        return [p for p in paths if self.matches(p)]

    def __bool__(self) -> bool:
        return bool(self.patterns)


def matching_files(files: Iterable[str], patterns: Sequence[str]) -> list[str]:
    """Return the files (sorted) claimed by any of ``patterns``."""
    if not patterns:
        return []
    return sorted(GlobSet(patterns).filter(files))


def list_workspace_files(root: Path, ignore: Sequence[str] = ()) -> list[str]:
    """Every file in the workspace as sorted, root-relative POSIX paths.

    Uses ``git ls-files`` when the workspace is inside a Git work tree (so
    ``.gitignore`` is honoured) and falls back to a directory walk otherwise.
    VCS metadata, RSDE state, caches and ``ignore`` globs are always skipped.
    """
    files = _git_files(root)
    if files is None:
        files = _walk_files(root)
    skip = GlobSet((*IGNORED_GLOBS, *ignore))
    result = []
    for rel in files:
        parts = rel.split("/")
        if any(part in IGNORED_DIRS for part in parts[:-1]) or skip.matches(rel):
            continue
        result.append(rel)
    return sorted(set(result))


def _git_files(root: Path) -> list[str] | None:
    """Files according to git, or None when git cannot describe this workspace."""
    if shutil.which("git") is None:
        return None
    try:
        ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "."], capture_output=True, timeout=60)
        if ignored.returncode == 0:
            return None  # the workspace itself is git-ignored: git would hide every file
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    paths = proc.stdout.decode("utf-8", "surrogateescape").split("\0")
    files = [p for p in paths if p and (root / p).is_file()]
    return files or None


def _walk_files(root: Path) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        rel_dir = os.path.relpath(dirpath, root)
        for name in filenames:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            out.append(rel.replace(os.sep, "/"))
    return out


class FileHasher:
    """Content hashes with a (size, mtime) cache so repeated fingerprints are cheap."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[str, tuple[int, int, str]] = {}

    def hash_file(self, rel: str) -> str:
        path = self.root / rel
        try:
            st = path.stat()
        except OSError:
            return "missing"
        cached = self._cache.get(rel)
        if cached is not None and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
            return cached[2]
        if st.st_size > _HASH_LIMIT:
            digest = f"large:{st.st_size}:{st.st_mtime_ns}"
        else:
            h = hashlib.sha256()
            try:
                with path.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
            except OSError:
                return "unreadable"
            digest = h.hexdigest()
        self._cache[rel] = (st.st_size, st.st_mtime_ns, digest)
        return digest

    def digest(self, rels: Iterable[str]) -> str:
        """One hash over the names and contents of ``rels``."""
        h = hashlib.sha256()
        for rel in sorted(rels):
            h.update(f"{rel}\0{self.hash_file(rel)}\n".encode("utf-8", "surrogateescape"))
        return h.hexdigest()


@dataclass(frozen=True)
class Changes:
    """Files that differ between two snapshots."""

    created: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted({*self.created, *self.modified, *self.deleted}))

    def __bool__(self) -> bool:
        return bool(self.created or self.modified or self.deleted)

    def __len__(self) -> int:
        return len(self.paths)

    def to_dict(self) -> dict[str, list[str]]:
        return {"created": list(self.created), "modified": list(self.modified), "deleted": list(self.deleted)}


Snapshot = dict[str, str]


def take_snapshot(root: Path, hasher: FileHasher, ignore: Sequence[str] = ()) -> Snapshot:
    return {rel: hasher.hash_file(rel) for rel in list_workspace_files(root, ignore)}


def diff_snapshots(before: Snapshot, after: Snapshot) -> Changes:
    created = tuple(sorted(p for p in after if p not in before))
    deleted = tuple(sorted(p for p in before if p not in after))
    modified = tuple(sorted(p for p in after if p in before and after[p] != before[p]))
    return Changes(created, modified, deleted)
