from prometheus_client import Counter
from prometheus_client import Gauge as _Gauge


def Gauge(*args, **kwargs):
    """Gauge that, in multiprocess (multi-worker) mode, defaults to the
    "mostrecent" aggregation so each metric stays a single coherent series (the
    most recently written value across workers) instead of exploding into a
    per-pid series. Counters already sum across workers automatically; for
    cluster-accurate rates use the ``*_total`` counters with rate() rather than
    these window gauges. The kwarg is ignored when not in multiprocess mode
    (single worker), so single-worker behavior is unchanged."""
    kwargs.setdefault("multiprocess_mode", "mostrecent")
    return _Gauge(*args, **kwargs)


# --- Prometheus Gauges ---
# Existing metrics
num_requests_running = Gauge(
    "vllm:num_requests_running", "Number of running requests", ["server"]
)
num_requests_waiting = Gauge(
    "vllm:num_requests_waiting", "Number of waiting requests", ["server"]
)
gpu_prefix_cache_hit_rate = Gauge(
    "vllm:gpu_prefix_cache_hit_rate",
    "GPU Prefix Cache Hit Rate",
    ["server"],
)
gpu_prefix_cache_hits_total = Gauge(
    "vllm:gpu_prefix_cache_hits_total",
    "Total GPU Prefix Cache Hits",
    ["server"],
)
gpu_prefix_cache_queries_total = Gauge(
    "vllm:gpu_prefix_cache_queries_total",
    "Total GPU Prefix Cache Queries",
    ["server"],
)
current_qps = Gauge("vllm:current_qps", "Current Queries Per Second", ["server"])
avg_decoding_length = Gauge(
    "vllm:avg_decoding_length", "Average Decoding Length", ["server"]
)
num_prefill_requests = Gauge(
    "vllm:num_prefill_requests", "Number of Prefill Requests", ["server"]
)
num_decoding_requests = Gauge(
    "vllm:num_decoding_requests", "Number of Decoding Requests", ["server"]
)

# New metrics per dashboard update
healthy_pods_total = Gauge(
    "vllm:healthy_pods_total", "Number of healthy vLLM pods", ["server"]
)

# --- Router capacity / scale-out signals ------------------------------------
# The router is a single asyncio event loop per worker; these say when a worker
# (not the engines) is the bottleneck and more router replicas are needed.
# livemax: report the worst worker's lag; livesum: total concurrency over workers.
router_event_loop_lag_seconds = Gauge(
    "router_event_loop_lag_seconds",
    "Router asyncio event-loop scheduling lag, windowed max (seconds). High "
    "values mean the worker's loop cannot keep up (CPU / sync blocking / too "
    "many concurrent requests) and /health starts queueing -> add router "
    "replicas. Leading indicator that captures all saturation causes in one "
    "number.",
    multiprocess_mode="livemax",
)
router_active_requests = Gauge(
    "router_active_requests",
    "Requests currently being proxied by the router (summed across workers).",
    multiprocess_mode="livesum",
)
avg_latency = Gauge(
    "vllm:avg_latency", "Average end-to-end request latency", ["server"]
)
avg_itl = Gauge("vllm:avg_itl", "Average Inter-Token Latency", ["server"])
num_requests_swapped = Gauge(
    "vllm:num_requests_swapped", "Number of swapped requests", ["server"]
)

# Cache-aware load balancing routing metrics (sliding window probabilities).
cache_aware_stickiness_rate = Gauge(
    "vllm:cache_aware_stickiness_rate",
    "Fraction of session requests whose sticky engine was not overloaded "
    "(routed to it) in the window",
)
cache_aware_fallback_rate = Gauge(
    "vllm:cache_aware_fallback_rate",
    "Fraction of session requests whose sticky engine was overloaded "
    "(fallback triggered; request stays on it only if all engines are overloaded) "
    "in the window",
)
cache_aware_fallback_reason_rate = Gauge(
    "vllm:cache_aware_fallback_reason_rate",
    "Fraction of session requests for which each threshold triggered fallback "
    "in the window",
    ["reason"],
)
cache_aware_inflight_requests = Gauge(
    "vllm:cache_aware_inflight_requests",
    "Requests this router has dispatched to each engine but not yet seen complete",
    ["server"],
)
# Cumulative counters since router start (use rate() in Prometheus for any window).
cache_aware_sticky_total = Counter(
    "vllm:cache_aware_sticky_total",
    "Total session requests whose sticky engine was not overloaded",
)
cache_aware_fallback_total = Counter(
    "vllm:cache_aware_fallback_total",
    "Total session requests whose sticky engine was overloaded (fallback "
    "triggered; stays on it only if all engines are overloaded)",
)
cache_aware_fallback_reason_total = Counter(
    "vllm:cache_aware_fallback_reason_total",
    "Total session requests for which each threshold triggered fallback",
    ["reason"],
)

# --- Returning-session request funnel ---------------------------------------
# Every session request this router routes is partitioned, within the returning
# TTL, into exactly one of:
#   * first visit  -- first time this session id is seen (one-shot or the first
#                     of several visits). These should be excluded when judging
#                     how sticky *returning* users are.
#   * returning    -- the session was seen before (2nd+ visit). This is the
#                     subset that reflects whether the upstream multi-provider
#                     layer actually sent the same session back to this provider
#                     AND whether this router then kept it on its sticky engine.
# Each of the two is further split into sticky / fallback, mirroring the base
# cache_aware_* metrics. Invariants (cumulative counters):
#   first_visit_routed + returning_routed == sticky_total + fallback_total
#   first_visit_sticky + returning_sticky == sticky_total
#   first_visit_fallback + returning_fallback == fallback_total
cache_aware_first_visit_routed_total = Counter(
    "vllm:cache_aware_first_visit_routed_total",
    "Total session requests routed on the first visit of a session id (within "
    "the returning TTL)",
)
cache_aware_first_visit_sticky_total = Counter(
    "vllm:cache_aware_first_visit_sticky_total",
    "First-visit session requests whose sticky engine was not overloaded",
)
cache_aware_first_visit_fallback_total = Counter(
    "vllm:cache_aware_first_visit_fallback_total",
    "First-visit session requests whose sticky engine was overloaded (fallback)",
)
cache_aware_returning_routed_total = Counter(
    "vllm:cache_aware_returning_routed_total",
    "Total session requests routed for a returning session (2nd+ visit within "
    "the returning TTL)",
)
cache_aware_returning_sticky_total = Counter(
    "vllm:cache_aware_returning_sticky_total",
    "Returning session requests whose sticky engine was not overloaded",
)
cache_aware_returning_fallback_total = Counter(
    "vllm:cache_aware_returning_fallback_total",
    "Returning session requests whose sticky engine was overloaded (fallback)",
)

# Sliding-window probabilities for the funnel (decay to 0 when traffic stops).
cache_aware_first_visit_request_ratio = Gauge(
    "vllm:cache_aware_first_visit_request_ratio",
    "Fraction of session requests in the window that were a session's first "
    "visit (|N| / |U|)",
)
cache_aware_returning_request_ratio = Gauge(
    "vllm:cache_aware_returning_request_ratio",
    "Fraction of session requests in the window that were returning visits "
    "(|R| / |U|)",
)
cache_aware_returning_stickiness_rate = Gauge(
    "vllm:cache_aware_returning_stickiness_rate",
    "Among returning session requests in the window, fraction whose sticky "
    "engine was not overloaded (R_sticky / |R|)",
)
cache_aware_returning_fallback_rate = Gauge(
    "vllm:cache_aware_returning_fallback_rate",
    "Among returning session requests in the window, fraction whose sticky "
    "engine was overloaded (R_fallback / |R|)",
)

# Returning-session store backend health (Redis). Incremented on any fail-open
# event; routing is unaffected but returning metrics degrade while this rises.
cache_aware_returning_session_store_errors_total = Counter(
    "vllm:cache_aware_returning_session_store_errors_total",
    "Returning-session store backend errors (fail-open; routing unaffected)",
    ["operation"],
)

# --- load_balanced_affinity routing ---
# hit + shed + placement partition every request: a returning session is either
# kept on its remembered engine (hit) or moved to a lighter one (shed); anything
# else (first visit / no session id / affinity off) is a fresh P2C placement.
load_balanced_inflight_requests = Gauge(
    "vllm:load_balanced_inflight_requests",
    "load_balanced_affinity: in-flight requests dispatched by this router",
    ["server"],
)
load_balanced_affinity_hit_total = Counter(
    "vllm:load_balanced_affinity_hit_total",
    "load_balanced_affinity: returning sessions kept on their remembered engine",
)
load_balanced_affinity_shed_total = Counter(
    "vllm:load_balanced_affinity_shed_total",
    "load_balanced_affinity: returning sessions moved off a busier remembered "
    "engine to a lighter one (affinity sacrificed for balance)",
)
load_balanced_placement_total = Counter(
    "vllm:load_balanced_placement_total",
    "load_balanced_affinity: requests placed by power-of-two-choices",
)
load_balanced_affinity_hit_rate = Gauge(
    "vllm:load_balanced_affinity_hit_rate",
    "load_balanced_affinity: windowed affinity-hit fraction, hit / (hit + shed)",
)
# Shared session->engine store backend errors (redis). Incremented on any
# fail-open event; routing falls back to a fresh placement while this rises.
load_balanced_affinity_store_errors_total = Counter(
    "vllm:load_balanced_affinity_store_errors_total",
    "load_balanced_affinity: session->engine store backend errors (fail-open; "
    "routing degrades to plain load balancing)",
    ["operation"],
)
