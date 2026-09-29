#!/usr/bin/env bash
# Local end-to-end check of the sandbox under gVisor. Builds the verifier image, pins both
# images by digest and runs the real-container test suite with gVisor REQUIRED.
# Needs: Docker with the runsc runtime (install_gvisor.sh), uv, pip-audit, network for PyPI.
set -euo pipefail
cd "$(dirname "$0")/../.."

docker info --format '{{json .Runtimes}}' | grep -q '"runsc"' \
    || { echo "runsc is not registered with Docker; run install_gvisor.sh first" >&2; exit 1; }
docker run --rm --runtime=runsc --network=none python:3.12-slim dmesg | grep -q "Starting gVisor" \
    || { echo "container did not boot under gVisor" >&2; exit 1; }

docker build -q -f verification_compiler/docker/verifier.Dockerfile -t vc-verifier:local verification_compiler/docker
docker pull -q python:3.12-slim >/dev/null
VC_E2E_VERIFIER_IMAGE="$(docker image inspect --format '{{.Id}}' vc-verifier:local)"
VC_E2E_RUNTIME_IMAGE="$(docker image inspect --format '{{index .RepoDigests 0}}' python:3.12-slim)"
export VC_E2E_VERIFIER_IMAGE VC_E2E_RUNTIME_IMAGE
unset VC_E2E_ALLOW_RUNC

python -m pytest verification_compiler/tests/test_sandbox_docker.py -v
