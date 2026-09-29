"""Turns a Jira issue into a requirements payload.

Jira content is untrusted input: it is carried as data into a pull request, where the compiler
treats the PR text exactly like any other requirement text. Nothing here interprets it.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]+-[1-9][0-9]*$")
MAX_TEXT = 20_000
_AC_HEADING = re.compile(r"^\s*(?:#+\s*|h\d\.\s*|\*)?\s*acceptance criteria\s*:?\s*\**\s*$", re.I)
_HEADING = re.compile(r"^\s*(?:#+\s+|h\d\.\s+)\S")
_BULLET = re.compile(r"^\s*(?:[-*•#]+|\d+[.)]|\(?[a-z]\))\s+")


class RequirementPayload(BaseModel):
    jira_key: str
    summary: str
    description: str
    acceptance_criteria: list[str]
    linked_issues: list[str]
    target_repo: str
    base_branch: str

    @property
    def branch(self) -> str:
        return f"feature/jira-{self.jira_key}-auto-impl"

    def requirements_text(self) -> str:
        parts = [f"Jira {self.jira_key}: {self.summary}", "", self.description.strip() or "(no description)"]
        if self.acceptance_criteria:
            parts += ["", "Acceptance criteria:"] + [f"- {ac}" for ac in self.acceptance_criteria]
        if self.linked_issues:
            parts += ["", "Linked issues (context only):"] + [f"- {li}" for li in self.linked_issues]
        return "\n".join(parts)[:MAX_TEXT]


def adf_to_text(node: Any) -> str:
    """Atlassian Document Format -> plain text, keeping paragraph, list and heading structure."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return "\n"
    if kind in ("mention", "emoji"):
        return node.get("attrs", {}).get("text", "")
    inner = adf_to_text(node.get("content", []))
    if kind == "heading":
        return f"\n# {inner.strip()}\n"
    if kind == "listItem":
        return f"- {inner.strip()}\n"
    if kind in ("paragraph", "codeBlock", "blockquote"):
        return f"{inner}\n"
    if kind in ("bulletList", "orderedList", "doc"):
        return inner if kind == "doc" else f"{inner}\n"
    return inner


def extract_acceptance_criteria(description: str, field_value: Any = None) -> tuple[str, list[str]]:
    """Returns (description without the AC section, criteria). A dedicated field wins over the section."""
    if field_value:
        text = adf_to_text(field_value) if not isinstance(field_value, list) else "\n".join(map(str, field_value))
        return description, _bullets(text.splitlines())
    lines = description.splitlines()
    for i, line in enumerate(lines):
        if _AC_HEADING.match(line):
            end = next((j for j in range(i + 1, len(lines)) if _HEADING.match(lines[j])), len(lines))
            criteria = _bullets(lines[i + 1:end])
            return "\n".join(lines[:i] + lines[end:]).strip(), criteria
    return description, []


def _bullets(lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        text = _BULLET.sub("", line).strip()
        if text:
            out.append(text[:500])
    return out[:50]


def normalize(issue: dict, target_repo: str, base_branch: str, ac_field: str | None = None) -> RequirementPayload:
    key = issue.get("key", "")
    if not ISSUE_KEY.match(key):
        raise ValueError(f"invalid issue key {key!r}")
    fields = issue.get("fields") or {}
    description, criteria = extract_acceptance_criteria(
        adf_to_text(fields.get("description")).strip(), fields.get(ac_field) if ac_field else None)
    links = []
    for link in fields.get("issuelinks") or []:
        other = link.get("outwardIssue") or link.get("inwardIssue") or {}
        relation = (link.get("type") or {}).get("outward" if "outwardIssue" in link else "inward", "relates to")
        if other.get("key"):
            links.append(f"{relation} {other['key']}: {(other.get('fields') or {}).get('summary', '')}".strip())
    return RequirementPayload(
        jira_key=key,
        summary=(fields.get("summary") or "").strip()[:300] or key,
        description=description[:MAX_TEXT],
        acceptance_criteria=criteria,
        linked_issues=links[:20],
        target_repo=target_repo,
        base_branch=base_branch,
    )
