from prometheus_client import Counter, Gauge

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
    "Fraction of session requests routed to their sticky engine in the window",
)
cache_aware_fallback_rate = Gauge(
    "vllm:cache_aware_fallback_rate",
    "Fraction of session requests that fell back off the sticky engine in the window",
)
cache_aware_fallback_reason_rate = Gauge(
    "vllm:cache_aware_fallback_reason_rate",
    "Fraction of session requests that fell back due to each threshold in the window",
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
    "Total session requests routed to their sticky engine",
)
cache_aware_fallback_total = Counter(
    "vllm:cache_aware_fallback_total",
    "Total session requests that fell back off the sticky engine",
)
cache_aware_fallback_reason_total = Counter(
    "vllm:cache_aware_fallback_reason_total",
    "Total session requests that fell back due to each threshold",
    ["reason"],
)
