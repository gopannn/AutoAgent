# Jira → AI compiler webhook bridge. Build context: repository root.
#   docker build -f verification_compiler/deploy/jira-bridge.Dockerfile -t jira-bridge .
#   docker run -p 8080:8080 -v jira-bridge:/state \
#     -e JIRA_WEBHOOK_SECRET=... -e JIRA_BOT_ACCOUNT_ID=... -e GITHUB_TOKEN=... \
#     -e JIRA_PROJECT_REPOS='{"ABC": {"repo": "org/service", "base_branch": "main"}}' jira-bridge
# The bridge holds a GitHub token that can create branches, commit one request file and open PRs;
# it never runs models or executes ticket content.
ARG RUNTIME_IMAGE=python:3.12-slim
FROM ${RUNTIME_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app \
    JIRA_BRIDGE_STATE=/state/jira_bridge.sqlite3
COPY verification_compiler/jira/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check -r /tmp/requirements.txt
# Only the bridge and the package root (whose __init__ needs config.py); no models, sandbox or graph.
COPY verification_compiler/__init__.py verification_compiler/config.py /app/verification_compiler/
COPY verification_compiler/jira/ /app/verification_compiler/jira/
RUN mkdir -p /state && chown 65534:65534 /state
WORKDIR /app
USER 65534:65534
VOLUME ["/state"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"]
CMD ["python", "-m", "uvicorn", "verification_compiler.jira.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
