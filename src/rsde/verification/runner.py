"""Verification: run a spec's checks and turn the outcome into evidence.

A spec's checks are, in order:

1. the built-in *implementation check* (only if the spec has ``@implement``):
   every declared path or glob must match at least one file;
2. every ``@verify`` command, run from the workspace root with the host shell
   (``/bin/sh`` on POSIX, ``cmd.exe`` on Windows). ``{python}`` expands to the
   quoted Python executable running RSDE.

A check passes only if it exits with status 0 within the timeout. All checks
run even after a failure, so an agent sees every problem at once.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Sequence

from rsde.process import ProcessResult, run_process, tail
from rsde.repository.files import matching_files
from rsde.repository.state import CheckResult
from rsde.syntax.ast import SpecDocument

IMPLEMENTATION_CHECK = "rsde: declared implementation exists"


def expand_verify_command(command: str) -> str:
    """Resolve portable placeholders using quoting for the host shell."""
    python = subprocess.list2cmdline([sys.executable]) if os.name == "nt" else shlex.quote(sys.executable)
    return command.replace("{python}", python)


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
        expanded = expand_verify_command(command)

        def run(script: str) -> ProcessResult:
            return run_process(script, cwd=self.root, shell=True, env=env, timeout=self.timeout, log_path=log_path)

        if os.name == "nt" and "\n" in expanded:
            # cmd /c does not execute embedded newlines as a batch script.
            # Keep the temporary script outside the tracked workspace.
            with tempfile.TemporaryDirectory(prefix="rsde-verify-") as directory:
                script = Path(directory) / "verify.cmd"
                script.write_text("@echo off\n" + expanded + "\n", encoding="utf-8")
                result = run(subprocess.list2cmdline([str(script)]))
        else:
            result = run(expanded)
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
