"""Components 4 and 7 applied to an e-commerce orders table.

Component 4 finds stored columns that carry no information. Component 7 then
proves the resulting invariants are real tests by running each one against a
mutated copy constructed to violate it.
"""
import random
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from epistemic_toolkit import lint, confidence_vector_rank, Suite

TIER = {"bronze": 0.00, "silver": 0.05, "gold": 0.10}
REGION = {"EU": ("EUR", 0.20), "UK": ("GBP", 0.20), "US": ("USD", 0.00)}


def make_orders(n=400, seed=4):
    rng = random.Random(seed)
    rows = []
    for i in range(1, n + 1):
        tier = rng.choice(list(TIER))
        region = rng.choice(list(REGION))
        cur, vat = REGION[region]
        subtotal = round(rng.uniform(10, 400), 2)
        rows.append({
            "order_id": i,
            "customer_tier": tier,
            "tier_discount_rate": TIER[tier],           # f(tier)
            "region": region,
            "currency": cur,                            # f(region)
            "vat_rate": vat,                            # f(region)
            "subtotal": subtotal,
            "subtotal_cents": round(subtotal * 100),    # affine f(subtotal)
            "line_items": rng.randint(1, 9),
            "schema_version": "v2",                     # constant
            "source_system": "web",                     # constant
            "fraud_score": rng.random(),
            "risk_score": None,                         # filled below
            "warehouse": rng.choice(["DUB", "LHR", "IAD"]),
        })
        rows[-1]["risk_score"] = rows[-1]["fraud_score"]  # duplicate
    # simulate drift: a handful of rows where currency was edited by hand
    for r in rng.sample(rows, 6):
        r["currency"] = "USD" if r["currency"] != "USD" else "EUR"
    return rows


def normalise(rows):
    """The fix: drop every derivable column; keep reference tables separately."""
    keep = ["order_id", "customer_tier", "region", "subtotal",
            "line_items", "fraud_score", "warehouse"]
    return [{k: r[k] for k in keep} for r in rows]


if __name__ == "__main__":
    orders = make_orders()

    print("=== 4. Derived-field lint on the orders table ===")
    print("-- observed only (no declared rules) --")
    rep = lint(orders, key="order_id")
    print(rep.text())

    print("\n-- with declared rules from the reference tables --")
    rules = {"declared_rules": [
        {"id": "R_TIER_DISCOUNT", "determinants": ["customer_tier"], "dependent": "tier_discount_rate",
         "lookup": {k: v for k, v in TIER.items()}},
        {"id": "R_REGION_VAT", "determinants": ["region"], "dependent": "vat_rate",
         "lookup": {k: v[1] for k, v in REGION.items()}},
        {"id": "R_REGION_CURRENCY", "determinants": ["region"], "dependent": "currency",
         "lookup": {k: v[0] for k, v in REGION.items()}},
        {"id": "R_CENTS", "type": "affine", "determinants": ["subtotal"], "dependent": "subtotal_cents",
         "slope": 100, "intercept": 0, "tolerance": 1e-6},
    ]}
    rep2 = lint(orders, key="order_id", config=rules)
    print(rep2.text())
    drift = next(f for f in rep2.findings if f.kind == "rule_violated")
    print(f"  drifted rows to inspect: {drift.violating_keys}")

    print("\n=== 4b. Multi-dimensional score audit ===")
    # a 'quality score' with five dimensions, as vendors like to ship them
    rng = random.Random(9)
    scored = []
    for _ in range(200):
        x = round(rng.uniform(0.4, 0.99), 3)
        scored.append({"accuracy": x, "completeness": 0.97, "consistency": 0.97,
                       "timeliness": 0.99, "validity": x})
    for r in scored:
        r["overall"] = round((r["accuracy"] + r["validity"]) / 2 * 0.9 + 0.05, 6)  # a linear blend
    print(confidence_vector_rank(scored, ["accuracy", "completeness", "consistency",
                                          "timeliness", "validity", "overall"]))

    # --------------------------------------------------------------------
    print("\n=== 7. Negative tests: prove each invariant catches its defect ===")
    good = normalise(make_orders())
    DERIVED = {"tier_discount_rate", "currency", "vat_rate", "subtotal_cents",
               "schema_version", "source_system", "risk_score"}

    s = Suite()
    s.check("no_derivable_columns",
            lambda rows: not (set(rows[0]) & DERIVED),
            catches=["add_currency", "add_cents"],
            description="INV: nothing stored that is a function of another column")
    # v1 of this check read only `.removable`. After the merge, observed FDs
    # are candidates, not proven — and the negative harness flagged the check
    # VACUOUS against add_currency. The check now reads both lists.
    s.check("linter_finds_nothing_derivable",
            lambda rows: not (lambda r: r.removable or r.candidates)(lint(rows, key="order_id")),
            catches=["add_currency", "add_cents", "add_constant"])
    s.check("order_ids_unique",
            lambda rows: len({r["order_id"] for r in rows}) == len(rows),
            catches=["duplicate_id"])
    s.check("subtotal_positive",
            lambda rows: all(r["subtotal"] > 0 for r in rows),
            catches=["negative_subtotal"])
    # a deliberately weak check: it passes on the defect it claims to catch
    s.check("row_count_nonzero",
            lambda rows: len(rows) > 0,
            catches=["duplicate_id"],
            description="looks like a test, catches nothing")

    s.good("normalised_orders", good)
    s.mutation_suite(good, {
        "add_currency": lambda rows: [r.update(currency=REGION[r["region"]][0]) for r in rows],
        "add_cents": lambda rows: [r.update(subtotal_cents=round(r["subtotal"] * 100))
                                   for r in rows],
        "add_constant": lambda rows: [r.update(schema_version="v2") for r in rows],
        "duplicate_id": lambda rows: rows.append(dict(rows[0])),
        "negative_subtotal": lambda rows: rows[3].update(subtotal=-5.0),
    })
    res = s.run()
    print(res.text())
    print("\n  coverage (which checks catch each defect):")
    for defect, catchers in s.coverage().items():
        print(f"    {defect:18s} <- {catchers}")
