"""Interiors incidents, encoded from the workbook's own taxonomy.

Nothing here is invented. Every incident is one of the failure mechanisms the
workbook already names — the twelve exception types on 32_EXCEPTION_QUEUE and
the nine canonical revision reason groups on 35_ENUMERATIONS. The firm has
already done the hard analytical work of enumerating how its projects go wrong;
what was missing was a typed relational encoding of the *mechanism*, so the
list could do something other than sit there.

Severities are the ones the exception queue already assigns (High=5, Medium=3,
Low=2), so the priority ranking inherits the firm's own judgement rather than a
number this file made up.

Check procedures are written against things the workbook already measures —
04_PROJECTS columns, log rows, 21_RESEARCH_COMPONENTS review flags. A predicted
failure whose check names a cell someone can go and read is the whole
deliverable; one that says "monitor closely" is not.
"""

from __future__ import annotations

from ..kernel import R, Record

__all__ = ["INTERIORS_INCIDENTS"]

HIGH, MED, LOW = 5, 3, 2


REWORK_SPIRAL = Record(
    id="rework-spiral",
    domain="design-delivery",
    attrs={
        "incident": True, "severity": HIGH,
        "independence_group": "customer-change",
        "exception_type": "REVISION_WITHOUT_REASON",
        "checks": {
            "RETRIES": "Count revision tasks against {1} in the 05x logs "
                       "rows 52-56. More than two per design stage with no "
                       "reason logged is the spiral, not normal iteration.",
            "AMPLIFIES": "Compare 04_PROJECTS Y (Revisions) against U (Tasks "
                         "Logged) week on week. A rising ratio while scope is "
                         "nominally frozen is amplification.",
            "SATURATES": "Read 04_PROJECTS AW against AX. A stage past its "
                         "SLA while revisions are still arriving will not "
                         "recover by working harder.",
        },
    },
    types={"client": "CLIENT", "design-stage": "STAGE",
           "scope-change": "SCOPE", "days-in-stage": "ELAPSED"},
    rels=(
        R("DEPENDS", "client", "design-stage"),
        R("CAUSES", R("EXCESS", "scope-change"),
                    R("QUEUEING", "design-stage")),
        R("INCREASES", R("QUEUEING", "design-stage"), "days-in-stage"),
        R("SENSES", "client", "days-in-stage"),
        R("CAUSES", R("SENSES", "client", "days-in-stage"),
                    R("RETRIES", "client", "design-stage")),
        R("CAUSES", R("RETRIES", "client", "design-stage"),
                    R("AMPLIFIES", "scope-change", "design-stage")),
        R("CAUSES", R("AMPLIFIES", "scope-change", "design-stage"),
                    R("SATURATES", "design-stage")),
    ),
    text="Each round of changes lengthened the stage, which gave the client "
         "longer to think of more changes. CUSTOMER_CHANGE.",
)

BRIEF_GAP = Record(
    id="brief-gap-rework",
    domain="briefing",
    attrs={
        "incident": True, "severity": HIGH,
        "independence_group": "brief-incomplete",
        "exception_type": "BRIEF_INCOMPLETE",
        "checks": {
            "INCOMPLETE": "04_PROJECTS AT — brief completeness below 100%. "
                          "AU names the missing fields. This is measured, not "
                          "estimated.",
            "RETRIES": "Cross-read 18_LEARNING block E: revisions per task "
                       "against brief completeness for the same project.",
            "BREACHES": "04_PROJECTS AY. A stage that started on an "
                        "incomplete brief breaches its SLA while waiting for "
                        "information that was never captured.",
        },
    },
    types={"designer": "DESIGNER", "brief": "BRIEF", "design-stage": "STAGE",
           "rework": "REVISIONS", "completeness": "COMPLETENESS"},
    rels=(
        R("DEPENDS", "designer", "brief"),
        R("INCOMPLETE", "brief"),
        R("CAUSES", R("INCOMPLETE", "brief"), R("QUEUEING", "design-stage")),
        R("INCREASES", R("QUEUEING", "design-stage"), "completeness"),
        R("CAUSES", R("QUEUEING", "design-stage"),
                    R("RETRIES", "designer", "design-stage")),
        R("CAUSES", R("RETRIES", "designer", "design-stage"),
                    R("AMPLIFIES", "rework", "design-stage")),
        R("CAUSES", R("AMPLIFIES", "rework", "design-stage"),
                    R("BREACHES", "design-stage")),
    ),
    text="Design started before district, property type and carpet area were "
         "captured. Every assumption that turned out wrong became a revision.",
)

PAYMENT_STALL = Record(
    id="payment-block-stall",
    domain="cashflow",
    attrs={
        "incident": True, "severity": HIGH,
        "independence_group": "payment",
        "exception_type": "PAYMENT_BLOCK",
        "checks": {
            "WITHHOLDS": "17_PAYMENTS — any tranche DUE or OVERDUE against "
                         "this project. 04_PROJECTS BP shows the resulting "
                         "gate state.",
            "BREACHES": "04_PROJECTS AY. A stage blocked on a gate still "
                        "ages against its SLA; the clock does not stop.",
            "DELAYED": "04_PROJECTS BI — slip against target. Compare with "
                       "the date the tranche fell due.",
        },
    },
    types={"client": "CLIENT", "ops": "MANAGER", "cash": "CASH",
           "production-stage": "STAGE", "backlog": "WORKLOAD",
           "overdue-days": "ELAPSED"},
    rels=(
        R("DEPENDS", "ops", "cash"),
        R("CAUSES", R("SCARCE", "cash"), R("QUEUEING", "production-stage")),
        R("INCREASES", R("QUEUEING", "production-stage"), "overdue-days"),
        R("SENSES", "client", "overdue-days"),
        R("CAUSES", R("SENSES", "client", "overdue-days"),
                    R("WITHHOLDS", "client", "cash")),
        R("CAUSES", R("WITHHOLDS", "client", "cash"),
                    R("AMPLIFIES", "backlog", "production-stage")),
        R("CAUSES", R("AMPLIFIES", "backlog", "production-stage"),
                    R("BREACHES", "production-stage")),
    ),
    text="The tranche went unpaid, production was gated, the client saw no "
         "progress and held the next tranche too.",
)

DESIGNER_OVERLOAD = Record(
    id="designer-overload",
    domain="capacity",
    attrs={
        "incident": True, "severity": MED,
        "independence_group": "capacity",
        "exception_type": "OVER_CAPACITY",
        "checks": {
            "SATURATES": "16_QUALITY_GATES block C and each 05x log row 65 — "
                         "planned hours above daily capacity for the same "
                         "person on consecutive days.",
            "DEGRADES": "07_DESIGN_DASHBOARD — revision rate for that "
                        "designer against the firm rate over the same weeks.",
            "DELAYED": "04_PROJECTS BG, days per stage for that designer "
                       "against 18_LEARNING block D.",
        },
    },
    types={"designer": "DESIGNER", "schedule": "SCHEDULE",
           "workload": "WORKLOAD", "hours-gap": "ELAPSED"},
    rels=(
        R("DEPENDS", "designer", "schedule"),
        R("CAUSES", R("EXCESS", "workload"), R("QUEUEING", "schedule")),
        R("INCREASES", R("QUEUEING", "schedule"), "hours-gap"),
        R("CAUSES", R("QUEUEING", "schedule"), R("SATURATES", "schedule")),
        R("CAUSES", R("SATURATES", "schedule"), R("DEGRADES", "designer")),
        R("CAUSES", R("DEGRADES", "designer"),
                    R("AMPLIFIES", "workload", "schedule")),
    ),
    text="One designer carried three active projects. Quality fell, which "
         "produced rework, which added load to the same person.",
)

STALE_RATE = Record(
    id="stale-rate-quote",
    domain="estimating",
    attrs={
        "incident": True, "severity": MED,
        "independence_group": "research-freshness",
        "exception_type": "STALE_RESEARCH_RATE",
        "checks": {
            "STALE": "21_RESEARCH_COMPONENTS J8:J20 — any rate flagged "
                     "REVIEW. 37_GOVERNANCE block B gives the window.",
            "RETRIES": "Compare 24_COMPONENT_ESTIMATOR against the signed "
                       "BOQ. A gap above the firm's tolerance means the "
                       "estimate was rebuilt after quoting.",
            "INCOMPLETE": "39_ESTIMATE_LANES — do the three lanes agree? "
                          "Divergence above the band means one lane is "
                          "priced off stale data.",
        },
    },
    types={"designer": "DESIGNER", "estimate": "BRIEF",
           "rate-drift": "SCOPE", "quoted-price": "PRICE"},
    rels=(
        R("DEPENDS", "designer", "estimate"),
        R("STALE", "quoted-price"),
        R("CAUSES", R("STALE", "quoted-price"),
                    R("EXCESS", "rate-drift")),
        R("INCREASES", R("EXCESS", "rate-drift"), "quoted-price"),
        R("CAUSES", R("EXCESS", "rate-drift"),
                    R("RETRIES", "designer", "estimate")),
        R("CAUSES", R("RETRIES", "designer", "estimate"),
                    R("BREACHES", "estimate")),
    ),
    text="The estimate was built on rates past their review window. The BOQ "
         "came back above the quote and the estimate was rebuilt.",
)

SEQUENCE_VIOLATION = Record(
    id="premature-work",
    domain="sequencing",
    attrs={
        "incident": True, "severity": HIGH,
        "independence_group": "sequence",
        "exception_type": "SEQUENCE_VIOLATION",
        "checks": {
            "FREEZES": "04_PROJECTS BK:BN — appliance, material, automation "
                       "and wet-work freezes. Check each was set BEFORE the "
                       "stage that depends on it started (AV).",
            "RETRIES": "Any task redone after a later freeze landed. The 05x "
                       "logs carry the dates.",
            "CAUSES": "04_PROJECTS BF, sequence check, and BO gate status.",
        },
    },
    types={"crew": "CREW", "site": "SITE", "finishes-stage": "STAGE",
           "redo": "REVISIONS"},
    rels=(
        R("DEPENDS", "crew", "site"),
        R("CAUSES", R("INCOMPLETE", "site"),
                    R("QUEUEING", "finishes-stage")),
        R("PREVENTS", R("FREEZES", "crew", "site"),
                      R("RETRIES", "crew", "finishes-stage")),
        R("CAUSES", R("INCOMPLETE", "site"),
                    R("RETRIES", "crew", "finishes-stage")),
        R("CAUSES", R("RETRIES", "crew", "finishes-stage"),
                    R("AMPLIFIES", "redo", "finishes-stage")),
        R("CAUSES", R("AMPLIFIES", "redo", "finishes-stage"),
                    R("BREACHES", "finishes-stage")),
    ),
    text="Finishes started before wet-work sign-off. The wet work was then "
         "done twice and the second pass damaged the first finish.",
)

SITE_MISMATCH = Record(
    id="site-mismatch",
    domain="site-survey",
    attrs={
        "incident": True, "severity": HIGH,
        "independence_group": "site-error",
        "exception_type": "SITE_INFORMATION_MISSING",
        "checks": {
            "INCOMPLETE": "04_PROJECTS AT and AU. 32_EXCEPTION_QUEUE row 11 "
                          "counts this across the register.",
            "RETRIES": "Revision tasks whose reason is SITE_ERROR, from the "
                       "05x log reason rows.",
            "DEGRADES": "Modules that had to be re-cut. Compare the released "
                        "BOQ quantities against the installed quantities.",
        },
    },
    types={"designer": "DESIGNER", "site": "SITE", "cabinetry": "STAGE",
           "remake": "REVISIONS", "measured-gap": "COMPLETENESS"},
    rels=(
        R("DEPENDS", "designer", "site"),
        R("INCOMPLETE", "site"),
        R("CAUSES", R("INCOMPLETE", "site"), R("QUEUEING", "cabinetry")),
        R("INCREASES", R("QUEUEING", "cabinetry"), "measured-gap"),
        R("CAUSES", R("QUEUEING", "cabinetry"),
                    R("RETRIES", "designer", "cabinetry")),
        R("CAUSES", R("RETRIES", "designer", "cabinetry"),
                    R("AMPLIFIES", "remake", "cabinetry")),
        R("CAUSES", R("AMPLIFIES", "remake", "cabinetry"),
                    R("FAILS", "cabinetry")),
    ),
    text="Drawings were made off a builder's plan rather than a measured "
         "survey. Every unit was 40mm out and the run had to be remade.",
)

VENDOR_LEADTIME = Record(
    id="vendor-leadtime",
    domain="procurement",
    attrs={
        "incident": True, "severity": MED,
        "independence_group": "vendor",
        "exception_type": "FORECAST_SLIP",
        "checks": {
            "DELAYED": "Compare the quoted lead time against the stage start "
                       "date on 04_PROJECTS AV for the stage that consumes it.",
            "OSCILLATES": "Order quantities by week. Ordering ahead to cover "
                          "a lengthening lead time is the bullwhip signature.",
            "BREACHES": "04_PROJECTS BI, slip against target handover.",
        },
    },
    types={"ops": "MANAGER", "supply-line": "CHANNEL",
           "install-stage": "STAGE", "orders": "WORKLOAD",
           "lead-time": "ELAPSED"},
    rels=(
        R("DEPENDS", "ops", "supply-line"),
        R("CAUSES", R("EXCESS", "orders"), R("QUEUEING", "install-stage")),
        R("INCREASES", R("QUEUEING", "install-stage"), "lead-time"),
        R("SENSES", "ops", "lead-time"),
        R("CAUSES", R("DELAYED", "lead-time"), R("OSCILLATES", "orders")),
        R("CAUSES", R("OSCILLATES", "orders"),
                    R("AMPLIFIES", "orders", "install-stage")),
        R("CAUSES", R("AMPLIFIES", "orders", "install-stage"),
                    R("BREACHES", "install-stage")),
    ),
    text="Lead times stretched, so ops ordered earlier and larger. The vendor "
         "saw demand swing far harder than the project schedule did.",
)

APPROVAL_STALL = Record(
    id="approval-stall",
    domain="governance",
    attrs={
        "incident": True, "severity": MED,
        "independence_group": "approval",
        "exception_type": "APPROVAL_OVERDUE",
        "checks": {
            "DELAYED": "Each 05x log row 62 — days a manager sign-off has "
                       "been outstanding. 32_EXCEPTION_QUEUE row 13 totals it.",
            "OSCILLATES": "Plan-versus-done by day for the affected person "
                          "while the approval is outstanding.",
            "BREACHES": "04_PROJECTS AY for the stage awaiting the approval.",
        },
    },
    types={"manager": "MANAGER", "review-stage": "STAGE",
           "queue": "WORKLOAD", "waiting-days": "ELAPSED"},
    rels=(
        R("DEPENDS", "manager", "review-stage"),
        R("CAUSES", R("EXCESS", "queue"), R("QUEUEING", "review-stage")),
        R("INCREASES", R("QUEUEING", "review-stage"), "waiting-days"),
        R("CAUSES", R("DELAYED", "waiting-days"), R("OSCILLATES", "queue")),
        R("CAUSES", R("OSCILLATES", "queue"),
                    R("BREACHES", "review-stage")),
    ),
    text="Sign-offs arrived in batches after several days. Work downstream "
         "alternated between idle and overloaded.",
)

INTERIORS_INCIDENTS = [
    REWORK_SPIRAL, BRIEF_GAP, PAYMENT_STALL, DESIGNER_OVERLOAD, STALE_RATE,
    SEQUENCE_VIOLATION, SITE_MISMATCH, VENDOR_LEADTIME, APPROVAL_STALL,
]
