"""Authorization — one implementation of one policy.

Before this module the same question — *may this principal see this record?* —
had three implementations that returned different answers:

    ctd.providers.GraphProvider._tenant_allowed        untagged -> VISIBLE
    ctd.structural_repository ...list                  untagged -> VISIBLE
    ctd.unified_close._authorized                      untagged -> HIDDEN

An untagged record being visible to every tenant is a cross-tenant disclosure,
and a disclosure whose occurrence depends on which code path happened to ask is
worse than either answer consistently applied: it cannot be reasoned about, it
cannot be tested once, and it will be discovered by a customer rather than by a
test. Duplicated policy logic diverges. That is not a risk, it is a schedule.

The resolved policy, and why each dimension resolves the way it does:

    tenant_id    FAIL CLOSED. A record with no tenant tag is not "shared", it
                 is unattributed, and in a multi-tenant deployment the cost of
                 hiding an unattributed record from its owner is a support
                 ticket while the cost of showing it to a stranger is a breach.
                 The two are not symmetric, so the default is not a matter of
                 taste.

    security_label
                 FAIL OPEN on absence, and this one is deliberate rather than
                 inherited. An absent label means *unclassified*, which is a
                 positive statement in every classification scheme this is
                 modelled on: material is classified by act, and unclassified
                 material is public by default. Reversing it would make every
                 label-scoped query return nothing until a full back-fill was
                 complete, which in practice means the scoping gets disabled.
                 A present label that is not permitted is always refused.

    authorization_scope
                 FAIL OPEN on absence, for the same reason and with the same
                 caveat: a record declaring scopes must match at least one.

The asymmetry between tenant and label is the point, not an oversight, so it is
stated here once and tested once rather than re-derived in three places.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

__all__ = [
    "AuthorizationFilter", "TENANT_POLICY", "LABEL_POLICY",
    "TenantMode", "UntaggedRecordWarning", "audit_tenant_tags", "TagAudit",
]


class UntaggedRecordWarning(UserWarning):
    """Raised once per filter when legacy mode admits an untagged record.

    Silent legacy modes never get turned off, because nobody is ever reminded
    they are on. A warning is the cheapest thing that keeps the migration
    moving without breaking the deployment on upgrade day.
    """


class TenantMode:
    STRICT = "strict"
    """An untagged record is hidden from every tenant. The safe default."""

    LEGACY = "legacy"
    """Pre-5.1 behaviour: an untagged record is visible to every tenant.

    Provided so an upgrade does not silently empty a deployment's result sets
    on the day it lands. It is a migration path with a deadline attached, not
    a supported configuration: run `audit_tenant_tags` to size the exposure,
    tag the records, then switch to STRICT.
    """

TENANT_POLICY = "fail-closed: an untagged record is hidden from every tenant"
LABEL_POLICY = "fail-open: an unlabelled record is unclassified, hence visible"


@dataclass(frozen=True)
class AuthorizationFilter:
    """The single authorization decision point.

    Constructed from a `RuntimePolicy`, a `Principal`, or raw values, so every
    caller in the system reaches the same code regardless of which layer it
    sits in.
    """

    tenant_id: str | None = None
    allowed_security_labels: frozenset[str] | None = None
    authorization_scope: frozenset[str] | None = None
    tenant_mode: str = TenantMode.STRICT

    # ---- construction --------------------------------------------------

    @classmethod
    def from_policy(cls, policy: Any) -> "AuthorizationFilter":
        return cls(
            tenant_id=getattr(policy, "tenant_id", None),
            allowed_security_labels=_frozen(
                getattr(policy, "allowed_security_labels", None)),
            authorization_scope=_frozen(
                getattr(policy, "authorization_scope", None)),
            tenant_mode=getattr(policy, "tenant_mode", TenantMode.STRICT),
        )

    @classmethod
    def unrestricted(cls) -> "AuthorizationFilter":
        return cls()

    @property
    def active(self) -> bool:
        return (self.tenant_id is not None
                or self.allowed_security_labels is not None
                or self.authorization_scope is not None)

    # ---- the decision --------------------------------------------------

    def tenant_allowed(self, tenant: Any) -> bool:
        """Fail closed. Absence is not permission.

        Under `TenantMode.LEGACY` an untagged record is admitted and a
        `UntaggedRecordWarning` is issued, so the migration is visible in the
        logs rather than forgotten.
        """
        if self.tenant_id is None:
            return True
        if tenant is None:
            if self.tenant_mode == TenantMode.LEGACY:
                warnings.warn(
                    "admitting an untagged record under tenant "
                    f"{self.tenant_id!r} because tenant_mode='legacy'. This is "
                    "pre-5.1 behaviour and exposes untagged records to every "
                    "tenant. Run ctd.authz.audit_tenant_tags to size the "
                    "exposure, tag the records, then switch to 'strict'.",
                    UntaggedRecordWarning, stacklevel=3)
                return True
            return False
        return tenant == self.tenant_id

    def label_allowed(self, label: Any) -> bool:
        """Fail open on absence; a declared label must be permitted."""
        if self.allowed_security_labels is None:
            return True
        if label is None:
            return True
        if "*" in self.allowed_security_labels:
            return True
        return label in self.allowed_security_labels

    def scope_allowed(self, scopes: Any) -> bool:
        if self.authorization_scope is None or scopes is None:
            return True
        if "*" in self.authorization_scope:
            return True
        declared = ({scopes} if isinstance(scopes, str)
                    else set(scopes) if isinstance(scopes, Iterable)
                    else {scopes})
        return bool(declared & set(self.authorization_scope))

    def allows(self, attributes: Mapping[str, Any] | None) -> bool:
        attrs = attributes or {}
        return (self.tenant_allowed(attrs.get("tenant_id"))
                and self.label_allowed(attrs.get("security_label"))
                and self.scope_allowed(attrs.get("authorization_scope")))

    def allows_object(self, obj: Any) -> bool:
        """For objects carrying the fields directly rather than in `attributes`
        — `StructuralCase`, for instance."""
        return (self.tenant_allowed(getattr(obj, "tenant_id", None))
                and self.label_allowed(getattr(obj, "security_label", None))
                and self.scope_allowed(
                    getattr(obj, "authorization_scope", None)))

    def refusal_reason(self, attributes: Mapping[str, Any] | None) -> str | None:
        """Why a record was withheld.

        A filter that silently drops rows produces result sets nobody can
        explain, and the first question after any surprising empty result is
        always whether it was filtered or genuinely absent.
        """
        attrs = attributes or {}
        tenant = attrs.get("tenant_id")
        if not self.tenant_allowed(tenant):
            return ("no tenant tag; withheld under a tenant-scoped policy"
                    if tenant is None
                    else f"belongs to tenant {tenant!r}")
        if not self.label_allowed(attrs.get("security_label")):
            return f"security label {attrs.get('security_label')!r} not permitted"
        if not self.scope_allowed(attrs.get("authorization_scope")):
            return "declares no authorization scope the principal holds"
        return None


def _frozen(value: Any) -> frozenset[str] | None:
    if value is None:
        return None
    return frozenset(value)


# --------------------------------------------------------------------------
# Migration support
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TagAudit:
    """How exposed a deployment is to the 5.1 tenant change.

    Run this BEFORE switching to strict mode. The number that matters is
    `untagged`: those records are visible to every tenant today and will become
    visible to none. Discovering that after the upgrade, from a user reporting
    an empty screen, is the expensive order to do it in.
    """
    total: int
    tagged: int
    untagged: int
    untagged_ids: tuple[str, ...]
    tenants: tuple[str, ...]

    @property
    def safe_to_switch(self) -> bool:
        return self.untagged == 0

    def __str__(self) -> str:
        head = (f"{self.total} record(s): {self.tagged} tagged across "
                f"{len(self.tenants)} tenant(s), {self.untagged} untagged")
        if self.safe_to_switch:
            return head + "\nSafe to switch tenant_mode to 'strict'."
        shown = ", ".join(self.untagged_ids[:10])
        more = "" if self.untagged <= 10 else f" (+{self.untagged - 10} more)"
        return (head + "\nNOT safe to switch: these records are visible to "
                f"every tenant today and will become visible to none.\n"
                f"  {shown}{more}")


def audit_tenant_tags(records: Iterable[Any],
                      *, attribute: str = "tenant_id") -> TagAudit:
    """Count tenant tags across anything carrying `attributes` or the field."""
    total = tagged = 0
    untagged_ids: list[str] = []
    tenants: set[str] = set()
    for rec in records:
        total += 1
        attrs = getattr(rec, "attributes", None) or getattr(rec, "attrs", None)
        value = (attrs.get(attribute) if isinstance(attrs, Mapping)
                 else getattr(rec, attribute, None))
        if value is None:
            untagged_ids.append(str(getattr(rec, "id", f"<row {total}>")))
        else:
            tagged += 1
            tenants.add(str(value))
    return TagAudit(total, tagged, len(untagged_ids),
                    tuple(untagged_ids), tuple(sorted(tenants)))
