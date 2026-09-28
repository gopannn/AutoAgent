"""Deployment adapters.

The engine is a component, not a platform, and the proof of that claim is a
worked deployment against a system that already exists and was not designed
around it. `interiors` binds all three planes to a 48-sheet Excel ERP:

    record       the workbook's own register and governance table
    resolution   ctd.Resolver, deciding feasibility on evidence
    hypothesis   ctd.kernel pre-mortem, projecting failure mechanisms

Nothing in here reaches back into the engine. An adapter that required changes
to the kernel would mean the kernel was not a component after all.
"""

from .cross_domain import CROSS_DOMAIN_INCIDENTS
from .incidents import INTERIORS_INCIDENTS
from .schema_interiors import (
    EVIDENCE_PLANE, EXCEPTION_PREDICATE, INTERIORS_SCHEMA,
)

ALL_INCIDENTS = list(CROSS_DOMAIN_INCIDENTS) + list(INTERIORS_INCIDENTS)
"""The full 20-incident, 20-domain corpus — the evaluation default.

Eleven incidents was too few to resolve the ablations: every guard arm scored
identically and the release notes had to report that as a corpus limitation
rather than a result. At twenty the arms separate, and two components that
could not previously be shown to earn their place now demonstrably do —
removing kinship costs 68% of MRR, and removing subsumption demotion takes
hit@1 to zero.

The two halves were authored for different purposes, which is the nearest
thing to independence available without a real corpus: the cross-domain set
exists to be transferred from, the interiors set was written for a specific
ERP deployment. Evaluate with INTERIORS_SCHEMA, which is the superset.
"""

__all__ = [
    "ALL_INCIDENTS", "CROSS_DOMAIN_INCIDENTS", "INTERIORS_INCIDENTS",
    "INTERIORS_SCHEMA", "EVIDENCE_PLANE", "EXCEPTION_PREDICATE",
]
