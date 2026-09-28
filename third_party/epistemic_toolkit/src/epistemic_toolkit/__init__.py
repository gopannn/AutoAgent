"""epistemic_toolkit 2.0 — merged from SSIA-transferable-toolkit 1.0.0 (A) and
epistemic-toolkit 1.0.0 (B). Seven components, standard library only.

  1. ledger        typed, traceable, circularity-aware log-odds evidence ledger
  2. robustness    five-test gate (4 classic + independence group-out)
  3. outcomes/policy  computed / suppressed / unresolved; null where a number would lie
  4. derived       schema linter with proven vs observed evidence levels
  5. statemachine  single-source FSM, resource-bounded proofs, 5 encodings + runtime
  6. instrument    calibrate-before-measure with Wilson-bound gate + fingerprint lock
  7. regression    strict negative tests (in-memory Suite or JSON manifest)
"""
from .outcomes import COMPUTED, SUPPRESSED, UNRESOLVED, ScorePolicy, public_result, policy_definition
from .ledger import Evidence, Hypothesis, Ledger, LedgerError, Source, load_ledger
from .robustness import GateConfig, GateResult, gate, gate_all, minimum_flip_weight, run_robustness_gate
from .policy import auto_classify, classify, report, report_table
from .derived import lint, load_records, audit_records, confidence_vector_rank
from .statemachine import Machine, MachineError, State, Transition
from .instrument import Instrument, wilson, trials_needed, run_builtin_calibration, measure_real
from .regression import Suite, run_manifest

__version__ = "2.0.0"
__all__ = [
    "COMPUTED", "SUPPRESSED", "UNRESOLVED", "ScorePolicy", "public_result", "policy_definition",
    "Evidence", "Hypothesis", "Ledger", "LedgerError", "Source", "load_ledger",
    "GateConfig", "GateResult", "gate", "gate_all", "minimum_flip_weight", "run_robustness_gate",
    "auto_classify", "classify", "report", "report_table",
    "lint", "load_records", "audit_records", "confidence_vector_rank",
    "Machine", "MachineError", "State", "Transition",
    "Instrument", "wilson", "trials_needed", "run_builtin_calibration", "measure_real",
    "Suite", "run_manifest",
]
