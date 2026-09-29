"""FastAPI webhook receiver.

    uvicorn verification_compiler.jira.app:create_app --factory --host 0.0.0.0 --port 8080

POST /webhooks/jira   authenticated Jira webhook; 202 when accepted, 200 when deliberately ignored
GET  /jobs/{key}      job history for an issue (requires the same secret as a bearer token)
GET  /healthz         liveness
"""
from __future__ import annotations

import hmac
import json
import logging

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .clients import GitHub, GitHubClient
from .config import BridgeSettings
from .normalize import ISSUE_KEY
from .service import Store, deliver, should_act, verify_signature, verify_token

log = logging.getLogger("verification_compiler.jira")


def create_app(settings: BridgeSettings | None = None, github: GitHub | None = None,
               store: Store | None = None) -> FastAPI:
    settings = settings or BridgeSettings.from_env()
    github = github or GitHubClient(settings.github_token, settings.github_api)
    store = store or Store(settings.state_path)
    app = FastAPI(title="Jira → AI compiler bridge", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/webhooks/jira")
    async def webhook(request: Request, background: BackgroundTasks,
                      token: str | None = Query(default=None)) -> JSONResponse:
        declared = request.headers.get("content-length") or "0"
        if not declared.isdigit():
            raise HTTPException(400, "invalid content-length")
        if int(declared) > settings.max_body_bytes:
            raise HTTPException(413, "payload too large")
        body = await request.body()
        if len(body) > settings.max_body_bytes:
            raise HTTPException(413, "payload too large")
        if settings.auth_mode == "hmac":
            authentic = verify_signature(settings.webhook_secret, body, request.headers.get(settings.signature_header))
        else:
            authentic = verify_token(settings.webhook_secret, token)
        if not authentic:
            raise HTTPException(401, "invalid webhook signature")
        try:
            event = json.loads(body)
        except ValueError as err:
            raise HTTPException(400, "body is not JSON") from err
        if not isinstance(event, dict):
            raise HTTPException(400, "body is not a JSON object")

        decision = should_act(event, settings)
        if not decision.act:
            return JSONResponse({"status": "ignored", "reason": decision.reason}, status_code=200)
        if not store.claim(decision.idempotency_key, event["issue"]["key"]):
            return JSONResponse({"status": "duplicate"}, status_code=200)
        background.add_task(deliver, event, settings, github, store, decision.idempotency_key)
        return JSONResponse({"status": "accepted", "idempotency_key": decision.idempotency_key}, status_code=202)

    @app.get("/jobs/{key}")
    def jobs(key: str, authorization: str | None = Header(default=None)) -> dict:
        expected = f"Bearer {settings.webhook_secret}"
        if not authorization or not hmac.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(401, "unauthorized")
        if not ISSUE_KEY.match(key):
            raise HTTPException(400, "invalid issue key")
        return {"jira_key": key, "jobs": store.get(key)}

    return app
