"""Settings for the Jira webhook bridge. Everything is read once at startup and validated fail-closed."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")


class RepoTarget(BaseModel):
    repo: str
    base_branch: str = "main"

    @field_validator("repo")
    @classmethod
    def _repo(cls, v: str) -> str:
        if not _REPO.match(v):
            raise ValueError(f"invalid repository {v!r}; expected owner/name")
        return v

    @field_validator("base_branch")
    @classmethod
    def _branch(cls, v: str) -> str:
        if not _BRANCH.match(v) or ".." in v:
            raise ValueError(f"invalid base branch {v!r}")
        return v


class BridgeSettings(BaseModel):
    # Authentication of incoming webhooks. "hmac" verifies Jira Cloud's X-Hub-Signature (sha256=...);
    # "token" compares a shared token query parameter, for Jira Data Center, which cannot sign.
    auth_mode: str = Field(default="hmac", pattern="^(hmac|token)$")
    webhook_secret: str = Field(min_length=16)
    signature_header: str = "X-Hub-Signature"
    bot_account_id: str = Field(min_length=1, description="Jira accountId of the automation user.")
    trigger_statuses: frozenset[str] = frozenset({"In Progress"})
    projects: dict[str, RepoTarget] = Field(description="Jira project key -> repository and base branch.")
    acceptance_criteria_field: str | None = None
    max_body_bytes: int = 1_000_000
    github_token: str = Field(min_length=1)
    github_api: str = "https://api.github.com"
    state_path: Path = Path("jira_bridge.sqlite3")

    @field_validator("projects")
    @classmethod
    def _projects(cls, v: dict) -> dict:
        if not v:
            raise ValueError("at least one Jira project must be mapped to a repository")
        for key in v:
            if not re.match(r"^[A-Z][A-Z0-9_]+$", key):
                raise ValueError(f"invalid Jira project key {key!r}")
        return v

    @classmethod
    def from_env(cls) -> "BridgeSettings":
        required = ["JIRA_WEBHOOK_SECRET", "JIRA_BOT_ACCOUNT_ID", "JIRA_PROJECT_REPOS", "GITHUB_TOKEN"]
        missing = [n for n in required if not os.environ.get(n)]
        if missing:
            raise RuntimeError(f"missing required settings: {', '.join(missing)}")
        statuses = os.environ.get("JIRA_TRIGGER_STATUSES", "In Progress")
        return cls(
            auth_mode=os.environ.get("JIRA_WEBHOOK_AUTH", "hmac"),
            webhook_secret=os.environ["JIRA_WEBHOOK_SECRET"],
            signature_header=os.environ.get("JIRA_SIGNATURE_HEADER", "X-Hub-Signature"),
            bot_account_id=os.environ["JIRA_BOT_ACCOUNT_ID"],
            trigger_statuses=frozenset(s.strip() for s in statuses.split(",") if s.strip()),
            projects=json.loads(os.environ["JIRA_PROJECT_REPOS"]),
            acceptance_criteria_field=os.environ.get("JIRA_ACCEPTANCE_CRITERIA_FIELD") or None,
            github_token=os.environ["GITHUB_TOKEN"],
            github_api=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
            state_path=Path(os.environ.get("JIRA_BRIDGE_STATE", "jira_bridge.sqlite3")),
        )
