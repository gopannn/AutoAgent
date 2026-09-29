"""What the bridge does with a Jira event.

    webhook ─► authenticate ─► should_act? ─► claim (idempotent) ─► normalise ─► branch + request file + PR

The bridge never runs a model and never pushes code. It opens a pull request whose title and
body carry the ticket; `.github/workflows/ai-compiler.yml` then compiles, verifies and signs,
and reports back to the ticket (verification_compiler.jira.feedback). There is one gate.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .clients import GitHub
from .config import BridgeSettings, RepoTarget
from .normalize import ISSUE_KEY, RequirementPayload, normalize

log = logging.getLogger("verification_compiler.jira")
MARKER = "<!-- jira-bridge:{key} -->"
REQUEST_DIR = ".verification/requests"
_EVENTS = {"jira:issue_created", "jira:issue_updated"}
# Deliveries are serialised: two events for one ticket (assigned, then moved) must not both see
# "no open PR" and race to create it. Volume is a few tickets a minute, so one lock is enough.
_DELIVERY_LOCK = threading.Lock()


# ---------------------------------------------------------------- authentication

def verify_signature(secret: str, body: bytes, header_value: str | None) -> bool:
    """Jira Cloud signs the raw body: `X-Hub-Signature: sha256=<hex HMAC-SHA256>`."""
    if not header_value or "=" not in header_value:
        return False
    algorithm, _, received = header_value.partition("=")
    if algorithm.strip().lower() != "sha256":
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received.strip().lower())


def verify_token(secret: str, supplied: str | None) -> bool:
    return bool(supplied) and hmac.compare_digest(secret.encode(), supplied.encode())


# ---------------------------------------------------------------- filtering

@dataclass
class Decision:
    act: bool
    reason: str
    idempotency_key: str = ""


def should_act(event: dict, settings: BridgeSettings) -> Decision:
    kind = event.get("webhookEvent")
    if kind not in _EVENTS:
        return Decision(False, f"ignored event {kind!r}")
    issue = event.get("issue") or {}
    key = issue.get("key", "")
    if not ISSUE_KEY.match(key):
        return Decision(False, "missing or malformed issue key")
    fields = issue.get("fields") or {}
    project = (fields.get("project") or {}).get("key") or key.rsplit("-", 1)[0]
    if project not in settings.projects:
        return Decision(False, f"project {project} is not mapped to a repository")
    assignee = (fields.get("assignee") or {}).get("accountId")
    if assignee != settings.bot_account_id:
        return Decision(False, "issue is not assigned to the automation user")

    items = (event.get("changelog") or {}).get("items") or []
    assigned_now = any(i.get("field") == "assignee" and i.get("to") == settings.bot_account_id for i in items)
    moved_to_trigger = any(i.get("field") == "status" and i.get("toString") in settings.trigger_statuses
                           for i in items)
    status = (fields.get("status") or {}).get("name")
    if kind == "jira:issue_created":
        if status not in settings.trigger_statuses and not assigned_now:
            return Decision(False, "created without a trigger status")
    elif not (assigned_now or moved_to_trigger):
        return Decision(False, "update did not assign the bot or move the issue to a trigger status")

    marker = (event.get("changelog") or {}).get("id") or event.get("timestamp") or fields.get("updated") or ""
    return Decision(True, "accepted", f"{key}:{marker}")


# ---------------------------------------------------------------- state

class Store:
    """Idempotency and job status. One row per delivery; a (key, marker) pair is processed once."""

    def __init__(self, path: Path):
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            idempotency_key TEXT PRIMARY KEY, jira_key TEXT NOT NULL, status TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '', pr_url TEXT, updated REAL NOT NULL)""")
        self.db.commit()

    def claim(self, idempotency_key: str, jira_key: str) -> bool:
        with self._lock:
            try:
                self.db.execute("INSERT INTO jobs VALUES (?, ?, 'accepted', '', NULL, ?)",
                                (idempotency_key, jira_key, time.time()))
                self.db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def update(self, idempotency_key: str, status: str, detail: str = "", pr_url: str | None = None) -> None:
        with self._lock:
            self.db.execute("UPDATE jobs SET status=?, detail=?, pr_url=COALESCE(?, pr_url), updated=? "
                            "WHERE idempotency_key=?", (status, detail[:2000], pr_url, time.time(), idempotency_key))
            self.db.commit()

    def get(self, jira_key: str) -> list[dict]:
        with self._lock:
            rows = self.db.execute("SELECT idempotency_key, status, detail, pr_url, updated FROM jobs "
                                   "WHERE jira_key=? ORDER BY updated DESC", (jira_key,)).fetchall()
        return [dict(zip(("idempotency_key", "status", "detail", "pr_url", "updated"), r)) for r in rows]


# ---------------------------------------------------------------- delivery

def request_file(payload: RequirementPayload) -> str:
    return (f"# {payload.jira_key}: {payload.summary}\n\n"
            "Requirement request created by the Jira bridge. The AI compiler workflow uses the pull request\n"
            "title and body as requirements; this file only gives the branch its first commit.\n\n"
            "```json\n" + json.dumps(payload.model_dump(), indent=2, sort_keys=True) + "\n```\n")


def pull_body(payload: RequirementPayload) -> str:
    return (payload.requirements_text() + "\n\n---\n"
            "Opened by the Jira bridge. Verification, repair and signing run in `ai-compiler.yml`; "
            "the result is reported back to the ticket.\n" + MARKER.format(key=payload.jira_key))


def deliver(event: dict, settings: BridgeSettings, github: GitHub, store: Store, idempotency_key: str) -> None:
    issue = event["issue"]
    project = (issue.get("fields", {}).get("project") or {}).get("key") or issue["key"].rsplit("-", 1)[0]
    target = settings.projects[project]
    with _DELIVERY_LOCK:
        _deliver(issue, target, settings, github, store, idempotency_key)


def _deliver(issue: dict, target: RepoTarget, settings: BridgeSettings, github: GitHub, store: Store,
             idempotency_key: str) -> None:
    try:
        payload = normalize(issue, target.repo, target.base_branch, settings.acceptance_criteria_field)
        existing = github.open_pull(target.repo, payload.branch)
        if existing:
            store.update(idempotency_key, "skipped", "a pull request is already open", existing.get("html_url"))
            return
        if github.branch_sha(target.repo, payload.branch) is None:
            base_sha = github.branch_sha(target.repo, target.base_branch)
            if base_sha is None:
                raise RuntimeError(f"base branch {target.base_branch} not found in {target.repo}")
            github.create_branch(target.repo, payload.branch, base_sha)
        github.put_file(target.repo, payload.branch, f"{REQUEST_DIR}/{payload.jira_key}.md", request_file(payload),
                        f"{payload.jira_key}: requirement request from Jira")
        pr = github.create_pull(target.repo, payload.branch, target.base_branch,
                                f"{payload.jira_key}: {payload.summary}"[:250], pull_body(payload))
        store.update(idempotency_key, "pr_opened", "", pr.get("html_url"))
        log.info("opened %s for %s", pr.get("html_url"), payload.jira_key)
    except Exception as err:  # noqa: BLE001 - recorded, visible at /jobs; the ticket stays untouched
        log.exception("delivery failed for %s", idempotency_key)
        store.update(idempotency_key, "failed", f"{type(err).__name__}: {err}")
