"""Portable process execution, including descendant cleanup on timeouts."""

import os
import subprocess
import sys
import time
import venv
from pathlib import Path

import rsde

from rsde.process import run_process


def test_timeout_stops_a_child_process_too(tmp_path):
    # A surviving child would write this file after run_process returns.
    child = "import time; from pathlib import Path; time.sleep(3); Path('escaped.txt').write_text('alive')"
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "print('child started', flush=True); time.sleep(30)"
    )
    result = run_process([sys.executable, "-c", parent], cwd=tmp_path, timeout=1)
    assert result.timed_out and "child started" in result.output
    time.sleep(3)
    assert not (tmp_path / "escaped.txt").exists()


def test_python_placeholder_works_with_spaces_in_interpreter_path(tmp_path):
    environment = tmp_path / "python with spaces"
    venv.EnvBuilder().create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    # Run a small driver with the copied interpreter and the installed source
    # location; do not monkeypatch os.name or Path's platform implementation.
    script = tmp_path / "driver.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(Path(rsde.__file__).parent.parent)!r})\n"
        "from pathlib import Path\n"
        "from rsde import parse_document\n"
        "from rsde.verification import Verifier\n"
        "doc, _ = parse_document('# Test', 'master.md')\n"
        "result = Verifier(Path.cwd()).run_command(doc, '{python} -c \"print(123)\"')\n"
        "assert result.passed and result.output == '123', result\n",
        encoding="utf-8",
    )
    result = subprocess.run([str(python), str(script)], cwd=tmp_path, capture_output=True)
    assert result.returncode == 0, result.stderr
