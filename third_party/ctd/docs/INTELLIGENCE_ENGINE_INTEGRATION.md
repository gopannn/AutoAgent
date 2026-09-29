# Intelligence Engine Integration into CTD v4

CTD v4 incorporates the strongest ideas from the supplied Intelligence Engine research prototype without merging that prototype wholesale.

Integrated and upgraded concepts:

- encoding validation -> production `encoding_quality` gate;
- recursive relational cases -> typed `structural` domain;
- cheap MAC retrieval -> bounded candidate shortlist;
- greedy FAC alignment -> upgraded bounded beam/global-consistency aligner;
- leftover projection -> explicit `HYPOTHESIS` objects only;
- cross-domain convergence -> independence-aware hypothesis ranking;
- failure projection -> production pre-mortem findings with mandatory checks;
- `PASS/FAIL/UNKNOWN` -> three-valued CTD constraint truth;
- `DataRequest` -> first-class `ResolutionGap` acquisition plans;
- bitmap/cheap filtering principle -> optional provider prefilter capability;
- closure/abstention benchmark separation -> v4 deterministic intelligence benchmarks.

Not copied directly:

- the prototype's flat package layout;
- its custom store as the primary database;
- greedy one-mapping analogy behavior;
- simulated-oracle accuracy claims;
- any mechanism that could convert analogy directly into truth.

The CTD invariant is explicit: structural transfer generates investigation hypotheses, while only the evidence-backed Resolution Plane may return `RESOLVED`.
