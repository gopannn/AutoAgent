# Production image for a verified service. Build context: package_image's staged output.
#   docker build -f verification_compiler/deploy/service.Dockerfile \
#     --build-arg RUNTIME_IMAGE=<verified digest> --build-arg APP_ENTRYPOINT=app.main:app <staged-context>
# Dependencies come only from the verified, hash-pinned lockfile.
ARG RUNTIME_IMAGE

FROM ${RUNTIME_IMAGE} AS deps
COPY requirements.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir --disable-pip-version-check --require-hashes --no-deps \
        --only-binary=:all: --target /deps -r /tmp/requirements.lock

FROM ${RUNTIME_IMAGE}
ARG APP_ENTRYPOINT
ENV PYTHONPATH=/deps:/app \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ENTRYPOINT=${APP_ENTRYPOINT}
COPY --from=deps /deps /deps
COPY app/ /app/
WORKDIR /app
USER 65534:65534
EXPOSE 8000
CMD ["sh", "-c", "exec python -m uvicorn \"$APP_ENTRYPOINT\" --host 0.0.0.0 --port 8000"]
