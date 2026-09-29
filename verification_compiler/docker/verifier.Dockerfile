# Verifier image: static analysis tools plus the black-box test runner.
# Build it, then pin the result by digest in VC_VERIFIER_IMAGE:
#   docker build -f docker/verifier.Dockerfile -t vc-verifier:1 docker/
#   export VC_VERIFIER_IMAGE=$(docker image inspect --format '{{.Id}}' vc-verifier:1)
# For reproducible builds, replace the base tag with its @sha256 digest.
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    PATH=/opt/node-global/bin:$PATH \
    SEMGREP_SEND_METRICS=off

RUN pip install \
        ruff==0.5.1 \
        pytest==8.2.2 \
        semgrep==1.78.0 \
        httpx==0.27.0 \
        PyJWT==2.8.0 \
        nodejs-wheel==20.18.0 \
 && npm install --global --prefix /opt/node-global pyright@1.1.371 \
 && pyright --version \
 && chmod -R a+rX /opt/node-global

COPY semgrep-rules /opt/semgrep-rules
RUN chmod -R a+rX /opt/semgrep-rules

USER 65534:65534
