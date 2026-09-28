"""Command-line entry points: python -m epistemic_toolkit <command> ..."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .derived import lint, load_records
from .instrument import measure_real, run_builtin_calibration
from .ledger import Ledger
from .policy import report, report_table
from .regression import run_manifest
from .robustness import GateConfig, run_robustness_gate
from .statemachine import Machine


def _load(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(payload: Any, path: str | None) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n"
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="epistemic-toolkit")
    c = p.add_subparsers(dest="command", required=True)
    a = c.add_parser("ledger", help="validate, audit and break down an evidence ledger")
    a.add_argument("input"); a.add_argument("--output"); a.add_argument("--strict-locator", action="store_true")
    a = c.add_parser("robustness", help="run the robustness gate")
    a.add_argument("input"); a.add_argument("--config"); a.add_argument("--output")
    a = c.add_parser("report", help="publish scores (markdown table or JSON)")
    a.add_argument("input"); a.add_argument("--config"); a.add_argument("--json", action="store_true")
    a = c.add_parser("derived", help="lint stored fields for derivability")
    a.add_argument("input"); a.add_argument("--table"); a.add_argument("--key"); a.add_argument("--config")
    a.add_argument("--output"); a.add_argument("--fail-on", choices=["error", "warning"], default=None)
    a.add_argument("--no-coerce", action="store_true")
    a = c.add_parser("fsm", help="prove an FSM and generate all encodings")
    a.add_argument("input"); a.add_argument("output_dir"); a.add_argument("--entity-table", default="entity")
    a = c.add_parser("verify-encodings", help="detect hand-edits to generated FSM encodings")
    a.add_argument("output_dir")
    a = c.add_parser("calibrate", help="calibrate the built-in mean_shift instrument")
    a.add_argument("input"); a.add_argument("--output")
    a = c.add_parser("measure", help="measure real data only after a matching calibration")
    a.add_argument("instrument"); a.add_argument("calibration"); a.add_argument("sample"); a.add_argument("--output")
    a = c.add_parser("negative", help="prove that checks fail on broken artifacts")
    a.add_argument("manifest"); a.add_argument("--output"); a.add_argument("--lenient", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "ledger":
        led = Ledger.from_json(args.input)
        issues = led.validate(require_locator=args.strict_locator)
        _write({"ledger_id": led.ledger_id, "valid": not issues, "issues": issues, "audit": led.audit(),
                "breakdowns": [led.breakdown(h.id).to_dict() for h in led]}, args.output)
        return 1 if issues else 0
    if args.command == "robustness":
        _write(run_robustness_gate(args.input, _load(args.config) if args.config else None), args.output)
        return 0
    if args.command == "report":
        led = Ledger.from_json(args.input)
        cfg = GateConfig.from_dict(_load(args.config)) if args.config else None
        rows = report(led, cfg)
        print(json.dumps([r.to_dict() for r in rows], indent=2) if args.json else report_table(rows))
        return 0
    if args.command == "derived":
        rows = load_records(args.input, table=args.table, coerce_numeric=not args.no_coerce)
        rep = lint(rows, key=args.key, config=_load(args.config) if args.config else None)
        _write(rep.to_dict(), args.output)
        if args.output:
            print(rep.text())
        return 1 if args.fail_on and not rep.passes(args.fail_on) else 0
    if args.command == "fsm":
        r = Machine.from_json(args.input).generate(args.output_dir, args.entity_table)
        print(r["proof"].text())
        return 0
    if args.command == "verify-encodings":
        d = Path(args.output_dir)
        man = _load(str(d / "manifest.json"))
        m = Machine.from_json(d / "source.json")
        texts = {n: (d / n).read_text(encoding="utf-8") for n in man["files"]}
        problems = m.verify_encodings(texts, man.get("entity_table", "entity"))
        if m.fingerprint() != man["source_sha256"]:
            problems.append("source.json no longer matches the manifest fingerprint")
        print(json.dumps({"agree": not problems, "problems": problems}, indent=2))
        return 1 if problems else 0
    if args.command == "calibrate":
        rep = run_builtin_calibration(_load(args.input))
        _write(rep, args.output)
        return 0 if rep["validated"] else 1
    if args.command == "measure":
        _write(measure_real(_load(args.sample), _load(args.instrument), _load(args.calibration)), args.output)
        return 0
    if args.command == "negative":
        mp = Path(args.manifest).resolve()
        rep = run_manifest(_load(str(mp)), mp.parent, strict=not args.lenient)
        _write(rep, args.output)
        return 0 if rep["summary"]["passes"] else 1
    return 2
