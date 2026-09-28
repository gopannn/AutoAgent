"""Fail-closed behaviour of the sandbox, exercised without a Docker daemon."""
import subprocess

import pytest

from verification_compiler.errors import InfrastructureError
from verification_compiler.sandbox import DockerSandbox, materialize_workspace, parse_junit
from verification_compiler.schemas import AcceptanceResult

from . import fakes

MODULES = {"test_AT_a": "AT_a", "test_AT_b": "AT_b"}


def junit(*cases: str) -> str:
    return f'<?xml version="1.0"?><testsuites><testsuite name="pytest">{"".join(cases)}</testsuite></testsuites>'


def test_parse_junit_maps_cases_to_acceptance_ids():
    results, unmapped = parse_junit(junit(
        '<testcase classname="test_AT_a" name="test_one"/>',
        '<testcase classname="test_AT_a" name="test_two"/>',
        '<testcase classname="test_AT_b" name="test_x"><failure message="assert 401 == 200">tb</failure></testcase>',
    ), MODULES)
    assert results["AT_a"] == AcceptanceResult(id="AT_a", passed=True, cases=2)
    assert not results["AT_b"].passed and "assert 401 == 200" in results["AT_b"].failures[0]
    assert unmapped == []


@pytest.mark.parametrize("child", [
    '<error message="collection failure"/>',
    '<skipped message="skipped"/>',
])
def test_errors_and_skips_are_failures(child):
    results, _ = parse_junit(junit(f'<testcase classname="test_AT_a" name="t">{child}</testcase>'), MODULES)
    assert not results["AT_a"].passed


def test_collection_error_without_classname_is_attributed():
    results, _ = parse_junit(junit('<testcase classname="" name="test_AT_b"><error message="ImportError"/></testcase>'),
                             MODULES)
    assert not results["AT_b"].passed


def test_hidden_spec_path_is_scrubbed_from_messages():
    results, _ = parse_junit(junit(
        '<testcase classname="test_AT_a" name="t"><failure message="/compiler_spec/test_AT_a.py:3 boom"/></testcase>'
    ), MODULES)
    assert "/compiler_spec/" not in results["AT_a"].failures[0]


def test_empty_evidence_never_passes():
    result = fakes.passing_result(fakes.codebase(), fakes.spec(), {"sha256": "x"})
    assert result.passed
    assert not result.model_copy(update={"acceptance": []}).passed
    assert not result.model_copy(update={"expected_acceptance_ids": [], "acceptance": []}).passed
    zero_cases = [AcceptanceResult(id="AT_health", passed=True, cases=0)]
    assert not result.model_copy(update={"acceptance": zero_cases}).passed
    assert not result.model_copy(update={"checks": {}}).passed


def test_materialize_rejects_traversal(tmp_path):
    with pytest.raises(ValueError):
        materialize_workspace(tmp_path, [{"path": "../x.py", "content": ""}])


def test_materialize_writes_readonly_files(tmp_path):
    materialize_workspace(tmp_path, fakes.codebase()["files"])
    assert (tmp_path / "service" / "main.py").read_text() == fakes.APP
    assert oct((tmp_path / "service" / "main.py").stat().st_mode & 0o777) == "0o644"


class ScriptedRunner:
    def __init__(self, handler):
        self.handler = handler
        self.commands = []

    def __call__(self, cmd, timeout):
        self.commands.append(cmd)
        return self.handler(cmd)


def ok(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout, "")


def test_missing_docker_is_infrastructure_error_not_host_fallback():
    def handler(cmd):
        raise FileNotFoundError("docker")

    with pytest.raises(InfrastructureError, match="refusing to run generated code"):
        DockerSandbox(fakes.config().sandbox, runner=ScriptedRunner(handler)).preflight()


def test_gvisor_required_by_default():
    runner = ScriptedRunner(lambda cmd: ok('{"runc": {}}'))
    with pytest.raises(InfrastructureError, match="runsc"):
        DockerSandbox(fakes.config().sandbox, runner=runner).preflight()


def test_gvisor_can_be_relaxed_explicitly():
    cfg = fakes.config().sandbox.model_copy(update={"require_gvisor": False})
    runner = ScriptedRunner(lambda cmd: ok('{"runc": {}}'))
    assert DockerSandbox(cfg, runner=runner).preflight() == "runc"


def test_unavailable_image_fails_preflight():
    def handler(cmd):
        if cmd[1] == "info":
            return ok('{"runsc": {}}')
        return subprocess.CompletedProcess(cmd, 1, "", "no such image")

    with pytest.raises(InfrastructureError, match="not available"):
        DockerSandbox(fakes.config().sandbox, runner=ScriptedRunner(handler)).preflight()


def test_hardening_flags():
    flags = DockerSandbox(fakes.config().sandbox)._hardened("runsc", "n", "none")
    for expected in ("--runtime=runsc", "--network=none", "--read-only", "--cap-drop=ALL",
                     "--security-opt=no-new-privileges", "--pull=never", "--pids-limit=256"):
        assert expected in flags
    assert flags[flags.index("--user") + 1] == "65534:65534"


def test_docker_launch_failure_is_infrastructure_error():
    runner = ScriptedRunner(lambda cmd: subprocess.CompletedProcess(cmd, 125, "", "invalid mount"))
    sandbox = DockerSandbox(fakes.config().sandbox, runner=runner)
    with pytest.raises(InfrastructureError, match="failed to launch"):
        sandbox._run_container("vc-x", ["image"], timeout=5)


def test_timeout_kills_container():
    def handler(cmd):
        if cmd[1] == "run":
            raise subprocess.TimeoutExpired(cmd, 5)
        return ok()

    runner = ScriptedRunner(handler)
    proc, timed_out = DockerSandbox(fakes.config().sandbox, runner=runner)._run_container("vc-x", ["image"], timeout=5)
    assert proc is None and timed_out
    assert runner.commands[-1] == ["docker", "rm", "-f", "vc-x"]
