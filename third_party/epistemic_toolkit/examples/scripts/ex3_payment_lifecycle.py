"""Component 5 applied to a payment lifecycle — three versions.

v1  as typically shipped: FAILED/REFUNDED/PENDING_RETRY unreachable, events unused.
v2  toolkit B's "fixed" version. It passes B's graph checks, but its retry loop
    PENDING_RETRY -> CREATED -> (card declined) -> PENDING_RETRY is unbounded:
    the limit lives inside a guard function the prover cannot see. The merged
    prover reports the cycle witness.
v3  the retry budget is a declared resource; every retry consumes it, so the
    expanded (state, budget) graph is acyclic and universal termination holds.
"""
import copy
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from epistemic_toolkit import Machine, MachineError

V1 = {
    "name": "Payment",
    "events": ["authorize", "capture", "settle", "refund", "retry", "cancel"],
    "states": [
        {"id": "CREATED", "kind": "initial"},
        {"id": "AUTHORIZED"}, {"id": "CAPTURED"}, {"id": "PENDING_RETRY"},
        {"id": "SETTLED", "kind": "terminal"}, {"id": "REFUNDED", "kind": "terminal"},
        {"id": "FAILED", "kind": "terminal"}, {"id": "CANCELLED", "kind": "terminal"},
    ],
    "transitions": [
        {"from": "CREATED", "event": "authorize", "to": "AUTHORIZED", "guard": "card_valid",
         "on_failure": "CANCELLED"},
        {"from": "AUTHORIZED", "event": "capture", "to": "CAPTURED", "guard": "funds_held",
         "on_failure": "CANCELLED"},
        {"from": "CAPTURED", "event": "settle", "to": "SETTLED", "guard": "batch_closed",
         "on_failure": "CANCELLED"},
        {"from": "AUTHORIZED", "event": "cancel", "to": "CANCELLED"},
        {"from": "PENDING_RETRY", "event": "authorize", "to": "AUTHORIZED", "guard": "card_valid",
         "on_failure": "CANCELLED"},
    ],
}

V2 = {
    "name": "Payment",
    "events": ["authorize", "capture", "settle", "refund", "retry", "cancel", "timeout", "give_up"],
    "default_failure": "PENDING_RETRY",
    "states": [
        {"id": "CREATED", "kind": "initial"},
        {"id": "AUTHORIZED"}, {"id": "CAPTURED"}, {"id": "SETTLED"}, {"id": "PENDING_RETRY"},
        {"id": "REFUNDED", "kind": "terminal"}, {"id": "CLOSED", "kind": "terminal"},
        {"id": "FAILED", "kind": "terminal"}, {"id": "CANCELLED", "kind": "terminal"},
    ],
    "transitions": [
        {"from": "CREATED", "event": "authorize", "to": "AUTHORIZED", "guard": "card_valid",
         "error_type": "CardDeclined"},
        {"from": "AUTHORIZED", "event": "capture", "to": "CAPTURED", "guard": "funds_held",
         "error_type": "CaptureFailed"},
        {"from": "CAPTURED", "event": "settle", "to": "SETTLED", "guard": "batch_closed",
         "error_type": "SettlementFailed"},
        {"from": "SETTLED", "event": "refund", "to": "REFUNDED", "guard": "within_refund_window",
         "on_failure": "CLOSED"},
        {"from": "SETTLED", "event": "timeout", "to": "CLOSED"},
        {"from": "PENDING_RETRY", "event": "retry", "to": "CREATED", "guard": "retries_remaining",
         "on_failure": "FAILED"},
        {"from": "PENDING_RETRY", "event": "give_up", "to": "FAILED"},
        {"from": "CREATED", "event": "cancel", "to": "CANCELLED"},
        {"from": "AUTHORIZED", "event": "cancel", "to": "CANCELLED"},
        {"from": "CAPTURED", "event": "timeout", "to": "PENDING_RETRY"},
    ],
}


def bounded(v2: dict, budget: int = 2) -> dict:
    v3 = copy.deepcopy(v2)
    v3["id"] = "payment_v3"
    v3["resources"] = [{"id": "retries", "min": 0, "initial": budget, "max": budget}]
    for t in v3["transitions"]:
        if t["from"] == "PENDING_RETRY" and t["event"] == "retry":
            t.pop("guard")                       # the limit is now in the model
            t.pop("on_failure")
            t["when"] = {"retries": {"gt": 0}}
            t["updates"] = {"retries": -1}
        if t["from"] == "PENDING_RETRY" and t["event"] == "give_up":
            t.pop("to"); t["to"] = "FAILED"      # always available: the explicit exit
    return v3


V3 = bounded(V2)

if __name__ == "__main__":
    print("=== v1 (as shipped) ===")
    print(Machine.from_dict(V1).prove().text())

    print("\n=== v2 (toolkit B's corrected version) ===")
    p2 = Machine.from_dict(V2).prove()
    print(p2.text())
    print("  existential termination (B's 'termination'):", p2.checks["existential_termination"])
    print("  universal termination:", p2.checks["universal_termination"])

    print("\n=== v3 (retry budget declared as a resource) ===")
    m = Machine.from_dict(V3)
    proof = m.prove()
    print(proof.text())

    out = Path(__file__).resolve().parents[2] / "dist" / "fsm-payment"
    r = m.generate(out, entity_table="payment")
    print(f"  generated {len(r['manifest']['files'])} encodings to {out.name}/ with source-sha256 "
          f"{r['manifest']['source_sha256'][:12]}…")

    texts = {n: (out / n).read_text() for n in r["manifest"]["files"]}
    print("  encodings agree:", not m.verify_encodings(texts, "payment"))
    edited = dict(texts)
    edited["state-machine.mmd"] += "    CREATED --> SETTLED: sneak\n"
    print("  ADDED edge detected:", m.verify_encodings(edited, "payment"))

    print("\n=== runtime ===")
    rt = m.runtime(guards={"card_valid": lambda c: c.get("card_ok", True), "funds_held": lambda c: True,
                           "batch_closed": lambda c: True, "within_refund_window": lambda c: c.get("days", 0) <= 30})
    for attempt in range(3):
        rt.fire("authorize", {"card_ok": False})
        print(f"  declined #{attempt + 1} -> {rt.state}  retries left {rt.resources['retries']}  "
              f"allowed {rt.allowed()}")
        if "retry" in rt.allowed():
            rt.fire("retry")
    rt.fire("give_up")
    print("  budget exhausted ->", rt.state)
    try:
        Machine.from_dict(V3).runtime({}).fire("authorize")
    except MachineError as e:
        print("  unregistered guard:", e)
