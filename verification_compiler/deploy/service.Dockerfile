# Production image for a verified service. Build context: repository root.
#   docker build -f verification_compiler/deploy/service.Dockerfile \
#     --build-arg PROJECT_ROOT=services/api --build-arg APP_ENTRYPOINT=app.main:app .
# Dependencies come only from the verified, hash-pinned lockfile.
ARG RUNTIME_IMAGE=python:3.12-slim

FROM ${RUNTIME_IMAGE} AS deps
COPY .verification/requirements.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir --disable-pip-version-check --require-hashes --no-deps \
        --only-binary=:all: --target /deps -r /tmp/requirements.lock

FROM ${RUNTIME_IMAGE}
ARG PROJECT_ROOT
ARG APP_ENTRYPOINT
ENV PYTHONPATH=/deps:/app \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ENTRYPOINT=${APP_ENTRYPOINT}
COPY --from=deps /deps /deps
COPY ${PROJECT_ROOT}/ /app/
WORKDIR /app
USER 65534:65534
EXPOSE 8000
CMD ["sh", "-c", "exec python -m uvicorn \"$APP_ENTRYPOINT\" --host 0.0.0.0 --port 8000"]
