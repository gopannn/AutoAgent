"""Topological engine, v5.

Two modes over one store:

    CLOSE     returns the match      -- total satisfaction, fail-closed
    TRANSFER  returns the leftover   -- partial satisfaction, hypotheses

Both are "find the complement of a partial description". CLOSE demands every
constraint be satisfied and hands back the record; TRANSFER accepts partial
alignment and hands back the part that did not align, rewritten for the target.
PREMORTEM is TRANSFER pointed at incident history.

v5 adds the three things v4 was missing rather than the three things it already
had: a declared schema the encoder is checked against, a search that can revise
a bad early commitment, and a measurement of whether any of it works.
"""

from .bitset import count as bit_count, indices as bit_indices
from .close import run_close
from .encoding import (
    CORE_VOCABULARY, Finding, Validation, gloss, summary, validate,
    validate_all,
)
from .engine import Telemetry, TopologicalEngine
from .evaluate import (
    AblationResult, ClosureScorecard, HeldOut, QueryResult, evaluate_library,
    format_ablations, run_ablations, score_closures, split_record,
)
from .premortem import Prediction, PremortemRound, report, run_premortem
from .query import (
    Budget, Closure, Constraint, DataRequest, Outcome, Oracle,
    RelaxationPolicy, SlotSpec, Stats, Tier, Verdict,
    ask, has, lexical, where,
)
from .schema import ANY, ENT, REL, CORE_SCHEMA, Schema, SchemaError, Signature
from .store import (
    MalformedRelation, R, Record, Rel, TopoStore, UnindexedAttribute, walk,
)
from .transfer import (
    Blocked, Inference, Knobs, Mapping, Round, TargetPattern, align,
    align_k_best, mac_score, project, run_transfer,
)

__version__ = "5.0.0"

__all__ = [
    "TopologicalEngine", "Telemetry",
    "TopoStore", "Record", "Rel", "R", "walk",
    "UnindexedAttribute", "MalformedRelation",
    "Schema", "Signature", "SchemaError", "CORE_SCHEMA", "ENT", "REL", "ANY",
    "SlotSpec", "Constraint", "has", "where", "lexical", "ask",
    "Budget", "RelaxationPolicy", "Stats", "Tier", "Verdict",
    "Closure", "Outcome", "DataRequest", "Oracle", "run_close",
    "TargetPattern", "Knobs", "Round", "Inference", "Mapping", "Blocked",
    "align", "align_k_best", "project", "run_transfer", "mac_score",
    "Prediction", "PremortemRound", "run_premortem", "report",
    "validate", "validate_all", "Validation", "Finding", "gloss",
    "summary", "CORE_VOCABULARY",
    "HeldOut", "QueryResult", "AblationResult", "ClosureScorecard",
    "split_record", "evaluate_library", "run_ablations", "format_ablations",
    "score_closures",
    "bit_count", "bit_indices",
    "__version__",
]
