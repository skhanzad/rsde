"""Verification: run a spec's checks and turn the outcome into evidence.

A spec's checks are, in order:

1. the built-in *implementation check* (only if the spec has ``@implement``):
   every declared path or glob must match at least one file;
2. every ``@verify`` command, run with ``/bin/sh`` from the workspace root.

A check passes only if it exits with status 0 within the timeout. All checks
run even after a failure, so an agent sees every problem at once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Sequence

from rsde.process import run_process, tail
from rsde.repository.files import matching_files
from rsde.repository.state import CheckResult
from rsde.syntax.ast import SpecDocument

IMPLEMENTATION_CHECK = "rsde: declared implementation exists"


class Verifier:
    def __init__(self, root: Path, *, timeout: float = 600.0) -> None:
        self.root = root
        self.timeout = timeout

    def implementation_check(self, doc: SpecDocument, files: Sequence[str]) -> CheckResult | None:
        if not doc.implement:
            return None
        missing = [t.pattern for t in doc.implement if not matching_files(files, [t.pattern])]
        if missing:
            return CheckResult(IMPLEMENTATION_CHECK, False, 1, 0.0, "missing: " + ", ".join(missing), builtin=True)
        count = len(matching_files(files, doc.implement_patterns))
        return CheckResult(IMPLEMENTATION_CHECK, True, 0, 0.0, f"{count} file(s) present", builtin=True)

    def run_command(self, doc: SpecDocument, command: str, *, log_path: Path | None = None) -> CheckResult:
        env = {"RSDE_SPEC_ID": doc.id, "RSDE_SPEC_FILE": doc.path, "RSDE_WORKSPACE": str(self.root)}
        result = run_process(command, cwd=self.root, shell=True, env=env, timeout=self.timeout, log_path=log_path)
        output = tail(result.output)
        if result.error:
            output = f"could not run check: {result.error}"
        return CheckResult(command, result.ok, result.exit_code, round(result.duration, 3), output, result.timed_out)

    def verify(
        self,
        doc: SpecDocument,
        files: Sequence[str],
        *,
        on_start: Callable[[str], None] | None = None,
        on_result: Callable[[CheckResult], None] | None = None,
        log_dir: Path | None = None,
        label: str = "verify",
    ) -> list[CheckResult]:
        results: list[CheckResult] = []
        builtin = self.implementation_check(doc, files)
        if builtin is not None:
            results.append(builtin)
            if on_result:
                on_result(builtin)
        for index, check in enumerate(doc.verify, 1):
            if on_start:
                on_start(check.command)
            log_path = None
            if log_dir is not None:
                log_dir.mkdir(parents=True, exist_ok=True)
                log_path = log_dir / f"{doc.id.replace('/', '__')}.{label}.check-{index}.log"
            result = self.run_command(doc, check.command, log_path=log_path)
            results.append(result)
            if on_result:
                on_result(result)
        return results
