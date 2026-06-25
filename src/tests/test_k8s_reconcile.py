"""Unit tests for K8sServiceDiscovery._reconcile_once (stale-engine pruning)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from vllm_router.service_discovery import EndpointInfo, K8sServiceDiscovery


def _endpoint(name: str, ip: str) -> EndpointInfo:
    return EndpointInfo(
        url=f"http://{ip}:8000",
        model_names=["m"],
        Id=name,
        added_timestamp=0,
        model_label="default",
        sleep=False,
        pod_name=name,
        namespace="default",
        model_info={},
    )


def _pod(name: str, ready: bool):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        status=SimpleNamespace(container_statuses=[SimpleNamespace(ready=ready)]),
    )


def _bare_discovery(engines, listed_pods, reconcile_interval=30.0):
    """Build a K8sServiceDiscovery without running __init__ (no k8s/threads)."""
    sd = object.__new__(K8sServiceDiscovery)
    sd.namespace = "default"
    sd.label_selector = "app=vllm"
    sd.reconcile_interval = reconcile_interval
    sd.available_engines = dict(engines)
    import threading

    sd.available_engines_lock = threading.Lock()
    sd.k8s_api = MagicMock()
    sd.k8s_api.list_namespaced_pod.return_value = SimpleNamespace(items=listed_pods)
    return sd


def test_reconcile_drops_engine_whose_pod_is_gone():
    # 'b' is no longer in the cluster list -> must be pruned (the ghost bug).
    sd = _bare_discovery(
        engines={"a": _endpoint("a", "10.0.0.1"), "b": _endpoint("b", "10.0.0.2")},
        listed_pods=[_pod("a", ready=True)],
    )
    sd._reconcile_once()
    assert set(sd.available_engines) == {"a"}


def test_reconcile_drops_engine_whose_pod_is_not_ready():
    sd = _bare_discovery(
        engines={"a": _endpoint("a", "10.0.0.1"), "b": _endpoint("b", "10.0.0.2")},
        listed_pods=[_pod("a", ready=True), _pod("b", ready=False)],
    )
    sd._reconcile_once()
    assert set(sd.available_engines) == {"a"}


def test_reconcile_keeps_all_ready_engines():
    sd = _bare_discovery(
        engines={"a": _endpoint("a", "10.0.0.1"), "b": _endpoint("b", "10.0.0.2")},
        listed_pods=[_pod("a", ready=True), _pod("b", ready=True)],
    )
    sd._reconcile_once()
    assert set(sd.available_engines) == {"a", "b"}


def test_reconcile_prune_only_does_not_add_unknown_ready_pods():
    # 'c' is ready in the cluster but not locally known: adding stays the
    # watch's job, reconcile must not invent it (no per-pod HTTP here).
    sd = _bare_discovery(
        engines={"a": _endpoint("a", "10.0.0.1")},
        listed_pods=[_pod("a", ready=True), _pod("c", ready=True)],
    )
    sd._reconcile_once()
    assert set(sd.available_engines) == {"a"}


def test_reconcile_list_error_keeps_engines_unchanged():
    sd = _bare_discovery(
        engines={"a": _endpoint("a", "10.0.0.1")},
        listed_pods=[],
    )
    sd.k8s_api.list_namespaced_pod.side_effect = RuntimeError("api down")
    sd._reconcile_once()
    # Transient API failure must not wipe the engine list.
    assert set(sd.available_engines) == {"a"}


def test_reconcile_grace_period_keeps_just_added_engine_absent_from_stale_cache():
    # 'b' was just added (recent timestamp) but a stale rv=0 list does not show
    # it yet. The grace window must keep it (the watch will not re-add it).
    import time

    fresh = _endpoint("b", "10.0.0.2")
    fresh.added_timestamp = time.time()
    sd = _bare_discovery(
        engines={"a": _endpoint("a", "10.0.0.1"), "b": fresh},
        listed_pods=[_pod("a", ready=True)],  # 'b' missing from the cache
        reconcile_interval=30.0,
    )
    sd._reconcile_once()
    assert set(sd.available_engines) == {"a", "b"}


def test_reconcile_drops_long_lived_engine_even_within_grace_math():
    # An engine known longer than one interval is a genuine ghost -> pruned.
    import time

    old = _endpoint("b", "10.0.0.2")
    old.added_timestamp = time.time() - 120  # well beyond the 30s interval
    sd = _bare_discovery(
        engines={"a": _endpoint("a", "10.0.0.1"), "b": old},
        listed_pods=[_pod("a", ready=True)],
        reconcile_interval=30.0,
    )
    sd._reconcile_once()
    assert set(sd.available_engines) == {"a"}


def test_reconcile_uses_watch_cache_resource_version():
    sd = _bare_discovery(
        engines={"a": _endpoint("a", "10.0.0.1")},
        listed_pods=[_pod("a", ready=True)],
    )
    sd._reconcile_once()
    _, kwargs = sd.k8s_api.list_namespaced_pod.call_args
    assert kwargs.get("resource_version") == "0"
