"""Jira webhook bridge: authentication, filtering, idempotency, normalisation, delivery and feedback."""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from verification_compiler.jira import feedback  # noqa: E402
from verification_compiler.jira.app import create_app  # noqa: E402
from verification_compiler.jira.config import BridgeSettings, RepoTarget  # noqa: E402
from verification_compiler.jira.normalize import (  # noqa: E402
    adf_to_text, extract_acceptance_criteria, normalize)
from verification_compiler.jira.service import (  # noqa: E402
    MARKER, REQUEST_DIR, Store, should_act, verify_signature)

SECRET = "s3cret-for-tests-only-0123456789"
BOT = "bot-account-1"


class FakeGitHub:
    def __init__(self, branches=None, pulls=None, fail_on=None):
        self.branches = dict({("org/svc", "main"): "base-sha"} if branches is None else branches)
        self.pulls = dict(pulls or {})
        self.files: dict[tuple[str, str, str], str] = {}
        self.created: list[dict] = []
        self.fail_on = fail_on

    def branch_sha(self, repo, branch):
        return self.branches.get((repo, branch))

    def create_branch(self, repo, branch, sha):
        if (repo, branch) in self.branches:
            return False
        self.branches[(repo, branch)] = sha
        return True

    def put_file(self, repo, branch, path, content, message):
        if self.fail_on == "put_file":
            raise RuntimeError("contents API unavailable")
        self.files[(repo, branch, path)] = content

    def open_pull(self, repo, branch):
        return self.pulls.get((repo, branch))

    def create_pull(self, repo, branch, base, title, body):
        pr = {"html_url": f"https://github.com/{repo}/pull/{len(self.created) + 1}",
              "head": branch, "base": base, "title": title, "body": body}
        self.created.append(pr)
        self.pulls[(repo, branch)] = pr
        return pr


class FakeJira:
    def __init__(self, statuses=("In Code Review",)):
        self.comments: list[tuple[str, str]] = []
        self.transitions: list[tuple[str, str]] = []
        self.statuses = statuses

    def comment(self, key, text):
        self.comments.append((key, text))

    def transition(self, key, status_name):
        if status_name not in self.statuses:
            return False
        self.transitions.append((key, status_name))
        return True


def settings(tmp_path, **overrides) -> BridgeSettings:
    values = dict(webhook_secret=SECRET, bot_account_id=BOT, github_token="ghp_test",
                  projects={"ABC": RepoTarget(repo="org/svc")}, state_path=tmp_path / "state.sqlite3",
                  acceptance_criteria_field="customfield_10100")
    values.update(overrides)
    return BridgeSettings(**values)


def adf(*paragraphs: str) -> dict:
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in paragraphs]}


def event(kind="jira:issue_updated", key="ABC-12", assignee=BOT, status="To Do", items=None, changelog_id="1001",
          **fields) -> dict:
    body = {
        "webhookEvent": kind,
        "timestamp": 1700000000000,
        "issue": {"key": key, "fields": {
            "summary": "Add a /health endpoint",
            "description": adf("Expose service health.", "Acceptance Criteria", "- GET /health returns 200"),
            "project": {"key": key.rsplit("-", 1)[0]},
            "assignee": {"accountId": assignee} if assignee else None,
            "status": {"name": status},
            **fields,
        }},
    }
    if items is not None:
        body["changelog"] = {"id": changelog_id, "items": items}
    return body


ASSIGNED = [{"field": "assignee", "to": BOT}]


def signed(body: dict) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Hub-Signature": f"sha256={sig}", "Content-Type": "application/json"}


@pytest.fixture
def bridge(tmp_path):
    github = FakeGitHub()
    s = settings(tmp_path)
    store = Store(s.state_path)
    return TestClient(create_app(s, github, store)), github, store


# ---------------------------------------------------------------- authentication

def test_signature_verification():
    body = b'{"a": 1}'
    good = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(SECRET, body, good)
    assert verify_signature(SECRET, body, good.upper().replace("SHA256=", "sha256="))
    assert not verify_signature(SECRET, body + b" ", good)
    assert not verify_signature(SECRET, body, good.replace("sha256", "sha1"))
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(SECRET, body, "garbage")


def test_unsigned_or_forged_requests_are_rejected(bridge):
    client, github, _ = bridge
    raw, headers = signed(event(items=ASSIGNED))
    assert client.post("/webhooks/jira", content=raw, headers={"Content-Type": "application/json"}).status_code == 401
    forged = dict(headers, **{"X-Hub-Signature": "sha256=" + "0" * 64})
    assert client.post("/webhooks/jira", content=raw, headers=forged).status_code == 401
    assert not github.created


def test_token_mode(tmp_path):
    github = FakeGitHub()
    s = settings(tmp_path, auth_mode="token")
    client = TestClient(create_app(s, github, Store(s.state_path)))
    raw = json.dumps(event(items=ASSIGNED)).encode()
    assert client.post("/webhooks/jira?token=wrong-token-value", content=raw).status_code == 401
    assert client.post("/webhooks/jira", content=raw).status_code == 401
    assert client.post(f"/webhooks/jira?token={SECRET}", content=raw).status_code == 202
    assert len(github.created) == 1


def test_oversized_and_malformed_bodies(tmp_path):
    s = settings(tmp_path, max_body_bytes=200)
    client = TestClient(create_app(s, FakeGitHub(), Store(s.state_path)))
    raw, headers = signed(event(items=ASSIGNED))
    assert client.post("/webhooks/jira", content=raw, headers=headers).status_code == 413
    raw, headers = signed([1, 2])  # authentic but not an object
    assert client.post("/webhooks/jira", content=raw, headers=headers).status_code == 400
    bad = b"not json"
    headers = {"X-Hub-Signature": "sha256=" + hmac.new(SECRET.encode(), bad, hashlib.sha256).hexdigest()}
    assert client.post("/webhooks/jira", content=bad, headers=headers).status_code == 400


# ---------------------------------------------------------------- filtering

@pytest.mark.parametrize("body, reason", [
    (event(kind="comment_created", items=ASSIGNED), "ignored event"),
    (event(key="abc-12", items=ASSIGNED), "malformed issue key"),
    (event(key="ABC-0", items=ASSIGNED), "malformed issue key"),
    (event(key="XYZ-3", items=ASSIGNED), "not mapped"),
    (event(assignee="someone-else", items=[{"field": "assignee", "to": "someone-else"}]), "not assigned"),
    (event(assignee=None, items=[]), "not assigned"),
    (event(items=[{"field": "summary", "toString": "new title"}]), "did not assign"),
    (event(kind="jira:issue_created", status="Backlog", items=[]), "trigger status"),
])
def test_events_that_are_ignored(tmp_path, body, reason):
    decision = should_act(body, settings(tmp_path))
    assert not decision.act and reason in decision.reason


@pytest.mark.parametrize("body", [
    event(items=ASSIGNED),
    event(items=[{"field": "status", "toString": "In Progress"}]),
    event(kind="jira:issue_created", status="In Progress"),
])
def test_events_that_trigger(tmp_path, body):
    decision = should_act(body, settings(tmp_path))
    assert decision.act and decision.idempotency_key.startswith("ABC-12:")


def test_ignored_event_returns_200_without_side_effects(bridge):
    client, github, store = bridge
    raw, headers = signed(event(items=[{"field": "summary"}]))
    r = client.post("/webhooks/jira", content=raw, headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "ignored"
    assert not github.created and store.get("ABC-12") == []


# ---------------------------------------------------------------- delivery and idempotency

def test_accepted_event_opens_a_pull_request(bridge):
    client, github, store = bridge
    raw, headers = signed(event(items=ASSIGNED))
    r = client.post("/webhooks/jira", content=raw, headers=headers)
    assert r.status_code == 202 and r.json()["idempotency_key"] == "ABC-12:1001"

    branch = "feature/jira-ABC-12-auto-impl"
    assert github.branches[("org/svc", branch)] == "base-sha"
    request = github.files[("org/svc", branch, f"{REQUEST_DIR}/ABC-12.md")]
    assert "Add a /health endpoint" in request
    [pr] = github.created
    assert pr["title"] == "ABC-12: Add a /health endpoint" and pr["base"] == "main" and pr["head"] == branch
    assert "- GET /health returns 200" in pr["body"] and MARKER.format(key="ABC-12") in pr["body"]
    [job] = store.get("ABC-12")
    assert job["status"] == "pr_opened" and job["pr_url"] == pr["html_url"]


def test_redelivery_is_processed_once(bridge):
    client, github, _ = bridge
    raw, headers = signed(event(items=ASSIGNED))
    assert client.post("/webhooks/jira", content=raw, headers=headers).status_code == 202
    r = client.post("/webhooks/jira", content=raw, headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "duplicate"
    assert len(github.created) == 1


def test_existing_open_pull_request_is_not_duplicated(tmp_path):
    branch = "feature/jira-ABC-12-auto-impl"
    github = FakeGitHub(pulls={("org/svc", branch): {"html_url": "https://github.com/org/svc/pull/9"}})
    s = settings(tmp_path)
    store = Store(s.state_path)
    client = TestClient(create_app(s, github, store))
    raw, headers = signed(event(items=ASSIGNED, changelog_id="2002"))
    assert client.post("/webhooks/jira", content=raw, headers=headers).status_code == 202
    assert not github.created and not github.files
    [job] = store.get("ABC-12")
    assert job["status"] == "skipped" and job["pr_url"].endswith("/pull/9")


@pytest.mark.parametrize("github, detail", [
    (FakeGitHub(branches={}), "base branch main not found"),
    (FakeGitHub(fail_on="put_file"), "contents API unavailable"),
])
def test_delivery_failures_are_recorded(tmp_path, github, detail):
    s = settings(tmp_path)
    store = Store(s.state_path)
    client = TestClient(create_app(s, github, store))
    raw, headers = signed(event(items=ASSIGNED))
    assert client.post("/webhooks/jira", content=raw, headers=headers).status_code == 202
    [job] = store.get("ABC-12")
    assert job["status"] == "failed" and detail in job["detail"]
    assert not github.created


def test_jobs_endpoint_requires_the_secret(bridge):
    client, _, _ = bridge
    raw, headers = signed(event(items=ASSIGNED))
    client.post("/webhooks/jira", content=raw, headers=headers)
    assert client.get("/jobs/ABC-12").status_code == 401
    assert client.get("/jobs/ABC-12", headers={"Authorization": "Bearer nope"}).status_code == 401
    auth = {"Authorization": f"Bearer {SECRET}"}
    assert client.get("/jobs/abc", headers=auth).status_code == 400
    r = client.get("/jobs/ABC-12", headers=auth)
    assert r.status_code == 200 and r.json()["jobs"][0]["status"] == "pr_opened"
    assert client.get("/healthz").json() == {"status": "ok"}


# ---------------------------------------------------------------- normalisation

def test_adf_to_text_keeps_structure():
    doc = {"type": "doc", "content": [
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Goal"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "line one"}, {"type": "hardBreak"},
                                          {"type": "text", "text": "line two"}]},
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "a"}]}]},
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "b"}]}]}]},
        {"type": "paragraph", "content": [{"type": "mention", "attrs": {"text": "@dev"}}]},
    ]}
    text = adf_to_text(doc)
    assert "# Goal" in text and "line one\nline two" in text and "- a\n" in text and "- b\n" in text
    assert "@dev" in text
    assert adf_to_text("plain wiki text") == "plain wiki text" and adf_to_text(None) == ""


def test_acceptance_criteria_from_section_and_field():
    description = "Intro.\n\nh3. Acceptance Criteria\n* returns 200\n* body is JSON\n\nh3. Notes\nkeep it small"
    rest, criteria = extract_acceptance_criteria(description)
    assert criteria == ["returns 200", "body is JSON"]
    assert "Acceptance Criteria" not in rest and "keep it small" in rest

    rest, criteria = extract_acceptance_criteria(description, adf("only this one"))
    assert criteria == ["only this one"] and rest == description
    assert extract_acceptance_criteria("no section here") == ("no section here", [])


def test_normalize_collects_links_and_rejects_bad_keys():
    issue = event(items=ASSIGNED, customfield_10100=adf("1. status is ok"), issuelinks=[
        {"type": {"outward": "blocks", "inward": "is blocked by"},
         "outwardIssue": {"key": "ABC-3", "fields": {"summary": "deploy pipeline"}}},
        {"type": {"outward": "blocks", "inward": "is blocked by"}, "inwardIssue": {"key": "ABC-1"}},
    ])["issue"]
    payload = normalize(issue, "org/svc", "main", "customfield_10100")
    assert payload.acceptance_criteria == ["status is ok"]
    assert payload.linked_issues == ["blocks ABC-3: deploy pipeline", "is blocked by ABC-1:"]
    assert payload.branch == "feature/jira-ABC-12-auto-impl"
    text = payload.requirements_text()
    assert text.startswith("Jira ABC-12: Add a /health endpoint") and "Acceptance criteria:" in text
    with pytest.raises(ValueError):
        normalize({"key": "ABC-12; rm -rf /"}, "org/svc", "main")


# ---------------------------------------------------------------- settings

def test_settings_fail_closed(tmp_path, monkeypatch):
    for name in ("JIRA_WEBHOOK_SECRET", "JIRA_BOT_ACCOUNT_ID", "JIRA_PROJECT_REPOS", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="missing required settings"):
        BridgeSettings.from_env()
    with pytest.raises(ValueError):
        settings(tmp_path, webhook_secret="short")
    with pytest.raises(ValueError):
        settings(tmp_path, projects={})
    with pytest.raises(ValueError):
        RepoTarget(repo="not a repo")
    with pytest.raises(ValueError):
        RepoTarget(repo="org/svc", base_branch="../main")

    monkeypatch.setenv("JIRA_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("JIRA_BOT_ACCOUNT_ID", BOT)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setenv("JIRA_PROJECT_REPOS", '{"ABC": {"repo": "org/svc", "base_branch": "develop"}}')
    monkeypatch.setenv("JIRA_TRIGGER_STATUSES", "In Progress, Ready for AI")
    s = BridgeSettings.from_env()
    assert s.projects["ABC"].base_branch == "develop"
    assert s.trigger_statuses == {"In Progress", "Ready for AI"}


# ---------------------------------------------------------------- feedback to Jira

def report_dir(tmp_path, headline, manifest=None):
    d = tmp_path / "vc-report"
    d.mkdir()
    (d / "summary.md").write_text(f"<!-- ai-compiler-report -->\n## AI Software Compiler: {headline}\n\nbody")
    if manifest is not None:
        (d / "release_manifest.json").write_text(json.dumps(manifest))
    return d


def test_feedback_on_release_comments_and_transitions(tmp_path):
    d = report_dir(tmp_path, "RELEASE_READY", {"artifact_hash": "abc123", "decision": {
        "ctd_outcome": "RESOLVED", "epistemic_verdict": "JUSTIFIED"}})
    jira = FakeJira()
    code = feedback.main(["--head-ref", "feature/jira-ABC-12-auto-impl", "--exit-code", "0", "--report-dir", str(d),
                          "--pr-url", "https://github.com/org/svc/pull/1"], jira=jira)
    assert code == 0
    [(key, text)] = jira.comments
    assert key == "ABC-12" and "RELEASE_READY" in text and "abc123" in text
    assert "CTD RESOLVED, epistemic JUSTIFIED" in text and "pull/1" in text
    assert jira.transitions == [("ABC-12", "In Code Review")]


def test_feedback_on_rejection_only_comments(tmp_path):
    d = report_dir(tmp_path, "REJECTED: repair budget exhausted")
    jira = FakeJira()
    feedback.main(["--head-ref", "feature/jira-ABC-12-auto-impl", "--exit-code", "1", "--report-dir", str(d),
                   "--pr-url", "u"], jira=jira)
    assert "REJECTED" in jira.comments[0][1] and "Not released" in jira.comments[0][1]
    assert jira.transitions == []


def test_feedback_skips_foreign_branches_and_missing_config(tmp_path, monkeypatch, capsys):
    jira = FakeJira()
    assert feedback.main(["--head-ref", "feature/my-work", "--report-dir", str(tmp_path), "--pr-url", "u"],
                         jira=jira) == 0
    assert not jira.comments
    for name in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert feedback.main(["--head-ref", "feature/jira-ABC-12-auto-impl", "--report-dir", str(tmp_path),
                          "--pr-url", "u"]) == 0
    assert "skipping Jira feedback" in capsys.readouterr().out


def test_feedback_failure_never_fails_the_workflow(tmp_path, capsys):
    class Down(FakeJira):
        def comment(self, key, text):
            raise ConnectionError("jira unreachable")

    assert feedback.main(["--head-ref", "feature/jira-ABC-12-auto-impl", "--exit-code", "0",
                          "--report-dir", str(tmp_path), "--pr-url", "u"], jira=Down()) == 0
    out = capsys.readouterr().out
    assert "::warning::" in out and "no report produced" not in out


def test_concurrent_events_for_one_ticket_open_one_pull_request(tmp_path):
    import threading
    import time

    from verification_compiler.jira.service import deliver

    class Slow(FakeGitHub):
        def open_pull(self, repo, branch):
            found = super().open_pull(repo, branch)
            time.sleep(0.05)  # widen the check-then-create window
            return found

    github, s = Slow(), settings(tmp_path)
    store = Store(s.state_path)
    events = [event(items=ASSIGNED, changelog_id="1"),
              event(items=[{"field": "status", "toString": "In Progress"}], changelog_id="2")]
    for i, body in enumerate(events, 1):
        assert store.claim(f"ABC-12:{i}", "ABC-12")
    threads = [threading.Thread(target=deliver, args=(body, s, github, store, f"ABC-12:{i}"))
               for i, body in enumerate(events, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(github.created) == 1
    assert sorted(j["status"] for j in store.get("ABC-12")) == ["pr_opened", "skipped"]


def test_invalid_content_length_is_a_client_error(bridge):
    client, _, _ = bridge
    raw, headers = signed(event(items=ASSIGNED))
    r = client.post("/webhooks/jira", content=raw, headers=dict(headers, **{"Content-Length": "abc"}))
    assert r.status_code in (400, 422)
