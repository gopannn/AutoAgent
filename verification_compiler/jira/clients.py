"""Minimal GitHub and Jira REST clients (httpx). Both are injectable, so the service is testable offline."""
from __future__ import annotations

import base64
from typing import Protocol

import httpx


class GitHub(Protocol):
    def branch_sha(self, repo: str, branch: str) -> str | None: ...
    def create_branch(self, repo: str, branch: str, sha: str) -> bool: ...
    def put_file(self, repo: str, branch: str, path: str, content: str, message: str) -> None: ...
    def open_pull(self, repo: str, branch: str) -> dict | None: ...
    def create_pull(self, repo: str, branch: str, base: str, title: str, body: str) -> dict: ...


class GitHubClient:
    def __init__(self, token: str, api: str = "https://api.github.com", timeout: float = 20.0):
        self.http = httpx.Client(base_url=api, timeout=timeout, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def branch_sha(self, repo: str, branch: str) -> str | None:
        r = self.http.get(f"/repos/{repo}/git/ref/heads/{branch}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()["object"]["sha"]

    def create_branch(self, repo: str, branch: str, sha: str) -> bool:
        """True if created, False if it already existed."""
        r = self.http.post(f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha})
        if r.status_code == 422 and "already exists" in r.text:
            return False
        r.raise_for_status()
        return True

    def put_file(self, repo: str, branch: str, path: str, content: str, message: str) -> None:
        existing = self.http.get(f"/repos/{repo}/contents/{path}", params={"ref": branch})
        payload = {"message": message, "branch": branch,
                   "content": base64.b64encode(content.encode("utf-8")).decode("ascii")}
        if existing.status_code == 200:
            payload["sha"] = existing.json()["sha"]
        elif existing.status_code != 404:
            existing.raise_for_status()
        self.http.put(f"/repos/{repo}/contents/{path}", json=payload).raise_for_status()

    def open_pull(self, repo: str, branch: str) -> dict | None:
        owner = repo.split("/")[0]
        r = self.http.get(f"/repos/{repo}/pulls", params={"head": f"{owner}:{branch}", "state": "open"})
        r.raise_for_status()
        pulls = r.json()
        return pulls[0] if pulls else None

    def create_pull(self, repo: str, branch: str, base: str, title: str, body: str) -> dict:
        r = self.http.post(f"/repos/{repo}/pulls", json={"title": title, "head": branch, "base": base, "body": body})
        r.raise_for_status()
        return r.json()


class Jira(Protocol):
    def comment(self, key: str, text: str) -> None: ...
    def transition(self, key: str, status_name: str) -> bool: ...


class JiraClient:
    """Jira Cloud REST v2 (plain-text comment bodies) with email + API token."""

    def __init__(self, base_url: str, email: str, api_token: str, timeout: float = 20.0):
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, auth=(email, api_token),
                                 headers={"Accept": "application/json"})

    def comment(self, key: str, text: str) -> None:
        self.http.post(f"/rest/api/2/issue/{key}/comment", json={"body": text[:30_000]}).raise_for_status()

    def transition(self, key: str, status_name: str) -> bool:
        """Moves the issue through the transition whose target status has this name. False if none exists."""
        r = self.http.get(f"/rest/api/2/issue/{key}/transitions")
        r.raise_for_status()
        for t in r.json().get("transitions", []):
            if (t.get("to") or {}).get("name", "").casefold() == status_name.casefold():
                self.http.post(f"/rest/api/2/issue/{key}/transitions",
                               json={"transition": {"id": t["id"]}}).raise_for_status()
                return True
        return False
