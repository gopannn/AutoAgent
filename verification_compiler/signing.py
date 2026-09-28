"""Optional Ed25519 signature over the canonical release manifest."""
from __future__ import annotations

import base64
from pathlib import Path

from .errors import InfrastructureError
from .hashing import canonical_json, sha256_hex


def _payload(manifest: dict) -> bytes:
    return canonical_json({k: v for k, v in manifest.items() if k != "signature"}).encode("utf-8")


def sign_manifest(manifest: dict, key_path: str | None) -> dict | None:
    if not key_path:
        return None
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as err:
        raise InfrastructureError("a signing key is configured but 'cryptography' is not installed") from err
    key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise InfrastructureError("signing key must be an unencrypted Ed25519 PEM private key")
    payload = _payload(manifest)
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {
        "algorithm": "ed25519",
        "public_key": public.hex(),
        "payload_sha256": sha256_hex(payload),
        "signature": base64.b64encode(key.sign(payload)).decode("ascii"),
    }


def verify_manifest(manifest: dict, trusted_public_key_hex: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    sig = manifest.get("signature") or {}
    if sig.get("algorithm") != "ed25519" or sig.get("public_key") != trusted_public_key_hex:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(trusted_public_key_hex)).verify(
            base64.b64decode(sig["signature"]), _payload(manifest)
        )
    except (InvalidSignature, KeyError, ValueError):
        return False
    return True
