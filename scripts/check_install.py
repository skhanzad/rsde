"""Smoke-test distribution installs without importing from the source checkout.

Run ``python scripts/check_install.py dist`` after ``python -m build``.
Each artifact gets a fresh virtual environment in a path containing spaces.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import venv
from pathlib import Path


def run(argv: list[str], cwd: Path) -> str:
    result = subprocess.run(
        argv,
        cwd=cwd,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    if result.returncode:
        raise RuntimeError(f"{argv!r} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
    return result.stdout


def check(artifact: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="rsde install ") as directory:
        root = Path(directory)
        environment = root / "environment with spaces"
        venv.EnvBuilder(with_pip=True).create(environment)
        bin_dir = environment / ("Scripts" if os.name == "nt" else "bin")
        python = str(bin_dir / ("python.exe" if os.name == "nt" else "python"))
        cli = str(bin_dir / ("rsde.exe" if os.name == "nt" else "rsde"))
        run([python, "-m", "pip", "install", "--disable-pip-version-check", str(artifact)], root)

        version = run([python, "-m", "rsde", "--version"], root).strip()
        assert run([cli, "--version"], root).strip() == version
        run([cli, "init", "sample project"], root)
        project = root / "sample project"
        (project / "master.md").write_text(
            "# Installed package\n\n@goal Verify an installed RSDE package.\n"
            "@implement check.py\n@verify {python} check.py\n",
            encoding="utf-8",
        )
        (project / "check.py").write_text(
            '''from importlib.metadata import version
from importlib.resources import files
from pathlib import Path
from rsde import SpecDocument, SpecGraph, __version__, build_graph, parse_document

assert __version__ == version("rsde")
assert files("rsde").joinpath("py.typed").is_file()
document, diagnostics = parse_document("# A spec\\n@goal Parse it.\\n", "a.spec.md")
assert isinstance(document, SpecDocument) and document.id == "a" and not diagnostics
graph = build_graph(Path.cwd())
assert isinstance(graph, SpecGraph) and graph.ok and graph.root_id == "master"
''',
            encoding="utf-8",
        )
        run([cli, "check", "--strict"], project)
        run([cli, "execute", "--verify-only"], project)
        status = json.loads(run([cli, "status", "--format", "json"], project))
        assert status["satisfied"]
        print(f"PASS {artifact.name}: library imports, CLI, verification, and paths with spaces", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="a wheel, source archive, or distribution directory")
    path = parser.parse_args().path.resolve()
    artifacts = sorted([*path.glob("*.whl"), *path.glob("*.tar.gz")]) if path.is_dir() else [path]
    if not artifacts or any(not artifact.is_file() for artifact in artifacts):
        parser.error(f"no distributions found at {path}")
    for artifact in artifacts:
        check(artifact)


if __name__ == "__main__":
    main()
