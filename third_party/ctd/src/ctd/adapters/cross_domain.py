"""Cross-domain incident library.

Eleven encoded incidents from structurally distant domains, carried inside the
package so a deployment has a transfer corpus on day one rather than after its
own history accumulates. Distributed systems, databases, public safety, power
grids, control theory, caching, finance, transport, operations, ecology and
supply chains.

They are here to be *transferred from*, not to be believed about any particular
firm. A deployment should expect them to be outranked by its own FIRM_ACTUAL
history as that history arrives; if they are not, the history is not being
captured.
"""

from __future__ import annotations

from ..kernel import R, Record

RETRY_STORM = Record(
    id="retry-storm",
    domain="distributed-systems",
    attrs={
        "incident": True, "severity": 5, "year": 2023,
        "checks": {
            "RETRIES": "Confirm whether {0} retries against {1} without "
                       "jitter or a circuit breaker. Read the client config.",
            "AMPLIFIES": "Inject 2s of latency at {1} in staging. Measure "
                         "inbound request rate at {1}. If rate rises while "
                         "latency rises, amplification is present.",
            "SATURATES": "Load-test {0} past its nominal ceiling with "
                         "retries enabled; record time to queue collapse.",
        },
    },
    types={"caller": "AGENT", "backend": "RESOURCE",
           "load": "LOAD", "latency": "SIGNAL"},
    rels=(
        R("DEPENDS", "caller", "backend"),
        R("CAUSES", R("EXCESS", "load"), R("QUEUEING", "backend")),
        R("INCREASES", R("QUEUEING", "backend"), "latency"),
        R("SENSES", "caller", "latency"),
        R("CAUSES", R("SENSES", "caller", "latency"),
                    R("RETRIES", "caller", "backend")),
        R("CAUSES", R("RETRIES", "caller", "backend"),
                    R("AMPLIFIES", "load", "backend")),
        R("CAUSES", R("AMPLIFIES", "load", "backend"),
                    R("SATURATES", "backend")),
    ),
    text="Client retries on timeout amplified load until the backend "
         "collapsed. No jitter, no circuit breaker.",
)

POOL_EXHAUSTION = Record(
    id="pool-exhaustion",
    domain="database",
    attrs={
        "incident": True, "severity": 4, "year": 2022,
        "checks": {
            "EXHAUSTS": "Count concurrent holders of {1} under peak load. "
                        "If holders equals pool size for any sustained "
                        "window, {1} is the binding resource.",
            "SHARED": "Enumerate every consumer of {0}. Confirm whether any "
                      "one consumer can starve the others.",
            "RETRIES": "Check whether {0} reconnects on checkout timeout "
                       "rather than failing fast.",
        },
    },
    types={"workers": "AGENT", "pool": "POOL",
           "checkout-rate": "LOAD", "wait-time": "SIGNAL"},
    rels=(
        R("DEPENDS", "workers", "pool"),
        R("SHARED", "pool", "workers"),
        R("CAUSES", R("EXCESS", "checkout-rate"), R("QUEUEING", "pool")),
        R("INCREASES", R("QUEUEING", "pool"), "wait-time"),
        R("SENSES", "workers", "wait-time"),
        R("CAUSES", R("SENSES", "workers", "wait-time"),
                    R("RETRIES", "workers", "pool")),
        R("CAUSES", R("RETRIES", "workers", "pool"),
                    R("EXHAUSTS", "checkout-rate", "pool")),
    ),
    text="Slow queries held connections; workers retried checkout and "
         "exhausted the pool.",
)

CROWD_CRUSH = Record(
    id="crowd-bottleneck",
    domain="public-safety",
    attrs={
        "incident": True, "severity": 5, "year": 2010,
        "checks": {
            "DELAYED": "Measure the lag between {0} changing and consumers "
                       "observing it. If the lag exceeds the time to act, "
                       "the signal cannot be used for control.",
            "SATURATES": "Compute peak arrival rate against the throughput "
                         "ceiling of {0}. Headroom below 20% is a crush risk.",
        },
    },
    types={"crowd": "HUMAN", "exit": "CHANNEL",
           "arrival-rate": "LOAD", "wait": "SIGNAL"},
    rels=(
        R("FLOWS", "crowd", "exit"),
        R("DEPENDS", "crowd", "exit"),
        R("CAUSES", R("EXCESS", "arrival-rate"), R("QUEUEING", "exit")),
        R("INCREASES", R("QUEUEING", "exit"), "wait"),
        R("CAUSES", R("DELAYED", "wait"), R("SATURATES", "exit")),
    ),
    text="Arrivals outpaced exit throughput. Congestion information reached "
         "the crowd too late to change behaviour.",
)

GRID_CASCADE = Record(
    id="grid-cascade",
    domain="power-systems",
    attrs={
        "incident": True, "severity": 5, "year": 2003,
        "checks": {
            "FAILS": "Identify the failure mode of {0} at 100% utilisation. "
                     "Confirm whether it degrades or stops.",
            "CASCADES": "Model removal of {1}. Does {0} redistribute onto "
                        "peers already near their ceiling?",
        },
    },
    types={"feeders": "AGENT", "grid": "RESOURCE",
           "demand": "LOAD", "frequency-dev": "SIGNAL"},
    rels=(
        R("DEPENDS", "feeders", "grid"),
        R("CAUSES", R("EXCESS", "demand"), R("QUEUEING", "grid")),
        R("INCREASES", R("QUEUEING", "grid"), "frequency-dev"),
        R("CAUSES", R("QUEUEING", "grid"), R("FAILS", "grid")),
        R("CAUSES", R("FAILS", "grid"), R("CASCADES", "demand", "grid")),
    ),
    text="An overloaded line tripped; its load redistributed onto neighbours "
         "already near capacity, which tripped in turn.",
)

THERMOSTAT = Record(
    id="delayed-control-loop",
    domain="control-theory",
    attrs={
        "incident": True, "severity": 3, "year": 1998,
        "checks": {
            "OSCILLATES": "Hold input steady and record {0} over several "
                          "control periods. Sustained periodic variation "
                          "under steady input confirms oscillation.",
            "DELAYED": "Measure signal lag against the control period. Lag "
                       "above half the period guarantees overshoot.",
            "REDUCES": "Confirm {0} acts on {1} rather than on a downstream "
                       "symptom of it.",
        },
    },
    types={"controller": "AGENT", "plant": "RESOURCE",
           "heat-demand": "LOAD", "temp-gap": "SIGNAL"},
    rels=(
        R("DEPENDS", "controller", "plant"),
        R("SENSES", "controller", "temp-gap"),
        R("CAUSES", R("SENSES", "controller", "temp-gap"),
                    R("REDUCES", "controller", "heat-demand")),
        R("CAUSES", R("DELAYED", "temp-gap"),
                    R("OSCILLATES", "heat-demand")),
    ),
    text="Feedback lagged the control period; the loop overshot in both "
         "directions indefinitely.",
)

CACHE_STAMPEDE = Record(
    id="cache-stampede",
    domain="caching",
    attrs={
        "incident": True, "severity": 4, "year": 2021,
        "checks": {
            "DEGRADES": "Measure p99 service time of {0} at 90% and 100% of "
                        "nominal capacity. A discontinuity means {0} "
                        "degrades rather than queues gracefully.",
            "AMPLIFIES": "Expire a hot key and count origin requests in the "
                         "following second against the steady-state rate.",
            "SATURATES": "Load-test {0} with a synchronised expiry of the "
                         "top 1% of keys.",
        },
    },
    types={"clients": "SERVICE", "origin": "RESOURCE",
           "miss-rate": "RATE", "fetch-latency": "SIGNAL"},
    rels=(
        R("DEPENDS", "clients", "origin"),
        R("SHARED", "origin", "clients"),
        R("CAUSES", R("EXCESS", "miss-rate"), R("QUEUEING", "origin")),
        R("INCREASES", R("QUEUEING", "origin"), "fetch-latency"),
        R("CAUSES", R("QUEUEING", "origin"), R("DEGRADES", "origin")),
        R("CAUSES", R("DEGRADES", "origin"),
                    R("AMPLIFIES", "miss-rate", "origin")),
        R("CAUSES", R("AMPLIFIES", "miss-rate", "origin"),
                    R("SATURATES", "origin")),
    ),
    text="A synchronised TTL expiry sent every client to the origin at once; "
         "the origin slowed, which lengthened the miss window, which sent "
         "more clients to the origin.",
)

BANK_RUN = Record(
    id="bank-run",
    domain="finance",
    attrs={
        "incident": True, "severity": 5, "year": 2023,
        "checks": {
            "REDUCES": "Confirm whether {0} can withdraw commitment from {1} "
                       "faster than {1} can be replenished.",
            "EXHAUSTS": "Compare the maximum same-day outflow of {0} against "
                        "the liquid balance of {1}.",
            "FAILS": "Identify the point at which {0} cannot meet an "
                     "obligation. Confirm whether that point is public.",
        },
    },
    types={"depositors": "HUMAN", "reserves": "RESOURCE",
           "withdrawal-rate": "RATE", "deposits": "LOAD",
           "liquidity-gap": "SIGNAL"},
    rels=(
        R("DEPENDS", "depositors", "reserves"),
        R("SHARED", "reserves", "depositors"),
        R("CAUSES", R("EXCESS", "withdrawal-rate"), R("QUEUEING", "reserves")),
        R("INCREASES", R("QUEUEING", "reserves"), "liquidity-gap"),
        R("SENSES", "depositors", "liquidity-gap"),
        R("CAUSES", R("SENSES", "depositors", "liquidity-gap"),
                    R("REDUCES", "depositors", "deposits")),
        R("CAUSES", R("REDUCES", "depositors", "deposits"),
                    R("AMPLIFIES", "withdrawal-rate", "reserves")),
        R("CAUSES", R("AMPLIFIES", "withdrawal-rate", "reserves"),
                    R("EXHAUSTS", "withdrawal-rate", "reserves")),
        R("CAUSES", R("EXHAUSTS", "withdrawal-rate", "reserves"),
                    R("FAILS", "reserves")),
    ),
    text="A visible liquidity gap caused depositors to withdraw, which "
         "widened the gap, which caused more withdrawals.",
)

HIGHWAY = Record(
    id="highway-congestion",
    domain="transport",
    attrs={
        "incident": True, "severity": 3, "year": 2015,
        "checks": {
            "REDISTRIBUTES": "Instrument the alternate routes. Confirm "
                             "whether diverted {0} arrives faster than {1} "
                             "recovers.",
            "OSCILLATES": "Record flow rate at fixed points over a peak "
                          "period. Stop-go waves travelling upstream confirm "
                          "oscillation.",
            "SATURATES": "Compare peak demand against the throughput ceiling "
                         "of {0}, not its lane count.",
        },
    },
    types={"commuters": "HUMAN", "highway": "CHANNEL",
           "trip-demand": "LOAD", "travel-time": "SIGNAL"},
    rels=(
        R("DEPENDS", "commuters", "highway"),
        R("FLOWS", "commuters", "highway"),
        R("CAUSES", R("EXCESS", "trip-demand"), R("QUEUEING", "highway")),
        R("INCREASES", R("QUEUEING", "highway"), "travel-time"),
        R("SENSES", "commuters", "travel-time"),
        R("CAUSES", R("SENSES", "commuters", "travel-time"),
                    R("REDISTRIBUTES", "trip-demand", "highway")),
        R("CAUSES", R("DELAYED", "travel-time"),
                    R("OSCILLATES", "trip-demand")),
        R("CAUSES", R("OSCILLATES", "trip-demand"),
                    R("SATURATES", "highway")),
    ),
    text="Congestion information reached drivers after they had committed to "
         "a route; demand sloshed between corridors instead of falling.",
)

ALERT_FATIGUE = Record(
    id="alert-fatigue",
    domain="operations",
    attrs={
        "incident": True, "severity": 3, "year": 2019,
        "checks": {
            "THROTTLES": "Count alerts silenced, snoozed or routed to a "
                         "muted channel by {0} over the last quarter.",
            "DEGRADES": "Measure time-to-acknowledge over the same period. A "
                        "rising trend under constant staffing is degradation.",
            "FAILS": "Identify one real incident that was silenced. If one "
                     "exists, {0} has already failed at least once.",
        },
    },
    types={"oncall": "HUMAN", "alerting": "RESOURCE",
           "alert-volume": "RATE", "noise": "SIGNAL"},
    rels=(
        R("DEPENDS", "oncall", "alerting"),
        R("CAUSES", R("EXCESS", "alert-volume"), R("QUEUEING", "alerting")),
        R("INCREASES", R("QUEUEING", "alerting"), "noise"),
        R("SENSES", "oncall", "noise"),
        R("CAUSES", R("SENSES", "oncall", "noise"),
                    R("THROTTLES", "oncall", "alert-volume")),
        R("CAUSES", R("THROTTLES", "oncall", "alert-volume"),
                    R("DEGRADES", "oncall")),
        R("CAUSES", R("DEGRADES", "oncall"), R("FAILS", "oncall")),
    ),
    text="Alert volume outgrew the team's capacity to read it; responders "
         "filtered aggressively and stopped seeing the real ones.",
)

FISHERY = Record(
    id="fishery-collapse",
    domain="ecology",
    attrs={
        "incident": True, "severity": 5, "year": 1992,
        "checks": {
            "SCARCE": "Compare the extraction rate of {0} against its "
                      "replenishment rate over a full cycle.",
            "REDISTRIBUTES": "Track where effort moves when local yield "
                             "falls. Confirm whether it leaves or intensifies.",
            "EXHAUSTS": "Identify the stock level below which recovery is no "
                        "longer observed historically.",
        },
    },
    types={"fleets": "AGENT", "stock": "RESOURCE",
           "catch-effort": "LOAD", "scarcity-signal": "SIGNAL"},
    rels=(
        R("DEPENDS", "fleets", "stock"),
        R("SHARED", "stock", "fleets"),
        R("CAUSES", R("EXCESS", "catch-effort"), R("SCARCE", "stock")),
        R("INCREASES", R("SCARCE", "stock"), "scarcity-signal"),
        R("SENSES", "fleets", "scarcity-signal"),
        R("CAUSES", R("SENSES", "fleets", "scarcity-signal"),
                    R("REDISTRIBUTES", "catch-effort", "stock")),
        R("CAUSES", R("REDISTRIBUTES", "catch-effort", "stock"),
                    R("EXHAUSTS", "catch-effort", "stock")),
        R("CAUSES", R("EXHAUSTS", "catch-effort", "stock"),
                    R("FAILS", "stock")),
    ),
    text="Falling yield raised effort rather than lowering it; the stock was "
         "fished below the level from which it had ever recovered.",
)

BULLWHIP = Record(
    id="supply-bullwhip",
    domain="supply-chain",
    attrs={
        "incident": True, "severity": 4, "year": 2020,
        "checks": {
            "OSCILLATES": "Plot order quantity by tier over time. Increasing "
                          "amplitude upstream of steady end demand is the "
                          "bullwhip signature.",
            "AMPLIFIES": "Compare order variance at {1} against end-customer "
                         "demand variance. A ratio above 1 is amplification.",
            "DELAYED": "Measure the lag between an order being placed and "
                       "being observable upstream.",
        },
    },
    types={"distributors": "AGENT", "factory": "RESOURCE",
           "orders": "LOAD", "lead-time": "SIGNAL"},
    rels=(
        R("DEPENDS", "distributors", "factory"),
        R("CAUSES", R("EXCESS", "orders"), R("QUEUEING", "factory")),
        R("INCREASES", R("QUEUEING", "factory"), "lead-time"),
        R("SENSES", "distributors", "lead-time"),
        R("CAUSES", R("DELAYED", "lead-time"), R("OSCILLATES", "orders")),
        R("CAUSES", R("OSCILLATES", "orders"),
                    R("AMPLIFIES", "orders", "factory")),
        R("CAUSES", R("AMPLIFIES", "orders", "factory"),
                    R("SATURATES", "factory")),
    ),
    text="Lead times lengthened, so distributors ordered ahead; the factory "
         "saw demand swing far harder than the end market did.",
)

INCIDENTS = [
    RETRY_STORM, POOL_EXHAUSTION, CROWD_CRUSH, GRID_CASCADE, THERMOSTAT,
    CACHE_STAMPEDE, BANK_RUN, HIGHWAY, ALERT_FATIGUE, FISHERY, BULLWHIP,
]

CROSS_DOMAIN_INCIDENTS = INCIDENTS
