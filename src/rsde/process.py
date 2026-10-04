"""Subprocess execution shared by verification and agent adapters.

Timeouts terminate the process tree: a session on POSIX, or ``taskkill /T``
on Windows, including the test runner and everything it spawned.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

OUTPUT_TAIL_LINES = 80
OUTPUT_TAIL_CHARS = 8000


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int | None
    output: str
    duration: float
    timed_out: bool = False
    error: str | None = None  # the process could not be started

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None


def tail(text: str, lines: int = OUTPUT_TAIL_LINES, chars: int = OUTPUT_TAIL_CHARS) -> str:
    """The last ``lines`` lines of ``text``, capped at ``chars`` characters."""
    kept = text.rstrip("\n").splitlines()[-lines:]
    out = "\n".join(kept)
    if len(out) > chars:
        out = "…" + out[-chars:]
    return out


def run_process(
    args: str | Sequence[str],
    *,
    cwd: Path,
    shell: bool = False,
    env: Mapping[str, str] | None = None,
    unset_env: Sequence[str] = (),
    timeout: float | None = None,
    stdin_text: str | None = None,
    on_output: Callable[[str], None] | None = None,
    log_path: Path | None = None,
) -> ProcessResult:
    """Run a process, capturing combined stdout/stderr, with a hard timeout."""
    started = time.monotonic()
    full_env = {k: v for k, v in os.environ.items() if k not in unset_env}
    full_env.setdefault("PYTHONIOENCODING", "utf-8")
    full_env.update(env or {})
    try:
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            shell=shell,
            env=full_env,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )
    except (OSError, ValueError) as exc:
        name = args if isinstance(args, str) else (args[0] if args else "")
        reason = "command not found" if isinstance(exc, FileNotFoundError) else str(exc)
        return ProcessResult(None, f"{name}: {reason}", time.monotonic() - started, error=reason)

    chunks: list[str] = []
    log = log_path.open("w", encoding="utf-8") if log_path is not None else None

    def pump() -> None:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").replace("\r\n", "\n")
            chunks.append(line)
            if log is not None:
                log.write(line)
                log.flush()
            if on_output is not None:
                on_output(line.rstrip("\n"))
        proc.stdout.close()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    if stdin_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_text.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
    except KeyboardInterrupt:
        _kill_tree(proc)
        raise
    finally:
        reader.join(timeout=5)
        if log is not None:
            log.close()

    output = "".join(chunks)
    if timed_out:
        output += f"\n[rsde] timed out after {timeout:g}s; process killed\n"
    return ProcessResult(proc.returncode if not timed_out else None, output, time.monotonic() - started, timed_out)


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        # terminate()/kill() only stop the immediate process on Windows. Kill
        # descendants before their parent disappears and they become orphaned.
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            proc.wait(timeout=5)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass  # Fall back to terminating the immediate process.
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=5)
    except (ProcessLookupError, PermissionError):
        pass
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
            proc.wait(timeout=5)
        except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
            pass
