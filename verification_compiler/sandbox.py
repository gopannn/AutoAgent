"""Fail-closed, two-container verification.

Trust layout:
  * static container  : verifier image, network none, workspace read-only. Runs ruff/pyright/semgrep.
  * SUT container     : runtime image, internal network only, workspace read-only. Runs the service.
  * oracle container  : verifier image, same internal network, never sees generated code.
                        Runs the hidden acceptance tests over HTTP and alone writes the verdict.

The generated code shares no process, filesystem or writable mount with whatever
produces evidence, so it cannot forge a result. Any step that cannot run is a
failure: there is no host fallback, and an empty or missing report never passes.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import SandboxConfig
from .errors import InfrastructureError
from .hashing import codebase_hash, hash_obj, sha256_hex
from .policy import validate_repo_path
from .schemas import AcceptanceResult, CheckResult, VerificationResult
from .static_checks import secret_scan, syntax_check

HARNESS_DIR = Path(__file__).parent / "harness"
LOG_TAIL = 4000
STATIC_TOOLS = ("ruff", "pyright", "semgrep")
_DOCKER_LAUNCH_FAILURE = (125, 126, 127)
# Exit codes meaning the tool or its configuration is broken, not the code under analysis.
_TOOL_BROKEN_EXIT = {
    "ruff": {2, 126, 127},
    "pyright": {3, 4, 126, 127},
    "semgrep": {4, 5, 7, 126, 127},
}

Runner = Callable[..., subprocess.CompletedProcess]


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _tail(text: str, limit: int = LOG_TAIL) -> str:
    return text if len(text) <= limit else "...[truncated]...\n" + text[-limit:]


def spec_module(test_id: str) -> str:
    return f"test_{test_id}"


# ---------------------------------------------------------------------- materialisation

def materialize_workspace(root: Path, files: list[dict]) -> None:
    root_real = root.resolve()
    for f in files:
        rel = validate_repo_path(f["path"])        # defence in depth; the policy gate already ran
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.parent.resolve().is_relative_to(root_real):
            raise InfrastructureError(f"refusing to write outside workspace: {f['path']}")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f["content"])
    _make_world_readable(root)


def materialize_spec(root: Path, spec: dict) -> dict[str, str]:
    """Writes one module per acceptance test. Returns module name -> acceptance test id."""
    modules = {}
    for test in spec["acceptance_tests"]:
        module = spec_module(test["id"])
        (root / f"{module}.py").write_text(test["executable_python_code"], encoding="utf-8")
        modules[module] = test["id"]
    _make_world_readable(root)
    return modules


def _make_world_readable(root: Path) -> None:
    # Containers run as nobody (65534); inputs must be readable, never writable.
    for dirpath, dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o755)
        for name in filenames:
            os.chmod(os.path.join(dirpath, name), 0o644)


# ---------------------------------------------------------------------- report parsing

def parse_junit(xml_text: str, modules: dict[str, str]) -> tuple[dict[str, AcceptanceResult], list[str]]:
    """Maps junit testcases onto acceptance test ids. Skips and errors count as failures."""
    root = ET.fromstring(xml_text)
    cases: dict[str, list[str | None]] = {}
    unmapped: list[str] = []
    for case in root.iter("testcase"):
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        module = (classname or name).split(".")[0]
        outcome = None
        for tag in ("failure", "error", "skipped"):
            node = case.find(tag)
            if node is not None:
                message = (node.get("message") or node.text or tag).strip().splitlines()
                first = message[0] if message else tag
                outcome = f"{name}: {tag}: {first.replace('/compiler_spec/', '')[:300]}"
                break
        if module in modules:
            cases.setdefault(modules[module], []).append(outcome)
        else:
            unmapped.append(outcome or f"{classname}.{name}: passed (unmapped)")
    results = {
        test_id: AcceptanceResult(
            id=test_id,
            passed=bool(outcomes) and all(o is None for o in outcomes),
            cases=len(outcomes),
            failures=[o for o in outcomes if o is not None],
        )
        for test_id, outcomes in cases.items()
    }
    return results, unmapped


def _read_exit(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ---------------------------------------------------------------------- sandbox

@dataclass
class _Layout:
    workspace: Path
    spec: Path
    harness: Path
    out: Path
    deps: Path


class DockerSandbox:
    def __init__(self, cfg: SandboxConfig, runner: Runner = _run, docker: str = "docker"):
        self.cfg = cfg
        self.run = runner
        self.docker = docker

    # ----------------------------------------------------------- infrastructure

    def _docker(self, args: list[str], timeout: float) -> subprocess.CompletedProcess:
        try:
            return self.run([self.docker, *args], timeout=timeout)
        except FileNotFoundError as err:
            raise InfrastructureError("docker CLI not found; refusing to run generated code without a sandbox") from err
        except OSError as err:
            raise InfrastructureError(f"docker could not be executed: {err!r}") from err

    def preflight(self) -> str:
        """Checks the sandbox can run at all and returns the OCI runtime in use."""
        proc = self._docker(["info", "--format", "{{json .Runtimes}}"], timeout=30)
        if proc.returncode != 0:
            raise InfrastructureError(f"docker daemon unavailable: {proc.stderr.strip()[:500]}")
        try:
            runtimes = json.loads(proc.stdout or "{}") or {}
        except json.JSONDecodeError as err:
            raise InfrastructureError("could not read docker runtimes") from err
        if "runsc" in runtimes:
            runtime = "runsc"
        elif self.cfg.require_gvisor:
            raise InfrastructureError("gVisor runtime 'runsc' is not registered with Docker (VC_REQUIRE_GVISOR=1)")
        else:
            runtime = "runc"
        for image in (self.cfg.verifier_image, self.cfg.runtime_image):
            if self._docker(["image", "inspect", "--format", "{{.Id}}", image], timeout=30).returncode == 0:
                continue
            if "@" not in image or self._docker(["pull", "-q", image], timeout=600).returncode != 0:
                raise InfrastructureError(f"image {image} is not available")
        return runtime

    def _hardened(self, runtime: str, name: str, network: str) -> list[str]:
        c = self.cfg
        return [
            "--name", name, f"--runtime={runtime}", f"--network={network}", "--pull=never",
            "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user", "65534:65534",
            f"--memory={c.memory}", f"--memory-swap={c.memory}", f"--cpus={c.cpus}",
            f"--pids-limit={c.pids_limit}",
            "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--label", "verification-compiler=1",
        ]

    def _run_container(self, name: str, args: list[str], timeout: float) -> tuple[subprocess.CompletedProcess | None, bool]:
        """Runs `docker run --rm ...`; on timeout the container is killed. Returns (proc, timed_out).

        Exit codes 125-127 come from Docker itself (container could not be created or its command
        could not be executed) and are infrastructure faults, never the generated code's fault.
        """
        try:
            proc = self._docker(["run", "--rm", *args], timeout=timeout)
        except subprocess.TimeoutExpired:
            self._docker(["rm", "-f", name], timeout=60)
            return None, True
        if proc.returncode in _DOCKER_LAUNCH_FAILURE:
            raise InfrastructureError(f"container {name} failed to launch (exit {proc.returncode}): {proc.stderr.strip()[:500]}")
        return proc, False

    # ----------------------------------------------------------- wheelhouse

    def wheelhouse(self, lockfile: dict) -> Path | None:
        """Content-addressed cache of binary wheels for a lockfile. Downloads never run package code."""
        if not lockfile["text"]:
            return None
        target = self.cfg.cache_dir / "wheelhouse" / lockfile["sha256"]
        if (target / ".complete").exists():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="staging-", dir=target.parent))
        lock_path = staging / "requirements.lock"
        lock_path.write_text(lockfile["text"], encoding="utf-8")
        platforms = [self.cfg.wheel_platform, "manylinux2014_x86_64", "manylinux_2_17_x86_64", "any"]
        cmd = [
            sys.executable, "-m", "pip", "download", "--require-hashes", "--no-deps", "--only-binary=:all:",
            "--python-version", self.cfg.python_version, "--implementation", "cp",
            *[arg for p in platforms for arg in ("--platform", p)],
            "--dest", str(staging), "--requirement", str(lock_path), "--disable-pip-version-check", "--quiet",
        ]
        try:
            proc = self.run(cmd, timeout=900)
        except (OSError, subprocess.SubprocessError) as err:
            shutil.rmtree(staging, ignore_errors=True)
            raise InfrastructureError(f"wheel download could not run: {err!r}") from err
        if proc.returncode != 0:
            shutil.rmtree(staging, ignore_errors=True)
            raise InfrastructureError(f"wheel download failed: {proc.stderr.strip()[-1500:]}")
        lock_path.unlink()
        (staging / ".complete").write_text(lockfile["sha256"])
        _make_world_readable(staging)
        try:
            staging.rename(target)
        except OSError:
            shutil.rmtree(staging, ignore_errors=True)   # a concurrent build won the race
        return target

    # ----------------------------------------------------------- verification

    def verify(self, codebase: dict, spec: dict, lockfile: dict) -> VerificationResult:
        runtime = self.preflight()
        files = codebase["files"]
        spec_ids = [t["id"] for t in spec["acceptance_tests"]]
        cb_hash = codebase_hash(codebase)
        key = f"{cb_hash[:10]}-{secrets.token_hex(3)}"

        checks: dict[str, CheckResult] = {
            "syntax": syntax_check(files),
            "secret_scan": secret_scan(files),
            "dependency_scan": CheckResult(**lockfile["audit"]),
        }
        logs: dict[str, str] = {}
        versions: dict[str, str] = {}
        acceptance: dict[str, AcceptanceResult] = {}
        wheels = self.wheelhouse(lockfile)

        with tempfile.TemporaryDirectory(prefix="vc-", ignore_cleanup_errors=True) as tmp:
            lay = _Layout(*(Path(tmp, d) for d in ("workspace", "spec", "harness", "out", "deps")))
            for d in (lay.workspace, lay.spec, lay.harness, lay.out, lay.deps):
                d.mkdir()
            materialize_workspace(lay.workspace, files)
            modules = materialize_spec(lay.spec, spec)
            shutil.copytree(HARNESS_DIR, lay.harness, dirs_exist_ok=True)
            (lay.harness / "requirements.lock").write_text(lockfile["text"], encoding="utf-8")
            _make_world_readable(lay.harness)
            os.chmod(lay.out, 0o777)
            os.chmod(lay.deps, 0o777)

            checks["dependency_install"] = self._install(lay, wheels, runtime, key, logs)
            checks.update(self._static(lay, runtime, key, logs, versions))

            if checks["syntax"].passed and checks["dependency_install"].passed:
                dyn_checks, acceptance = self._dynamic(lay, runtime, key, codebase["entrypoint"], modules, logs, versions)
                checks.update(dyn_checks)
            else:
                checks["acceptance_tests"] = CheckResult(
                    name="acceptance_tests", passed=False,
                    detail="not run: the service cannot start until syntax and dependency installation pass",
                )

            evidence = {
                p.name: sha256_hex(p.read_bytes())
                for p in sorted(lay.out.iterdir()) if p.is_file()
            }

        return VerificationResult(
            checks=checks,
            acceptance=[
                acceptance.get(i) or AcceptanceResult(id=i, passed=False, cases=0, failures=["not executed"])
                for i in spec_ids
            ],
            expected_acceptance_ids=spec_ids,
            runtime=runtime,
            images={"verifier": self.cfg.verifier_image, "runtime": self.cfg.runtime_image},
            toolchain_versions=versions,
            codebase_hash=cb_hash,
            lockfile_hash=lockfile["sha256"],
            spec_hash=hash_obj(spec),
            evidence_hashes=evidence,
            logs=logs,
        )

    def _install(self, lay: _Layout, wheels: Path | None, runtime: str, key: str, logs: dict) -> CheckResult:
        if wheels is None:
            return CheckResult(name="dependency_install", passed=True, exit_code=0, detail="no dependencies")
        name = f"vc-install-{key}"
        proc, timed_out = self._run_container(name, [
            *self._hardened(runtime, name, "none"),
            "-v", f"{wheels}:/wheelhouse:ro", "-v", f"{lay.harness}:/harness:ro", "-v", f"{lay.deps}:/deps:rw",
            self.cfg.runtime_image,
            "python", "-m", "pip", "install", "--no-index", "--find-links", "/wheelhouse", "--require-hashes",
            "--no-deps", "--only-binary=:all:", "--no-cache-dir", "--disable-pip-version-check",
            "--target", "/deps", "-r", "/harness/requirements.lock",
        ], timeout=self.cfg.install_timeout_s)
        if timed_out:
            return CheckResult(name="dependency_install", passed=False, detail="timed out")
        output = proc.stdout + proc.stderr
        logs["dependency_install"] = _tail(output)
        return CheckResult(
            name="dependency_install", passed=proc.returncode == 0, exit_code=proc.returncode,
            detail="" if proc.returncode == 0 else _tail(output, 1500), log_sha256=sha256_hex(output),
        )

    def _static(self, lay: _Layout, runtime: str, key: str, logs: dict, versions: dict) -> dict[str, CheckResult]:
        name = f"vc-static-{key}"
        proc, timed_out = self._run_container(name, [
            *self._hardened(runtime, name, "none"),
            "-v", f"{lay.workspace}:/workspace:ro", "-v", f"{lay.deps}:/deps:ro",
            "-v", f"{lay.harness}:/harness:ro", "-v", f"{lay.out}:/out:rw",
            self.cfg.verifier_image, "sh", "/harness/static.sh",
        ], timeout=self.cfg.static_timeout_s)
        if proc is not None and proc.returncode != 0:
            raise InfrastructureError(f"static analysis harness failed (exit {proc.returncode}): {proc.stderr.strip()[:500]}")

        results = {}
        for tool in STATIC_TOOLS:
            code = _read_exit(lay.out / f"{tool}.exit")
            output = _read(lay.out / f"{tool}.log")
            if code in _TOOL_BROKEN_EXIT[tool]:
                raise InfrastructureError(f"{tool} failed to run (exit {code}): {_tail(output, 800)}")
            logs[tool] = _tail(output)
            passed = code == 0
            detail = "timed out" if code is None and timed_out else ("did not run" if code is None else "")
            results[tool] = CheckResult(
                name=tool, passed=passed, exit_code=code,
                detail=detail or ("" if passed else _tail(output, 1500)), log_sha256=sha256_hex(output),
            )
        versions.update(self._versions(lay.out / "versions_static.json"))
        return results

    def _dynamic(self, lay: _Layout, runtime: str, key: str, entrypoint: str, modules: dict[str, str],
                 logs: dict, versions: dict) -> tuple[dict[str, CheckResult], dict[str, AcceptanceResult]]:
        net, sut, oracle = f"vc-net-{key}", f"vc-sut-{key}", f"vc-oracle-{key}"
        created = self._docker(["network", "create", "--internal", "--label", "verification-compiler=1", net], 60)
        if created.returncode != 0:
            raise InfrastructureError(f"could not create isolated network: {created.stderr.strip()[:500]}")
        timed_out = False
        try:
            started = self._docker([
                "run", "-d", *self._hardened(runtime, sut, net), "--network-alias", "sut",
                "-v", f"{lay.workspace}:/workspace:ro", "-v", f"{lay.deps}:/deps:ro",
                "--env", "PYTHONPATH=/deps:/workspace", "-w", "/workspace",
                self.cfg.runtime_image,
                "python", "-m", "uvicorn", entrypoint, "--host", "0.0.0.0", "--port", str(self.cfg.sut_port),
            ], timeout=120)
            if started.returncode != 0:
                raise InfrastructureError(f"could not start service container: {started.stderr.strip()[:500]}")

            _, timed_out = self._run_container(oracle, [
                *self._hardened(runtime, oracle, net),
                "-v", f"{lay.spec}:/compiler_spec:ro", "-v", f"{lay.harness}:/harness:ro", "-v", f"{lay.out}:/out:rw",
                "--env", f"SUT_BASE_URL=http://sut:{self.cfg.sut_port}",
                "--env", f"READY_TIMEOUT={self.cfg.sut_ready_timeout_s}",
                self.cfg.verifier_image, "sh", "/harness/oracle.sh",
            ], timeout=self.cfg.sut_ready_timeout_s + self.cfg.oracle_timeout_s)

            sut_logs = self._docker(["logs", sut], timeout=60)
            logs["service"] = _tail(sut_logs.stdout + sut_logs.stderr)
        finally:
            self._docker(["rm", "-f", sut, oracle], timeout=60)
            self._docker(["network", "rm", net], timeout=60)

        checks: dict[str, CheckResult] = {}
        ready = _read_exit(lay.out / "ready.exit")
        checks["service_startup"] = CheckResult(
            name="service_startup", passed=ready == 0, exit_code=ready,
            detail="" if ready == 0 else (_read(lay.out / "ready.log").strip() + "\n" + logs.get("service", ""))[-2000:],
        )
        versions.update({f"oracle_{k}": v for k, v in self._versions(lay.out / "versions_oracle.json").items()})

        pytest_exit = _read_exit(lay.out / "pytest.exit")
        pytest_log = _read(lay.out / "pytest.log")
        logs["acceptance_tests"] = f"pytest exit {pytest_exit}"   # the full log may quote hidden test source
        acceptance: dict[str, AcceptanceResult] = {}
        unmapped: list[str] = []
        junit = lay.out / "junit.xml"
        if junit.is_file():
            try:
                acceptance, unmapped = parse_junit(junit.read_text(encoding="utf-8"), modules)
            except ET.ParseError:
                acceptance, unmapped = {}, ["junit report unreadable"]
        all_ok = (
            pytest_exit == 0
            and not [u for u in unmapped if "passed (unmapped)" not in u]
            and all(acceptance.get(i) and acceptance[i].passed for i in modules.values())
        )
        if timed_out:
            detail = "acceptance run timed out"
        elif ready != 0:
            detail = "not run: service never became ready"
        elif pytest_exit is None:
            detail = "pytest produced no exit status"
        else:
            detail = "" if all_ok else "\n".join(unmapped)
        checks["acceptance_tests"] = CheckResult(
            name="acceptance_tests", passed=all_ok, exit_code=pytest_exit, detail=detail,
            log_sha256=sha256_hex(pytest_log),
        )
        return checks, acceptance

    @staticmethod
    def _versions(path: Path) -> dict[str, str]:
        try:
            data = json.loads(path.read_text())
            return {str(k): str(v) for k, v in data.items()}
        except (OSError, ValueError, AttributeError):
            return {}
