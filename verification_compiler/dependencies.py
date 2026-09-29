"""Dependency gate: real, hash-locked resolution plus a vulnerability audit.

Runs on the host with network access but never executes package code:
resolution and downloads are restricted to binary wheels.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from .config import SandboxConfig
from .errors import InfrastructureError
from .hashing import sha256_hex
from .schemas import CheckResult

Runner = Callable[..., subprocess.CompletedProcess]


class DependencyFailure(Exception):
    """The builder's dependency choice is unusable. Repairable."""


_NETWORK_HINTS = ("failed to fetch", "network", "connection", "dns", "timed out", "tls")


def _run(cmd: list[str], timeout: float, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)


class DependencyResolver:
    def __init__(self, cfg: SandboxConfig, runner: Runner = _run):
        self.cfg = cfg
        self.run = runner

    def resolve(self, dependencies: list[str]) -> dict:
        """Returns {"text", "sha256", "packages", "audit"}; raises DependencyFailure or InfrastructureError."""
        if not dependencies:
            empty = CheckResult(name="dependency_scan", passed=True, detail="no dependencies")
            return {"text": "", "sha256": sha256_hex(""), "packages": [], "audit": empty.model_dump()}

        uv = shutil.which("uv")
        auditor = shutil.which("pip-audit")
        if not uv or not auditor:
            raise InfrastructureError("dependency gate requires 'uv' and 'pip-audit' on PATH")

        with tempfile.TemporaryDirectory(prefix="vc-deps-") as tmp:
            req_in = Path(tmp, "requirements.in")
            req_in.write_text("\n".join(sorted(dependencies)) + "\n", encoding="utf-8")
            lock_path = Path(tmp, "requirements.lock")
            proc = self._call([
                uv, "pip", "compile", str(req_in), "-o", str(lock_path),
                "--generate-hashes", "--no-header", "--no-annotate", "--quiet",
                "--python-version", self.cfg.python_version,
                "--python-platform", self.cfg.uv_python_platform,
                "--only-binary", ":all:",
            ], timeout=300)
            if proc.returncode != 0:
                self._raise_for(proc, "dependency resolution failed")
            lock_text = lock_path.read_text(encoding="utf-8")

            audit = self._audit(auditor, lock_path)

        packages = sorted(
            line.split(" ", 1)[0].strip()
            for line in lock_text.splitlines()
            if line and not line.startswith((" ", "#", "\t"))
        )
        return {"text": lock_text, "sha256": sha256_hex(lock_text), "packages": packages, "audit": audit.model_dump()}

    def _audit(self, auditor: str, lock_path: Path) -> CheckResult:
        proc = self._call([
            auditor, "--requirement", str(lock_path), "--require-hashes", "--disable-pip", "--no-deps",
            "--format", "json", "--progress-spinner", "off",
        ], timeout=300)
        try:
            report = json.loads(proc.stdout or "")
        except json.JSONDecodeError:
            self._raise_for(proc, "pip-audit produced no report", infra_only=True)
        vulns = [
            f"{d['name']}=={d.get('version')}: {v['id']} (fix: {', '.join(v.get('fix_versions', [])) or 'none'})"
            for d in report.get("dependencies", [])
            for v in d.get("vulns", [])
        ]
        if proc.returncode not in (0, 1) or (proc.returncode == 1 and not vulns):
            self._raise_for(proc, "pip-audit failed", infra_only=True)
        return CheckResult(
            name="dependency_scan",
            passed=not vulns,
            exit_code=proc.returncode,
            detail="\n".join(vulns) or "no known vulnerabilities",
            log_sha256=sha256_hex(proc.stdout),
        )

    def _call(self, cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
        try:
            return self.run(cmd, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as err:
            raise InfrastructureError(f"{Path(cmd[0]).name} could not run: {err!r}") from err

    @staticmethod
    def _raise_for(proc: subprocess.CompletedProcess, what: str, infra_only: bool = False):
        stderr = (proc.stderr or "").strip()[-2000:]
        if infra_only or any(h in stderr.lower() for h in _NETWORK_HINTS):
            raise InfrastructureError(f"{what} (exit {proc.returncode}): {stderr}")
        raise DependencyFailure(f"{what} (exit {proc.returncode}): {stderr}")
