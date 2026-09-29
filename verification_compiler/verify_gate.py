"""Deployment gate: a signed manifest is only accepted for the code it actually describes.

    python -m verification_compiler.verify_gate --project-root services/api --entrypoint app.main:app \
        --identity "https://github.com/ORG/REPO/.github/workflows/ai-compiler.yml@refs/heads/main"

Checks, all of which must pass:
  1. the Sigstore bundle verifies the manifest (keyless identity + issuer, or --key),
  2. the manifest says release_ready, carries passing evidence, and its decision is RESOLVED/JUSTIFIED,
  3. the codebase hash recomputed from the files on disk equals the manifest's,
  4. the committed lockfile hashes to the manifest's lockfile hash,
  5. optionally, the embedded Ed25519 signature verifies against a trusted key.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Callable

from .hashing import codebase_hash, sha256_hex
from .repo_io import ProjectError, load_codebase
from .schemas import VerificationResult

GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
Runner = Callable[..., subprocess.CompletedProcess]


def verify_signature(manifest: Path, bundle: Path, *, identity: str | None, issuer: str, key: Path | None,
                     cosign: str = "cosign", runner: Runner = subprocess.run) -> str | None:
    """Returns None if the bundle verifies, else the reason it does not."""
    if not bundle.is_file():
        return f"signature bundle {bundle} not found"
    cmd = [cosign, "verify-blob", str(manifest), "--bundle", str(bundle)]
    if key:
        cmd += ["--key", str(key)]
    elif identity:
        cmd += ["--certificate-identity", identity, "--certificate-oidc-issuer", issuer]
    else:
        return "either --identity (keyless) or --key is required"
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as err:
        return f"cosign could not run: {err!r}"
    if proc.returncode != 0:
        return f"cosign verification failed: {(proc.stderr or proc.stdout).strip()[:1000]}"
    return None


def check(manifest_dir: Path, project_root: Path, entrypoint: str, *, identity: str | None, issuer: str,
          key: Path | None = None, ed25519_public_key: str | None = None, runner: Runner = subprocess.run) -> list[str]:
    manifest_path = manifest_dir / "release_manifest.json"
    problems: list[str] = []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        return [f"cannot read manifest: {err}"]

    reason = verify_signature(manifest_path, manifest_dir / "release_manifest.sigstore.json",
                              identity=identity, issuer=issuer, key=key, runner=runner)
    if reason:
        problems.append(reason)

    if manifest.get("status") != "release_ready":
        problems.append(f"manifest status is {manifest.get('status')!r}")
    decided = manifest.get("decision") or {}
    if decided.get("ctd_outcome") != "RESOLVED" or decided.get("epistemic_verdict") != "JUSTIFIED":
        problems.append(f"decision is {decided.get('ctd_outcome')}/{decided.get('epistemic_verdict')}, "
                        "not RESOLVED/JUSTIFIED")
    try:
        if not VerificationResult(**manifest["verification_evidence"]).passed:
            problems.append("manifest evidence is not passing")
    except (KeyError, TypeError, ValueError) as err:
        problems.append(f"manifest evidence unreadable: {err}")

    try:
        on_disk = codebase_hash(load_codebase(project_root, entrypoint, scan_secrets=False).codebase)
    except ProjectError as err:
        problems.append(f"cannot load project: {err}")
    else:
        if on_disk != manifest.get("codebase_hash"):
            problems.append(
                f"code on disk ({on_disk[:16]}...) is not the verified code "
                f"({str(manifest.get('codebase_hash'))[:16]}...); it changed after verification"
            )

    lock = manifest_dir / "requirements.lock"
    if not lock.is_file() or sha256_hex(lock.read_text(encoding="utf-8")) != manifest.get("lockfile_hash"):
        problems.append("committed lockfile does not match the verified lockfile")

    if ed25519_public_key:
        from .signing import verify_manifest

        if not verify_manifest(manifest, ed25519_public_key):
            problems.append("embedded Ed25519 signature does not verify against the trusted key")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verification_compiler.verify_gate")
    parser.add_argument("--manifest-dir", type=Path, default=Path(".verification"))
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--entrypoint", required=True)
    parser.add_argument("--identity", help="expected keyless certificate identity (workflow ref)")
    parser.add_argument("--issuer", default=GITHUB_OIDC_ISSUER)
    parser.add_argument("--key", type=Path, help="cosign public key (key-based signing instead of keyless)")
    parser.add_argument("--ed25519-public-key", help="hex public key for the manifest's embedded signature")
    args = parser.parse_args(argv)

    problems = check(args.manifest_dir, args.project_root, args.entrypoint, identity=args.identity,
                     issuer=args.issuer, key=args.key, ed25519_public_key=args.ed25519_public_key)
    if problems:
        print("DEPLOYMENT BLOCKED:", *[f"  - {p}" for p in problems], sep="\n", file=sys.stderr)
        return 1
    print("Deployment gate passed: signed manifest matches the code on disk.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
