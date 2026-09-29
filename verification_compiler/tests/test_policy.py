import pytest

from verification_compiler.config import Limits
from verification_compiler.policy import (
    PolicyViolation,
    evaluate_codebase,
    evaluate_spec,
    parse_pinned_dependency,
    validate_repo_path,
)
from verification_compiler.static_checks import secret_scan, syntax_check

from . import fakes


@pytest.mark.parametrize("path", ["service/main.py", "a/b_c/d-e.txt", "README.md"])
def test_valid_paths(path):
    assert str(validate_repo_path(path)) == path


@pytest.mark.parametrize("path", [
    "", "/etc/passwd", "../x.py", "a/../b.py", "a//b.py", "a/./b.py", "a/", "a\\b.py", "a\x00.py",
    ".env", "pkg/.hidden/x.py", "conftest.py", "tests/conftest.py", "sitecustomize.py", "x.pth",
    "PyTest.ini", "a b.py", "x" * 300,
])
def test_rejected_paths(path):
    with pytest.raises(PolicyViolation):
        validate_repo_path(path)


@pytest.mark.parametrize("dep,expected", [
    ("fastapi==0.111.0", ("fastapi", "0.111.0")),
    ("PyJWT[crypto]==2.8.0", ("pyjwt", "2.8.0")),
])
def test_pinned_dependencies(dep, expected):
    assert parse_pinned_dependency(dep) == expected


@pytest.mark.parametrize("dep", [
    "fastapi", "fastapi>=0.1", "fastapi==0.*", "fastapi==1.0,>=0.5", "fastapi===1.0",
    "pkg @ https://evil/pkg.whl", "fastapi==1.0; python_version<'3'", "--index-url x", "not a req!",
])
def test_rejected_dependencies(dep):
    with pytest.raises(PolicyViolation):
        parse_pinned_dependency(dep)


def test_valid_codebase_has_no_violations():
    assert evaluate_codebase(fakes.codebase(), Limits()) == []


def test_codebase_violations_are_all_reported():
    cb = fakes.codebase(deps=["fastapi>=0.1"])
    cb["files"].append({"path": "../escape.py", "content": ""})
    cb["files"].append({"path": "Service/main.py", "content": ""})
    cb["entrypoint"] = "service.missing:app"
    violations = evaluate_codebase(cb, Limits())
    joined = "\n".join(violations)
    assert "'..'" in joined
    assert "duplicate path" in joined
    assert "entrypoint module" in joined
    assert "exactly one" in joined
    assert "uvicorn" in joined


def test_size_limits():
    cb = fakes.codebase()
    cb["files"].append({"path": "big.txt", "content": "x" * 11})
    assert any("per-file limit" in v for v in evaluate_codebase(cb, Limits(max_file_bytes=10, max_repo_bytes=10**6)))


def test_file_directory_clash():
    cb = fakes.codebase()
    cb["files"].append({"path": "service/main.py/x.py", "content": ""})
    assert any("file and as a directory" in v for v in evaluate_codebase(cb, Limits()))


def test_spec_validation_accepts_black_box_tests():
    assert evaluate_spec(fakes.spec()) == []


@pytest.mark.parametrize("code,needle", [
    ("def test_x(:\n", "syntax error"),
    ("import os\nX = os.environ['SUT_BASE_URL']\n", "no top-level test"),
    ("def test_x():\n    assert True\n", "SUT_BASE_URL"),
    ("from service.main import app\nimport os\nos.environ['SUT_BASE_URL']\ndef test_x(): pass\n", "not allowed"),
    ("import subprocess, os\nos.environ['SUT_BASE_URL']\ndef test_x(): pass\n", "not allowed"),
])
def test_spec_validation_rejects(code, needle):
    assert any(needle in p for p in evaluate_spec(fakes.spec(code)))


def test_spec_requires_tests_and_unique_ids():
    sp = fakes.spec()
    assert evaluate_spec({**sp, "acceptance_tests": []})
    sp["acceptance_tests"].append({**sp["acceptance_tests"][0], "id": "at_HEALTH"})
    assert any("unique" in p for p in evaluate_spec(sp))


def test_syntax_check():
    bad = syntax_check([{"path": "a.py", "content": "def f(:\n"}, {"path": "b.txt", "content": "def f(:"}])
    assert not bad.passed and "a.py:1" in bad.detail and "b.txt" not in bad.detail


@pytest.mark.parametrize("line,flagged", [
    ('JWT_SECRET = "s3cr3t-value-123"', True),
    ('aws = "AKIAABCDEFGHIJKLMNOP"', True),
    ("-----BEGIN RSA PRIVATE KEY-----", True),
    ('JWT_SECRET = os.environ["JWT_SECRET"]', False),
    ('token_url = "/api/v1/auth/token"', False),
    ('password = "short"', False),
])
def test_secret_scan(line, flagged):
    assert secret_scan([{"path": "a.py", "content": line}]).passed is not flagged
