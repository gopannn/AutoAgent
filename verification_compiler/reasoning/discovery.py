"""DISCOVER: structural pre-mortem over the service topology with CTD's structural kernel.

Aligns the service's structure (topology.py) with a library of incident structures from
other domains and projects the failure modes those incidents would produce here. Every
projection carries a check and stays a HYPOTHESIS: it steers the spec, the architect and the
auditor, and the auditor must confirm or refute it, but it is never release evidence.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from ..errors import InfrastructureError
from ..hashing import hash_obj
from ..vendored import ctd_module
from .topology import Topology

LIBRARY_PATH = Path(__file__).resolve().parents[1] / "knowledge" / "failure_library.json"
MAX_RISKS = 8


def load_library(path: Path = LIBRARY_PATH) -> list:
    structural = ctd_module("structural")
    data = json.loads(path.read_text(encoding="utf-8"))
    return [structural.StructuralCase.model_validate(case) for case in data["cases"]]


def validate_library(cases) -> dict[str, list[str]]:
    """Strict kernel gate for every case: case id -> error findings (empty when clean)."""
    quality = ctd_module("encoding_quality")
    return {case.id: [f"{f.code}: {f.detail}" for f in quality.validate_structural_case_strict(case).findings
                      if f.level == "error"]
            for case in cases}


@lru_cache(maxsize=1)
def _engine():
    config = ctd_module("config")
    persistence = ctd_module("persistence")
    service = ctd_module("service")
    cases = load_library()
    errors = {cid: errs for cid, errs in validate_library(cases).items() if errs}
    if errors:
        raise InfrastructureError(f"failure library does not pass the strict structural gate: {errors}")
    engine = service.ProductionEngine(settings=config.EngineSettings(auth_mode="disabled"),
                                      store=persistence.SQLiteStore(":memory:"))
    for case in cases:
        engine.ingest_structural_case_v5(case)
    return engine


def library_fingerprint() -> str:
    return hash_obj(json.loads(LIBRARY_PATH.read_text(encoding="utf-8")))


def premortem(topology: Topology, name: str) -> list[dict]:
    """Ranked structural risks. Each item: id, relation, check, severity, source cases, lineage."""
    if not topology.relations:
        return []
    try:
        result = _engine().premortem_v5(topology.to_target(name))
    except InfrastructureError:
        raise
    except Exception as err:  # noqa: BLE001 - an engine failure must abort, not silently drop discovery
        raise InfrastructureError(f"structural premortem failed: {err!r}") from err
    risks = []
    for finding in result.findings[:MAX_RISKS]:
        relation = finding.relation.canonical_key()
        risks.append({
            "id": "RISK-" + hash_obj({"relation": relation, "cases": sorted(finding.source_cases)})[:10],
            "relation": relation,
            "state": str(finding.state),
            "check": finding.check,
            "severity": finding.severity,
            "source_cases": list(finding.source_cases),
            "source_domains": list(finding.source_domains),
            "priority": round(float(finding.priority_score), 3),
            "entity_map": dict(finding.lineage[0].get("entity_map", {})) if finding.lineage else {},
        })
    return risks
