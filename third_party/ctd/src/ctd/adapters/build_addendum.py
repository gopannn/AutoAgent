"""Build the 41_PREMORTEM addendum workbook.

Produced as a SEPARATE file, not by editing the master, and that is a
deliberate decision rather than a convenience.

The v10 master carries 42 `extLst` blocks in its worksheets — the modern
conditional-formatting extensions (data bars, colour scales) that openpyxl
states plainly it does not support and removes on load. Re-saving the master
through openpyxl would return a workbook that opens cleanly, recalculates
cleanly, passes every check, and has quietly lost 42 blocks of conditional
formatting that nobody would notice until a dashboard stopped colouring itself.
That is precisely the class of silent damage this whole engine exists to refuse,
and it would be incoherent to inflict it while shipping a tool that refuses it.

The addendum carries no formulas that reach into the master, so it cannot be
broken by the master moving a row. Insertion instructions are on the HOW TO
sheet; the sheet is designed to be copied in whole once someone has the master
open in Excel, which preserves the extensions because Excel wrote them.

Conventions copied from the master: Arial, navy #223967 headers in white bold,
body #222222 at 9pt, muted #6B7280 for notes, and the master's own column
widths.

    python3 interiors/build_addendum.py
"""

from __future__ import annotations

import os
from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .resolution import resolve_feasibility              # noqa: E402
from .workbook import (                                  # noqa: E402
    build_target, premortem_for, read_projects, to_exception_rows,
)
from .cross_domain import CROSS_DOMAIN_INCIDENTS as INCIDENTS

DEFAULT_MASTER = os.environ.get("CTD_ERP_MASTER", "")
DEFAULT_OUT = os.environ.get("CTD_ERP_OUT", "ERP_INTELLIGENCE_ADDENDUM.xlsx")

NAVY = "223967"
MUTED = "6B7280"
BODY = "222222"
AMBER = "FFF4D6"
RED = "FDE2E1"
GREEN = "E3F3E6"

H1 = Font(name="Arial", size=15, bold=True, color="FFFFFF")
HDR = Font(name="Arial", size=9, bold=True, color="FFFFFF")
TXT = Font(name="Arial", size=9, color=BODY)
NOTE = Font(name="Arial", size=9, color=MUTED)
BOLD = Font(name="Arial", size=9, bold=True, color=BODY)
FILL_NAVY = PatternFill("solid", fgColor=NAVY)
THIN = Border(bottom=Side(style="thin", color="D9D9D9"))
WRAP = Alignment(vertical="top", wrap_text=True)
TOP = Alignment(vertical="top")


def band(ws, row, text, width):
    ws.cell(row=row, column=1, value=text).font = H1
    for c in range(1, width + 1):
        ws.cell(row=row, column=c).fill = FILL_NAVY
    ws.row_dimensions[row].height = 22


def header(ws, row, labels):
    for i, lab in enumerate(labels, start=1):
        c = ws.cell(row=row, column=i, value=lab)
        c.font = HDR
        c.fill = FILL_NAVY
        c.alignment = WRAP
    ws.row_dimensions[row].height = 26


def widths(ws, spec):
    for i, w in enumerate(spec, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def main(master: str = "", out: str = "") -> str:
    master = master or DEFAULT_MASTER
    out = out or DEFAULT_OUT
    if not master or not os.path.exists(master):
        raise FileNotFoundError(
            f"workbook not found: {master!r}. Pass a path or set "
            f"CTD_ERP_MASTER.")
    projects = [p for p in read_projects(master) if p.live]
    results = [(p, *premortem_for(p, extra_library=INCIDENTS, limit=8))
               for p in projects]

    wb = Workbook()

    # ---- 41_PREMORTEM -------------------------------------------------
    ws = wb.active
    ws.title = "41_PREMORTEM"
    widths(ws, [13, 26, 46, 9, 6, 5, 22, 58, 12])
    band(ws, 1, "41 · PRE-MORTEM — PREDICTED EXCEPTIONS, EACH WITH A CHECK", 9)
    ws["A3"] = ("Structural transfer from the firm's own exception taxonomy "
                "and from incidents in unrelated domains. Every row is "
                "MODEL_PREDICTED — advisory only under 37_GOVERNANCE, and it "
                "may not drive a commitment.")
    ws["A3"].font = NOTE
    ws["A3"].alignment = WRAP
    ws.merge_cells("A3:I3")
    ws.row_dimensions[3].height = 28
    ws["A4"] = f"GENERATED  {date.today():%d-%b-%Y}"
    ws["A4"].font = BOLD

    r = 6
    ws.cell(row=r, column=1, value="A.  PREDICTED EXCEPTIONS").font = BOLD
    r += 1
    header(ws, r, ["Project", "Exception type", "Predicted relation",
                   "Priority", "Conv", "Sev", "Precedent domains",
                   "CHECK — what to measure", "Status"])
    first_data = r + 1
    r += 1

    for p, premise, preds in results:
        for e in to_exception_rows(p.id, preds):
            vals = [e.project, e.exception_type, e.relation, e.priority,
                    e.convergence, e.severity, e.precedent, e.check,
                    "CONDITIONAL" if e.conditional else "OPEN"]
            for i, v in enumerate(vals, start=1):
                c = ws.cell(row=r, column=i, value=v)
                c.font = TXT
                c.alignment = WRAP if i in (3, 7, 8) else TOP
                c.border = THIN
            if e.severity >= 5:
                ws.cell(row=r, column=4).fill = PatternFill("solid",
                                                            fgColor=RED)
            elif e.severity >= 3:
                ws.cell(row=r, column=4).fill = PatternFill("solid",
                                                            fgColor=AMBER)
            ws.cell(row=r, column=4).number_format = "0.0"
            r += 1
    last_data = r - 1

    r += 1
    ws.cell(row=r, column=1, value="B.  ROLL-UP").font = BOLD
    r += 1
    header(ws, r, ["Measure", "Value", "Reads"])
    r += 1
    rollup = [
        ("Predictions generated",
         f"=COUNTA($A${first_data}:$A${last_data})",
         "Rows in block A"),
        ("High severity (5)",
         f"=COUNTIF($F${first_data}:$F${last_data},5)",
         "Severity observed when this failed elsewhere"),
        ("Corroborated by 2+ independent precedents",
         f"=COUNTIF($E${first_data}:$E${last_data},\">=2\")",
         "Convergence — the strongest signal here"),
        ("Conditional on an unverified prediction",
         f"=COUNTIF($I${first_data}:$I${last_data},\"CONDITIONAL\")",
         "Check the premise first; these collapse if it fails"),
        ("Without a check procedure",
         f'=COUNTIF($H${first_data}:$H${last_data},'
         f'"no check available*")',
         "Leads, not findings"),
    ]
    for label, formula, reads in rollup:
        ws.cell(row=r, column=1, value=label).font = TXT
        ws.cell(row=r, column=2, value=formula).font = BOLD
        ws.cell(row=r, column=3, value=reads).font = NOTE
        ws.cell(row=r, column=3).alignment = WRAP
        r += 1

    r += 1
    ws.cell(row=r, column=1,
            value=("Convergence counts INDEPENDENT precedents — distinct "
                   "domains, distinct structure, distinct independence group. "
                   "Two retellings of one incident cannot manufacture "
                   "agreement.")).font = NOTE
    ws.cell(row=r, column=1).alignment = WRAP
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=9)

    # ---- 41_EVIDENCE ---------------------------------------------------
    we = wb.create_sheet("41_EVIDENCE")
    widths(we, [13, 50, 16, 24, 46])
    band(we, 1, "41B · EVIDENCE TRACE — WHAT THE PREMISE ASSERTED, AND WHY", 5)
    we["A3"] = ("Nothing was assumed. Every relation below traces to a column "
                "of 04_PROJECTS and carries its evidence class from "
                "37_GOVERNANCE. What was NOT captured is listed too, because "
                "a premise that quietly invents its own evidence is worse "
                "than a thin one.")
    we["A3"].font = NOTE
    we["A3"].alignment = WRAP
    we.merge_cells("A3:E3")
    we.row_dimensions[3].height = 30

    r = 5
    header(we, r, ["Project", "Relation asserted", "Source column",
                   "Evidence class", "Not captured, so not asserted"])
    r += 1
    for p, premise, _ in results:
        gaps = list(premise.gaps)
        for i, (rel, col, cls) in enumerate(premise.evidence):
            row = [p.id if i == 0 else "", str(rel), f"04_PROJECTS {col}", cls,
                   gaps[i] if i < len(gaps) else ""]
            for j, v in enumerate(row, start=1):
                c = we.cell(row=r, column=j, value=v)
                c.font = TXT if j != 4 else NOTE
                c.alignment = WRAP
                c.border = THIN
            r += 1
        for g in gaps[len(premise.evidence):]:
            we.cell(row=r, column=5, value=g).font = TXT
            we.cell(row=r, column=5).alignment = WRAP
            r += 1
        r += 1

    # ---- 42_RESOLUTION -------------------------------------------------
    # The evidence plane. CTD resolves; it is the only sheet here whose rows
    # can say a thing IS so.
    gates = resolve_feasibility(projects)
    wr = wb.create_sheet("42_RESOLUTION")
    widths(wr, [13, 24, 11, 9, 52, 56])
    band(wr, 1, "42 · RESOLUTION — FEASIBILITY DECIDED ON EVIDENCE", 6)
    wr["A3"] = ("38_DECISION_ENGINE block A, re-decided by the CTD evidence "
                "resolver. The difference is the third truth value: a "
                "condition can be UNKNOWN rather than forced to PASS or "
                "REVIEW, and every UNKNOWN carries what would settle it. "
                "VIOLATED is a fact and blocks; UNKNOWN is ignorance and "
                "reviews.")
    wr["A3"].font = NOTE
    wr["A3"].alignment = WRAP
    wr.merge_cells("A3:F3")
    wr.row_dimensions[3].height = 32

    r = 5
    wr.cell(row=r, column=1, value="A.  VERDICT BY PROJECT").font = BOLD
    r += 1
    header(wr, r, ["Project", "Verdict", "Coverage", "Conf.",
                   "Satisfied on admissible evidence", "Summary"])
    r += 1
    for g in gates:
        vals = [g.project, g.verdict, g.coverage, g.confidence,
                ", ".join(g.resolved) or "none", g.headline]
        for i, v in enumerate(vals, start=1):
            c = wr.cell(row=r, column=i, value=v)
            c.font = TXT
            c.alignment = WRAP if i in (5, 6) else TOP
            c.border = THIN
        wr.cell(row=r, column=3).number_format = "0%"
        wr.cell(row=r, column=4).number_format = "0.00"
        wr.cell(row=r, column=2).fill = PatternFill(
            "solid", fgColor=RED if g.verdict == "BLOCKED"
            else AMBER if g.verdict == "REVIEW" else GREEN)
        r += 1

    r += 1
    wr.cell(row=r, column=1,
            value="B.  CONDITION DETAIL — VIOLATED IS A FACT, "
                  "UNKNOWN IS A DATA REQUEST").font = BOLD
    r += 1
    header(wr, r, ["Project", "Condition", "Truth", "Pri",
                   "Why", "What would settle it"])
    gap_first = r + 1
    r += 1
    for g in gates:
        for i, gap in enumerate(g.gaps):
            vals = [g.project if i == 0 else "", gap["label"], gap["truth"],
                    gap["priority"], gap["reason"], gap["action"]]
            for j, v in enumerate(vals, start=1):
                c = wr.cell(row=r, column=j, value=v)
                c.font = TXT
                c.alignment = WRAP if j in (5, 6) else TOP
                c.border = THIN
            wr.cell(row=r, column=3).fill = PatternFill(
                "solid", fgColor=RED if gap["truth"] == "VIOLATED" else AMBER)
            r += 1
    gap_last = r - 1

    r += 1
    header(wr, r, ["Measure", "Value", "Reads"])
    r += 1
    for label, formula, reads in [
        ("Conditions VIOLATED — facts, block release",
         f'=COUNTIF($C${gap_first}:$C${gap_last},"VIOLATED")',
         "Something is true and stops the work"),
        ("Conditions UNKNOWN — data requests, not refusals",
         f'=COUNTIF($C${gap_first}:$C${gap_last},"UNKNOWN")',
         "38_DECISION_ENGINE can only render these as the constant REVIEW"),
        ("Projects blocked",
         f'=COUNTIF($B$7:$B${6 + len(gates)},"BLOCKED")',
         "Any condition violated"),
    ]:
        wr.cell(row=r, column=1, value=label).font = TXT
        wr.cell(row=r, column=2, value=formula).font = BOLD
        wr.cell(row=r, column=3, value=reads).font = NOTE
        wr.cell(row=r, column=3).alignment = WRAP
        r += 1

    r += 2
    wr.cell(row=r, column=1, value="C.  EVIDENCE CLASS → RESOLVER TRUST "
                                   "(37_GOVERNANCE BLOCK A, AS NUMBERS)"
            ).font = BOLD
    r += 1
    header(wr, r, ["Evidence class", "Trust", "May satisfy a gate?",
                   "37_GOVERNANCE wording"])
    r += 1
    from .resolution import COMMITMENT_CLASSES, TRUST
    from .schema_interiors import EVIDENCE_PLANE
    for cls, t in sorted(TRUST.items(), key=lambda kv: -kv[1]):
        allowed = cls in COMMITMENT_CLASSES
        vals = [cls, t, "yes" if allowed else "NO — hypothesis plane",
                EVIDENCE_PLANE.get(cls, ("", ""))[0]]
        for j, v in enumerate(vals, start=1):
            c = wr.cell(row=r, column=j, value=v)
            c.font = TXT
            c.alignment = WRAP if j == 4 else TOP
            c.border = THIN
        wr.cell(row=r, column=2).number_format = "0.00"
        if not allowed:
            wr.cell(row=r, column=3).fill = PatternFill("solid", fgColor=AMBER)
        r += 1
    r += 1
    wr.cell(row=r, column=1,
            value=("RESEARCH_DERIVED and MODEL_PREDICTED are excluded by "
                   "construction. No volume of either can move a gate to "
                   "PASS — which is what stops a pre-mortem prediction on "
                   "41_PREMORTEM, however many precedents converge on it, "
                   "from releasing work.")).font = NOTE
    wr.cell(row=r, column=1).alignment = WRAP
    wr.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)

    # ---- 41_HOWTO ------------------------------------------------------
    wh = wb.create_sheet("41_HOWTO")
    widths(wh, [30, 92])
    band(wh, 1, "41C · HOW TO USE, AND WHAT THIS IS NOT", 2)
    rows = [
        ("", ""),
        ("WHY A SEPARATE FILE", ""),
        ("Reason",
         "The v10 master carries 42 conditional-formatting extension blocks "
         "(data bars, colour scales). The library used to write this file "
         "removes them on load. Re-saving the master would have returned a "
         "workbook that opens and recalculates cleanly and has silently lost "
         "them. Copy this sheet in from Excel instead, which preserves them."),
        ("To insert",
         "Open both files in Excel · right-click the 41_PREMORTEM tab · Move "
         "or Copy · To book: the v10 master · Create a copy · place after "
         "40_ADMIN_RUNBOOK. Repeat for 41_EVIDENCE. Then add two rows to "
         "00_START navigation."),
        ("", ""),
        ("THE THREE PLANES", ""),
        ("42_RESOLUTION (evidence)",
         "CTD's evidence resolver. The ONLY layer whose rows may assert that "
         "something is so. Three-valued: SATISFIED / VIOLATED / UNKNOWN, with "
         "a named data request behind every UNKNOWN."),
        ("41_PREMORTEM (hypothesis)",
         "Structural transfer. Projects mechanisms onto what has not happened "
         "yet. Every row is MODEL_PREDICTED and can never become a fact by "
         "scoring highly — only by someone running the CHECK and recording "
         "the result as real evidence."),
        ("The workbook (record)",
         "Holds the facts and, on 37_GOVERNANCE, already declares which class "
         "each belongs to. That declaration now parameterises the resolver "
         "instead of being a convention people remember."),
        ("Why the separation matters",
         "A system that lets a confident prediction satisfy a commercial gate "
         "will eventually release work on evidence nobody has. The boundary "
         "is enforced in code, not documented as policy."),
        ("", ""),
        ("WHY THIS EXISTS AT ALL", ""),
        ("The gap",
         "18_LEARNING gates every engine on LN_MinProj COMPLETED projects. "
         "The register holds three active projects and zero completed, so "
         "every learning engine is reporting its fallback and will keep doing "
         "so for months. Statistical learning cannot start without history."),
        ("What replaces it",
         "Structural transfer does not need the firm's history. It needs the "
         "failure mechanism encoded once, from anywhere. 32_EXCEPTION_QUEUE "
         "and 35_ENUMERATIONS already enumerate the firm's mechanisms; this "
         "turns that list from a taxonomy into a projection."),
        ("When it stops mattering",
         "As completed projects accumulate, FIRM_ACTUAL outranks transferred "
         "cases under 37_GOVERNANCE and 18_LEARNING takes over. This layer "
         "should shrink in influence over time. If it does not, the history "
         "is not being captured."),
        ("", ""),
        ("HOW TO READ A ROW", ""),
        ("Priority",
         "Structural soundness x severity observed elsewhere x a discount for "
         "resting on unverified premises. It is a sort order, not a "
         "probability. Nothing here estimates likelihood of occurrence."),
        ("Convergence",
         "Independent precedents projecting the same prediction. 2 or more is "
         "evidence; 1 is a lead. Counted so that duplicate cases cannot "
         "inflate it."),
        ("CONDITIONAL",
         "The prediction rests on an earlier, unverified prediction. Check "
         "that premise FIRST — if it fails, everything below it collapses and "
         "there is no point measuring it."),
        ("CHECK",
         "What to measure to confirm or refute, named against a column or log "
         "row that already exists. A prediction without a check is a lead, "
         "not a finding, and is labelled as such."),
        ("", ""),
        ("WHAT THIS IS NOT", ""),
        ("Not a probability",
         "No row says a failure WILL happen. Each says: this mechanism has "
         "produced this failure elsewhere, this design has the same shape, go "
         "and measure X."),
        ("Not a commitment",
         "Every row is MODEL_PREDICTED. Under 37_GOVERNANCE block A that is "
         "advisory only. It may not price a quote, move a date, or release a "
         "stage."),
        ("Not validated on firm data",
         "The mechanism was measured on an eleven-incident benchmark, not on "
         "this firm's outcomes. Log what happened in 38_DECISION_ENGINE "
         "block F; after ten runs the acceptance rate there is the first real "
         "read on whether this is worth keeping."),
        ("Not a replacement",
         "It does not touch feasibility gates, style scoring, estimate lanes "
         "or the exception queue. It adds one thing they do not do: project "
         "forward from a mechanism rather than report a breach that already "
         "happened."),
    ]
    r = 3
    for a, b in rows:
        if a and not b:
            wh.cell(row=r, column=1, value=a).font = BOLD
        else:
            wh.cell(row=r, column=1, value=a).font = TXT
            c = wh.cell(row=r, column=2, value=b)
            c.font = TXT
            c.alignment = WRAP
            if b:
                wh.row_dimensions[r].height = max(14, 11 * (len(b) // 86 + 1))
        r += 1

    # Evidence plane first: what is true, then what might happen. A reader who
    # meets the predictions before the facts will read them as findings.
    wb.move_sheet("42_RESOLUTION", offset=-(
        wb.sheetnames.index("42_RESOLUTION")))

    for s in wb.worksheets:
        s.sheet_view.showGridLines = False
        s.freeze_panes = "A2"

    parent = os.path.dirname(os.path.abspath(out))
    os.makedirs(parent, exist_ok=True)
    wb.save(out)
    print(f"wrote {out}")
    print(f"{len(projects)} live project(s), "
          f"{sum(len(x[2]) for x in results)} predictions")
    return out


if __name__ == "__main__":
    import sys
    main(*(sys.argv[1:3]))
