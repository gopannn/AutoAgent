"""Store scalability: v4 against v5, same workload, same machine, same run.

The claim under test is narrow and mechanical: v4's index build and result
materialisation are quadratic in the number of records, and v5's are linear.
Nothing here depends on a model, a retriever, or a corpus being any good, which
is why it is worth quoting.

Where the quadratic terms were:

    ingest        `table[k] |= 1 << i` allocates an i-bit integer per record
                  per indexed key -> O(n^2 / 64) word operations
    materialise   `bitmap >> i` inside a loop over all records re-allocates the
                  whole integer every iteration -> O(n^2 / 64)
    count         `bin(bitmap).count("1")` builds an n-character string

Run:

    python3 bench/bench_scale.py
"""

from __future__ import annotations

import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import v4_store                                     # noqa: E402
from topo import store as v5_store                  # noqa: E402

SIZES = (1_000, 4_000, 16_000, 32_000)
DOMAINS = [f"domain-{i}" for i in range(16)]
TAGS = [f"tag-{i}" for i in range(40)]


def make_rows(n: int, rng: random.Random):
    return [
        (f"rec-{i}",
         rng.choice(DOMAINS),
         {"severity": rng.randint(1, 5),
          "tags": rng.sample(TAGS, 3)})
        for i in range(n)
    ]


def bench(module, rows, label: str) -> dict[str, float]:
    st = module.TopoStore()
    st.declare_index("domain")
    st.declare_index("severity")
    st.declare_index("tags")

    t0 = time.perf_counter()
    for rid, domain, attrs in rows:
        st.ingest(module.Record(id=rid, domain=domain, attrs=dict(attrs)))
    ingest_ms = (time.perf_counter() - t0) * 1000

    # A selective query: one domain intersected with one severity.
    t0 = time.perf_counter()
    for _ in range(20):
        bm = st.bitmap("domain", DOMAINS[0]) & st.bitmap("severity", 3)
        module.TopoStore.count(bm)
    query_ms = (time.perf_counter() - t0) * 1000

    bm = st.bitmap("domain", DOMAINS[0])
    t0 = time.perf_counter()
    for _ in range(5):
        st.materialise(bm)
    mat_ms = (time.perf_counter() - t0) * 1000

    return {"label": label, "ingest": ingest_ms,
            "query": query_ms, "materialise": mat_ms,
            "selected": module.TopoStore.count(bm)}


def main() -> None:
    rng = random.Random(20260912)
    print(f"python {sys.version.split()[0]}  "
          f"3 indexed attributes, one multi-valued\n")
    head = (f"{'n':>7}  {'ingest v4':>11}{'ingest v5':>11}{'x':>7}   "
            f"{'20 queries v4':>14}{'v5':>9}{'x':>7}   "
            f"{'5 materialise v4':>17}{'v5':>9}{'x':>7}")
    print(head)
    print("-" * len(head))

    for n in SIZES:
        rows = make_rows(n, rng)
        a = bench(v4_store, rows, "v4")
        b = bench(v5_store, rows, "v5")
        print(f"{n:>7}  "
              f"{a['ingest']:>10.1f}m{b['ingest']:>10.1f}m"
              f"{a['ingest'] / max(b['ingest'], 1e-9):>6.1f}x   "
              f"{a['query']:>13.1f}m{b['query']:>8.1f}m"
              f"{a['query'] / max(b['query'], 1e-9):>6.1f}x   "
              f"{a['materialise']:>16.1f}m{b['materialise']:>8.1f}m"
              f"{a['materialise'] / max(b['materialise'], 1e-9):>6.1f}x")
        assert a["selected"] == b["selected"], "stores disagree on the result set"

    print("\nall timings in milliseconds; 'x' is v4 time / v5 time")
    print("both stores returned identical result-set cardinalities at every n,")
    print("so this measures the access pattern and nothing else.")


if __name__ == "__main__":
    main()
