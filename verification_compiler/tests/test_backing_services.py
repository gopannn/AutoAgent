"""Backing services (Redis) without a Docker daemon: config, topology, ACLs, reset, governance and gating."""
from __future__ import annotations

import importlib.util
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from verification_compiler import prompts
from verification_compiler.config import SandboxConfig
from verification_compiler.errors import InfrastructureError
from verification_compiler.nodes import CompilerNodes
from verification_compiler.reasoning import governance
from verification_compiler.sandbox import HARNESS_DIR, DockerSandbox

from . import fakes

REDIS = "sha256:" + "b" * 64


def sandbox_cfg(**kw) -> SandboxConfig:
    return SandboxConfig(verifier_image=fakes.IMAGE, runtime_image=fakes.IMAGE, **kw)


def ok(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout, "")


class Docker:
    """Scripted docker CLI: every container gets its own IP, Redis answers PONG."""

    def __init__(self, redis_ready=True):
        self.commands: list[list[str]] = []
        self.ips: dict[str, str] = {}
        self.redis_ready = redis_ready

    def __call__(self, cmd, timeout):
        self.commands.append(cmd)
        verb = cmd[1]
        if verb == "inspect":
            name = cmd[-1]
            return ok(self.ips.setdefault(name, f"172.18.0.{len(self.ips) + 2}"))
        if verb == "exec" and cmd[-1] == "ping":
            return ok("PONG\n") if self.redis_ready else subprocess.CompletedProcess(cmd, 1, "", "LOADING")
        if verb == "exec" and cmd[-1] == "--version":
            return ok("Redis server v=7.4.11 sha=00000000:0 malloc=jemalloc-5.3.0 bits=64\n")
        if verb == "logs":
            return ok(f"log of {cmd[-1]}")
        return ok("container-id")

    def runs(self, prefix):
        return [c for c in self.commands if c[1] == "run" and c[c.index("--name") + 1].startswith(prefix)]


def probe(docker, services, tmp_path, **cfg):
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    sandbox = DockerSandbox(sandbox_cfg(redis_image=REDIS, **cfg), runner=docker)
    return sandbox._serve_and_probe("runsc", "k1", ["image", "serve"], tmp_path, tmp_path, out, services), out


def env_of(cmd) -> dict[str, str]:
    pairs = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--env"]
    return dict(p.split("=", 1) for p in pairs)


# ---------------------------------------------------------------- configuration

def test_redis_image_must_be_digest_pinned():
    with pytest.raises(ValueError, match="pinned by digest"):
        sandbox_cfg(redis_image="redis:7")
    assert sandbox_cfg().service_images() == {}
    assert sandbox_cfg(redis_image=REDIS).service_images() == {"redis": REDIS}


def test_from_env_reads_redis_settings(monkeypatch):
    monkeypatch.setenv("VC_VERIFIER_IMAGE", fakes.IMAGE)
    monkeypatch.setenv("VC_RUNTIME_IMAGE", fakes.IMAGE)
    monkeypatch.setenv("VC_REDIS_IMAGE", REDIS)
    monkeypatch.setenv("VC_STATEFUL_REPLICAS", "3")
    cfg = SandboxConfig.from_env()
    assert cfg.redis_image == REDIS and cfg.stateful_replicas == 3


@pytest.mark.parametrize("spec_services, cfg, match", [
    (["memcached"], {"redis_image": REDIS}, "unsupported"),
    (["redis"], {}, "no image configured"),
])
def test_unknown_or_unconfigured_services_fail_closed(spec_services, cfg, match):
    with pytest.raises(InfrastructureError, match=match):
        DockerSandbox(sandbox_cfg(**cfg)).services({"backing_services": spec_services})


def test_preflight_requires_the_redis_image():
    def handler(cmd, timeout):
        if cmd[1] == "info":
            return ok('{"runsc": {}}')
        if cmd[1] == "image" and cmd[-1] == REDIS:
            return subprocess.CompletedProcess(cmd, 1, "", "no such image")
        return ok("sha256:x")

    sandbox = DockerSandbox(sandbox_cfg(redis_image=REDIS), runner=handler)
    assert sandbox.preflight() == "runsc"          # not needed, not checked
    with pytest.raises(InfrastructureError, match=REDIS):
        sandbox.preflight(["redis"])


# ---------------------------------------------------------------- topology

def test_stateless_topology_is_unchanged(tmp_path):
    docker = Docker()
    (timed_out, log, versions), _ = probe(docker, (), tmp_path)
    assert not timed_out and versions == {} and log == "log of vc-sut-k1"
    assert [c[c.index("--name") + 1] for c in docker.runs("vc-sut")] == ["vc-sut-k1"]
    assert not docker.runs("vc-redis")
    oracle_env = env_of(docker.runs("vc-oracle")[0])
    assert "SUT_REPLICA_URLS" not in oracle_env and "VC_REDIS_ADDR" not in oracle_env
    assert "REDIS_URL" not in env_of(docker.runs("vc-sut")[0])


def test_redis_topology(tmp_path):
    docker = Docker()
    (_, log, versions), _ = probe(docker, ["redis"], tmp_path)
    assert versions == {"redis_server": "7.4.11"}

    [redis] = docker.runs("vc-redis")
    assert "--runtime=runsc" in redis and "--read-only" in redis and redis[redis.index("--user") + 1] == "65534:65534"
    args = redis[redis.index(REDIS) + 1:]
    assert args[:2] == ["redis-server", "--port"]
    assert args[args.index("--save") + 1] == "" and args[args.index("--appendonly") + 1] == "no"
    assert args[args.index("--user") + 1: args.index("--user") + 3] == ["default", "off"]
    app = args[args.index("app") + 1:]
    assert "-@dangerous" in app and "-@admin" in app and app[-1].startswith(">")
    oracle_user = args[args.index("oracle"):args.index("app")]
    assert "+@all" in oracle_user and "-@dangerous" not in oracle_user

    suts = docker.runs("vc-sut")
    assert [c[c.index("--name") + 1] for c in suts] == ["vc-sut-k1-0", "vc-sut-k1-1"]
    redis_ip = docker.ips["vc-redis-k1"]
    urls = {env_of(c)["REDIS_URL"] for c in suts}
    assert len(urls) == 1
    app_url = urls.pop()
    assert app_url.startswith("redis://app:") and app_url.endswith(f"@{redis_ip}:6379/0")

    oracle_env = env_of(docker.runs("vc-oracle")[0])
    replica_urls = oracle_env["SUT_REPLICA_URLS"].split(",")
    assert replica_urls == [f"http://{docker.ips[n]}:8000" for n in ("vc-sut-k1-0", "vc-sut-k1-1")]
    assert oracle_env["SUT_BASE_URL"] == replica_urls[0]
    assert oracle_env["VC_REDIS_ADDR"] == f"{redis_ip}:6379"
    assert oracle_env["VC_REDIS_PASSWORD"] not in app_url, "the service never gets the admin password"
    assert "REDIS_URL" not in oracle_env and "VC_REDIS_PASSWORD" not in env_of(suts[0])

    assert "[0] replica log" in log and "[1] replica log" in log
    cleanup = [c for c in docker.commands if c[1:3] == ["rm", "-f"]][-1]
    assert {"vc-sut-k1-0", "vc-sut-k1-1", "vc-oracle-k1", "vc-redis-k1"} <= set(cleanup)
    assert docker.commands[-1] == ["docker", "network", "rm", "vc-net-k1"]


def test_replica_count_is_configurable(tmp_path):
    docker = Docker()
    probe(docker, ["redis"], tmp_path, stateful_replicas=3)
    assert len(docker.runs("vc-sut")) == 3
    assert len(env_of(docker.runs("vc-oracle")[0])["SUT_REPLICA_URLS"].split(",")) == 3


def test_redis_that_never_answers_is_an_infrastructure_error(tmp_path):
    docker = Docker(redis_ready=False)
    with pytest.raises(InfrastructureError, match="redis did not become ready"):
        probe(docker, ["redis"], tmp_path, service_ready_timeout_s=0)
    assert not docker.runs("vc-sut"), "the service never starts without its store"
    assert docker.commands[-1][1:3] == ["network", "rm"]


def test_failed_state_reset_is_an_infrastructure_error(tmp_path):
    docker = Docker()
    out = tmp_path / "out"
    out.mkdir()
    (out / "state_reset.error").write_text("ConnectionRefusedError: [Errno 111]\n")
    with pytest.raises(InfrastructureError, match="could not be reset"):
        probe(docker, ["redis"], tmp_path)
    assert docker.commands[-1][1:3] == ["network", "rm"]


# ---------------------------------------------------------------- harness

def load_plugin():
    spec = importlib.util.spec_from_file_location("vc_state_reset", HARNESS_DIR / "plugins" / "vc_state_reset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRedis(threading.Thread):
    def __init__(self, replies: list[bytes]):
        super().__init__(daemon=True)
        self.server = socket.create_server(("127.0.0.1", 0))
        self.addr = "127.0.0.1:%d" % self.server.getsockname()[1]
        self.replies = replies
        self.received = b""

    def run(self):
        conn, _ = self.server.accept()
        with conn:
            for reply in self.replies:
                data = conn.recv(4096)
                self.received += data
                conn.sendall(reply)


def test_reset_plugin_authenticates_as_oracle_and_flushes():
    server = FakeRedis([b"+OK\r\n", b"+OK\r\n"])
    server.start()
    load_plugin().reset(server.addr, "s3cret")
    server.join(5)
    assert b"$4\r\nAUTH\r\n$6\r\noracle\r\n$6\r\ns3cret\r\n" in server.received
    assert b"$8\r\nFLUSHALL\r\n$4\r\nSYNC\r\n" in server.received


def test_reset_plugin_raises_on_refusal():
    server = FakeRedis([b"-WRONGPASS invalid username-password pair\r\n"])
    server.start()
    with pytest.raises(RuntimeError, match="AUTH failed"):
        load_plugin().reset(server.addr, "wrong")


def test_wait_ready_waits_for_every_replica(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    up = f"http://127.0.0.1:{server.server_address[1]}"
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    down = f"http://127.0.0.1:{closed.getsockname()[1]}"
    script = str(HARNESS_DIR / "wait_ready.py")
    try:
        assert subprocess.run([sys.executable, script, f"{up},{up}", "5"]).returncode == 0
        failed = subprocess.run([sys.executable, script, f"{up},{down}", "1"], capture_output=True, text=True)
        assert failed.returncode == 1 and down in failed.stdout
    finally:
        server.shutdown()
        closed.close()


# ---------------------------------------------------------------- governance and prompts

CROSS = fakes.TEST_HEALTH.replace('def test_health():', 'def test_health():\n    os.environ["SUT_REPLICA_URLS"]')


def stateful(code: str = CROSS) -> dict:
    return {**fakes.spec(code), "backing_services": ["redis"]}


@pytest.mark.parametrize("spec, expected", [
    (stateful(), []),
    (stateful(fakes.TEST_HEALTH), ["no test uses SUT_REPLICA_URLS"]),
    (stateful(CROSS.replace("/health", "/health?u=' + os.environ['VC_REDIS_ADDR'] + '")), ["must not reach"]),
    (fakes.spec(CROSS), ["only set for contracts with a backing service"]),
    (fakes.spec(), []),
])
def test_state_problems(spec, expected):
    problems = governance.state_problems(spec)
    assert len(problems) == len(expected)
    for problem, fragment in zip(problems, expected):
        assert fragment in problem


def test_prompts_describe_the_stateful_topology_only_when_declared():
    stateful_contract = {**fakes.CONTRACT, "backing_services": ["redis"]}
    assert "REDIS_URL" in prompts.service_rules(stateful_contract)
    assert "REDIS_URL" not in prompts.service_rules(fakes.CONTRACT)
    spec_prompt = prompts.verification_spec(stateful_contract)[1][1]
    assert "SUT_REPLICA_URLS" in spec_prompt
    assert "SUT_REPLICA_URLS" not in prompts.verification_spec(fakes.CONTRACT)[1][1]


# ---------------------------------------------------------------- compiler nodes

def nodes(cfg=None, spec_response=None):
    llms = fakes.FakeLLMs({"verification_compiler": [spec_response or fakes.spec()]})
    return CompilerNodes(cfg or fakes.config(), llms, fakes.FakeSandbox(), fakes.FakeResolver())


def test_stateful_contract_without_redis_image_aborts_before_any_model_call():
    n = nodes()
    update = n.requirement_gate({"requirements": "rate limit", "requirement_contract":
                                 {**fakes.CONTRACT, "backing_services": ["redis"]}})
    assert update["status"] == "execution_failed" and "VC_REDIS_IMAGE" in update["error"]
    assert n.llm.calls == []


def test_spec_topology_comes_from_the_contract():
    cfg = fakes.config()
    cfg = cfg.model_copy(update={"sandbox": cfg.sandbox.model_copy(update={"redis_image": REDIS})})
    produced = {**fakes.spec(CROSS), "backing_services": []}   # whatever the model says is overwritten
    update = nodes(cfg, produced).verification_compiler(
        {"requirement_contract": {**fakes.CONTRACT, "backing_services": ["redis"]}})
    assert update["verification_spec"]["backing_services"] == ["redis"]


def test_stateful_spec_without_cross_replica_test_is_rejected():
    update = nodes().verification_compiler({"requirement_contract": {**fakes.CONTRACT, "backing_services": ["redis"]}})
    assert update["status"] == "execution_failed" and "SUT_REPLICA_URLS" in update["error"]


def test_harness_plugin_is_packaged():
    assert (Path(HARNESS_DIR) / "plugins" / "vc_state_reset.py").is_file()


def test_report_names_the_backing_service():
    from verification_compiler.report import render_summary

    evidence = fakes.passing_result(fakes.codebase(), stateful(), {"sha256": "x"}).model_copy(update={
        "backing_services": ["redis"], "service_replicas": 2,
        "images": {"verifier": fakes.IMAGE, "runtime": fakes.IMAGE, "redis": REDIS}})
    text = render_summary({"status": "budget_exceeded", "verification_result": evidence.model_dump()})
    assert f"**Backing service:** redis `{REDIS}` (service ran as 2 replicas" in text
