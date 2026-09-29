#!/usr/bin/env bash
# Install gVisor (runsc) and register it as a Docker runtime. Run as root (or via sudo).
#
#   sudo verification_compiler/scripts/install_gvisor.sh              # latest release
#   sudo GVISOR_RELEASE=20260921.0 verification_compiler/scripts/install_gvisor.sh   # pinned
#
# gVisor publishes release bundles (gvisor.tar.bz2 + .sha512). The older per-binary
# URLs (.../latest/<arch>/runsc) no longer exist. For signature-verified installs on
# Debian/Ubuntu, prefer the apt repository (see docs/ARCHITECTURE.md); this script
# checks integrity against the published SHA-512.
set -euo pipefail

RELEASE="${GVISOR_RELEASE:-latest}"
ARCH="$(uname -m)"
case "$ARCH" in x86_64|aarch64) ;; *) echo "unsupported architecture: $ARCH" >&2; exit 1 ;; esac
URL="https://storage.googleapis.com/gvisor/releases/release/${RELEASE}/${ARCH}"
PREFIX="${GVISOR_PREFIX:-/usr/local/bin}"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
curl -fsSLO "${URL}/gvisor.tar.bz2"
curl -fsSLO "${URL}/gvisor.tar.bz2.sha512"
sha512sum -c gvisor.tar.bz2.sha512
tar -xjf gvisor.tar.bz2

install -m 0755 runsc containerd-shim-runsc-v1 "$PREFIX/"
rm -rf "$PREFIX/gvisor-bin"
cp -r gvisor-bin "$PREFIX/gvisor-bin"
"$PREFIX/runsc" --version

"$PREFIX/runsc" install
if command -v systemctl >/dev/null && systemctl is-active --quiet docker 2>/dev/null; then
    systemctl restart docker
else
    echo "Restart the Docker daemon to load the runsc runtime." >&2
fi
