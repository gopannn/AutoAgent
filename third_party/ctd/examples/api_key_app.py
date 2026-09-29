"""Example production ASGI entry point for CTD API-key authentication.

The key hashes are supplied through CTD_API_KEY_HASHES. Identity/role metadata is
application-owned and intentionally explicit rather than inferred from a key ID.
"""

from ctd.api import create_app
from ctd.auth import ApiKeyIdentityStore, Principal
from ctd.config import EngineSettings

settings = EngineSettings.from_env()
identities = ApiKeyIdentityStore(
    {
        "analyst": Principal(
            subject="analyst-service",
            roles={"analyst"},
            scopes={"resolve:read", "resolve:advanced"},
            security_labels={"PUBLIC", "INTERNAL", "CONFIDENTIAL"},
            auth_method="api_key",
        )
    }
)

app = create_app(settings=settings, identity_store=identities)
