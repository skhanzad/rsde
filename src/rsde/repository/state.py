"""Persistent execution state: verification evidence per spec.

State lives in ``<workspace>/.rsde/state.json``. Each spec's entry is an
:class:`Evidence` record: the fingerprint the spec had when it was verified
and the outcome of every check. Evidence is only trusted while its
fingerprint matches the spec's current fingerprint (see
:mod:`rsde.planning.fingerprint`), so stale evidence can never satisfy a spec.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_VERSION = 1
STATE_FILE = "state.json"
MAX_RUN_HISTORY = 50


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class CheckResult:
    """The outcome of one verification check."""

    command: str
    passed: bool
    exit_code: int | None
    duration: float = 0.0
    output: str = ""
    timed_out: bool = False
    builtin: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CheckResult":
        return cls(
            command=str(data.get("command", "")),
            passed=bool(data.get("passed", False)),
            exit_code=data.get("exit_code"),
            duration=float(data.get("duration", 0.0)),
            output=str(data.get("output", "")),
            timed_out=bool(data.get("timed_out", False)),
            builtin=bool(data.get("builtin", False)),
        )


@dataclass
class Evidence:
    """Verification evidence for one spec, bound to the fingerprint it was produced for."""

    spec_id: str
    fingerprint: str
    passed: bool
    checks: list[CheckResult]
    verified_at: str
    run_id: str = ""
    agent: str | None = None
    attempts: int = 0
    scope_violations: list[str] = field(default_factory=list)

    @property
    def failed_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["checks"] = [c.to_dict() for c in self.checks]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Evidence":
        return cls(
            spec_id=str(data["spec_id"]),
            fingerprint=str(data["fingerprint"]),
            passed=bool(data["passed"]),
            checks=[CheckResult.from_dict(c) for c in data.get("checks", [])],
            verified_at=str(data.get("verified_at", "")),
            run_id=str(data.get("run_id", "")),
            agent=data.get("agent"),
            attempts=int(data.get("attempts", 0)),
            scope_violations=list(data.get("scope_violations", [])),
        )


@dataclass
class RunRecord:
    id: str
    command: str
    target: str
    agent: str
    started_at: str
    finished_at: str = ""
    satisfied: bool = False
    outcome: str = ""


class StateStore:
    """Reads and atomically writes ``.rsde/state.json``."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.path = state_dir / STATE_FILE
        self.evidence: dict[str, Evidence] = {}
        self.runs: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != STATE_VERSION:
                raise ValueError(f"unsupported state version {data.get('version')!r}")
            self.evidence = {k: Evidence.from_dict(v) for k, v in data.get("evidence", {}).items()}
            self.runs = list(data.get("runs", []))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            backup = self.path.with_name(f"{STATE_FILE}.corrupt-{datetime.now():%Y%m%d%H%M%S}")
            try:
                self.path.replace(backup)
            except OSError:
                backup = self.path
            self.warnings.append(
                f"ignored unreadable execution state ({exc}); moved it to {backup}. "
                "All specs will be re-verified."
            )
            self.evidence, self.runs = {}, []

    def get(self, spec_id: str) -> Evidence | None:
        return self.evidence.get(spec_id)

    def put(self, evidence: Evidence) -> None:
        self.evidence[evidence.spec_id] = evidence

    def record_run(self, run: RunRecord) -> None:
        self.runs = [r for r in self.runs if r.get("id") != run.id]
        self.runs.append(asdict(run))
        self.runs = self.runs[-MAX_RUN_HISTORY:]

    def save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "evidence": {k: self.evidence[k].to_dict() for k in sorted(self.evidence)},
            "runs": self.runs,
        }
        fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix=".state-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=False)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def run_dir(self, run_id: str) -> Path:
        path = self.state_dir / "runs" / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path
