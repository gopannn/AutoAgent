"""Host-side checks that only parse generated code and never execute it."""
from __future__ import annotations

import ast
import re

from .schemas import CheckResult

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("API key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("hardcoded credential", re.compile(
        r"""(?i)\b(?P<name>\w*(?:secret|passw(?:or)?d|api_?key|token|signing_?key)\w*)["']?\s*[:=]\s*["'](?P<value>[^"'\s]{8,})["']"""
    )),
]
# Names that describe a credential rather than hold one (token_type, password_reset_path, ...).
_DESCRIPTIVE_NAME = re.compile(
    r"(?i)_(?:type|path|url|uri|name|field|header|endpoint|route|ttl|expiry|expires|length|len|prefix|label|scheme)$"
)



def syntax_check(files: list[dict]) -> CheckResult:
    errors = []
    for f in files:
        if not f["path"].endswith(".py"):
            continue
        try:
            ast.parse(f["content"], filename=f["path"])
        except SyntaxError as err:
            errors.append(f"{f['path']}:{err.lineno}: {err.msg}")
    return CheckResult(name="syntax", passed=not errors, detail="\n".join(errors))


def secret_scan(files: list[dict]) -> CheckResult:
    hits = []
    for f in files:
        for lineno, line in enumerate(f["content"].splitlines(), 1):
            for label, pattern in _SECRET_PATTERNS:
                match = pattern.search(line)
                if match and not _looks_like_non_secret(match.groupdict()):
                    hits.append(f"{f['path']}:{lineno}: possible {label}")
                    break
    detail = "\n".join(hits)
    if hits:
        detail += "\nLoad secrets from environment variables or a secret manager instead of source code."
    return CheckResult(name="secret_scan", passed=not hits, detail=detail)


def _looks_like_non_secret(groups: dict) -> bool:
    """Descriptive names (token_type, reset_path), route paths and URLs are not credentials.

    Identifier-like values are deliberately NOT exempt: JWT_SECRET = "super-secret-key" is a finding.

    Measured against labelled benign and secret-bearing lines in tests/test_detector_calibration.py.
    """
    value, name = groups.get("value"), groups.get("name")
    if value is None:
        return False
    return (
        value.startswith("/")
        or "://" in value
        or bool(name and _DESCRIPTIVE_NAME.search(name))
    )
