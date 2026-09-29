"""Component 3 — Reporting policy.

`report()` is the ONLY sanctioned way to publish scores. It:
  * passes declared suppressed/unresolved through (reason mandatory),
  * demotes a declared `computed` with no bearing evidence to `suppressed`
    (NEW — toolkit B let such a hypothesis reach the gate and fail for the
    wrong reason; toolkit A rejected the ledger outright),
  * re-gates every declared `computed` and demotes failures to `unresolved`
    loudly, with the failing tests named,
  * publishes computed scores with their 5-95% perturbation band.

`auto_classify()` assigns policies from data; manual null policies are kept
(a human may know data is missing that the ledger cannot see).
"""
from __future__ import annotations

from dataclasses import dataclass

from .ledger import Ledger
from .outcomes import COMPUTED, POLICIES, SUPPRESSED, UNRESOLVED, public_result
from .robustness import GateConfig, gate


@dataclass
class ReportedScore:
    hypothesis_id: str
    statement: str
    declared_policy: str
    policy: str
    confidence: float | None
    reason: str | None
    what_would_settle_it: str | None
    robust: bool | None
    band: tuple[float, float] | None
    demoted: bool = False

    def __str__(self):
        if self.confidence is None:
            flag = " (demoted)" if self.demoted else ""
            return f"{self.hypothesis_id:34s} {self.policy.upper():11s}{flag} (null) — {self.reason}"
        band = f" [{self.band[0]:.2f}-{self.band[1]:.2f}]" if self.band else ""
        return f"{self.hypothesis_id:34s} {self.confidence:.3f}{band}"

    def to_dict(self):
        d = self.__dict__.copy()
        d["band"] = list(self.band) if self.band else None
        return d


def classify(ledger: Ledger, hid: str, cfg: GateConfig | None = None) -> tuple[str, str | None]:
    h = ledger[hid]
    if not h.bearing:
        return SUPPRESSED, (f"No admissible evidence with non-zero weight ({len(h.evidence)} recorded, "
                            f"{len(h.inadmissible)} circular). The score would equal the prior, "
                            f"which is an assumption, not a finding.")
    g = gate(ledger, hid, cfg)
    if not g.robust:
        return UNRESOLVED, (f"Fails the robustness gate: {'; '.join(g.failures)}. Any point estimate "
                            f"would report the weights rather than the evidence.")
    return COMPUTED, None


def auto_classify(ledger: Ledger, cfg: GateConfig | None = None, respect_manual: bool = True) -> dict[str, str]:
    out = {}
    for h in ledger:
        if respect_manual and h.score_policy in (SUPPRESSED, UNRESOLVED):
            if not h.policy_reason:
                raise ValueError(f"{h.id}: policy '{h.score_policy}' needs a reason")
            out[h.id] = h.score_policy
            continue
        h.score_policy, h.policy_reason = classify(ledger, h.id, cfg)
        out[h.id] = h.score_policy
    return out


def report(ledger: Ledger, cfg: GateConfig | None = None, include_band: bool = True) -> list[ReportedScore]:
    out = []
    for h in ledger:
        if h.score_policy not in POLICIES:
            raise ValueError(f"{h.id}: unknown policy {h.score_policy}")
        if h.score_policy in (SUPPRESSED, UNRESOLVED):
            public_result(h.score_policy, None, h.policy_reason)  # enforces reason
            out.append(ReportedScore(h.id, h.statement, h.score_policy, h.score_policy, None,
                                     h.policy_reason, h.what_would_settle_it, None, None))
            continue
        if not h.bearing:
            out.append(ReportedScore(h.id, h.statement, COMPUTED, SUPPRESSED, None,
                                     "Marked computed but no admissible evidence bears on it; "
                                     "the number would be the prior.",
                                     h.what_would_settle_it, None, None, demoted=True))
            continue
        g = gate(ledger, h.id, cfg)
        if not g.robust:
            out.append(ReportedScore(h.id, h.statement, COMPUTED, UNRESOLVED, None,
                                     "Marked computed but fails the robustness gate: " + "; ".join(g.failures),
                                     h.what_would_settle_it, False, None, demoted=True))
            continue
        out.append(ReportedScore(h.id, h.statement, COMPUTED, COMPUTED, round(g.base_score, 3), None,
                                 h.what_would_settle_it, True, g.band if include_band else None))
    return out


def report_table(scores: list[ReportedScore]) -> str:
    rows = ["| Hypothesis | Policy | Confidence | 5–95% band | Note |", "|---|---|---|---|---|"]
    for s in scores:
        conf = "null" if s.confidence is None else f"{s.confidence:.3f}"
        band = f"{s.band[0]:.2f}–{s.band[1]:.2f}" if s.band else "—"
        pol = s.policy + (" (demoted)" if s.demoted else "")
        note = (s.reason or s.what_would_settle_it or "").replace("|", "/")
        note = note if len(note) <= 110 else note[:107] + "..."
        rows.append(f"| {s.hypothesis_id} | {pol} | {conf} | {band} | {note} |")
    return "\n".join(rows)
