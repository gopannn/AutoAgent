import pytest
from pydantic import ValidationError

from verification_compiler.config import SandboxConfig, is_digest_pinned, is_transient

DIGEST = "sha256:" + "0" * 64


class _Status(Exception):
    def __init__(self, code):
        self.status_code = code


class APIConnectionError(Exception):
    pass


@pytest.mark.parametrize("exc,expected", [
    (_Status(429), True), (_Status(503), True), (_Status(529), True),
    (_Status(400), False), (_Status(401), False), (_Status(404), False),
    (ConnectionError(), True), (TimeoutError(), True),
    (APIConnectionError(), True),
    (ValueError("bad schema"), False), (RuntimeError(), False), (KeyError("x"), False),
])
def test_is_transient(exc, expected):
    assert is_transient(exc) is expected


@pytest.mark.parametrize("ref,ok", [
    (DIGEST, True),
    ("python:3.12-slim@" + DIGEST, True),
    ("ghcr.io/org/verifier@" + DIGEST, True),
    ("python:3.12-slim", False),
    ("python@sha256:abc", False),
])
def test_digest_pinning(ref, ok):
    assert is_digest_pinned(ref) is ok


def test_sandbox_config_rejects_tags():
    with pytest.raises(ValidationError):
        SandboxConfig(verifier_image="python:3.12-slim", runtime_image=DIGEST)
