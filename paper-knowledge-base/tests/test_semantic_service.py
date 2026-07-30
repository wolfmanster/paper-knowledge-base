"""Tests for the resident semantic-search service."""

from __future__ import annotations

import sys
import threading
import types
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import semantic_service
from index_generation import mark_index_changed, read_index_generation


def make_fake_query_module(calls: list[str]):
    encoder = types.SimpleNamespace(device="cuda:0")
    reranker = object()
    collection = object()

    def search_with_components(**kwargs):
        calls.append(f"search:{kwargs['query']}:{kwargs['top_k']}")
        assert kwargs["bi_encoder"] is encoder
        assert kwargs["cross_encoder"] is reranker
        assert kwargs["collection"] is collection
        return [{"title": kwargs["query"], "score": 0.9}]

    return types.SimpleNamespace(
        search_with_components=search_with_components,
    )


def test_runtime_reuses_models_and_collection_across_searches(monkeypatch):
    calls: list[str] = []
    encoder = types.SimpleNamespace(device="cuda:0")
    reranker = object()
    collection = object()

    monkeypatch.setattr(semantic_service, "load_bi_encoder", lambda **kwargs: calls.append("bi") or encoder)
    monkeypatch.setattr(semantic_service, "load_cross_encoder", lambda **kwargs: calls.append("cross") or reranker)
    monkeypatch.setattr(semantic_service, "get_or_create_chroma_collection", lambda: calls.append("collection") or collection)

    def search_with_components(**kwargs):
        calls.append(f"search:{kwargs['query']}:{kwargs['top_k']}")
        assert kwargs["bi_encoder"] is encoder
        assert kwargs["cross_encoder"] is reranker
        assert kwargs["collection"] is collection
        return [{"title": kwargs["query"], "score": 0.9}]

    fake_query = types.SimpleNamespace(search_with_components=search_with_components)
    runtime = semantic_service.SemanticRuntime(lambda: fake_query)

    runtime.load()
    first = runtime.search("alpha", 3)
    second = runtime.search("beta", 5)

    assert runtime.health()["status"] == "ready"
    assert runtime.health()["device"] == "cuda:0"
    assert first == [{"title": "alpha", "score": 0.9}]
    assert second == [{"title": "beta", "score": 0.9}]
    assert calls == ["bi", "cross", "collection", "search:alpha:3", "search:beta:5"]


def test_runtime_rejects_results_after_index_generation_changes(monkeypatch):
    calls: list[str] = []
    generation = ["old"]

    def fake_bi(**kwargs):
        return types.SimpleNamespace(device="cpu")

    def fake_cross(**kwargs):
        return object()

    def fake_collection():
        return object()

    monkeypatch.setattr(semantic_service, "load_bi_encoder", fake_bi)
    monkeypatch.setattr(semantic_service, "load_cross_encoder", fake_cross)
    monkeypatch.setattr(semantic_service, "get_or_create_chroma_collection", fake_collection)

    runtime = semantic_service.SemanticRuntime(
        lambda: make_fake_query_module(calls),
        generation_reader=lambda: generation[0],
    )
    runtime.load()

    generation[0] = "new"

    assert runtime.health()["status"] == "stale"
    with pytest.raises(semantic_service.SemanticIndexChangedError):
        runtime.search("fresh papers", 5)


def test_index_generation_marker_is_atomic_and_changes(tmp_path):
    marker = tmp_path / "chroma.generation"

    assert read_index_generation(marker) == "0"
    first = mark_index_changed(marker)
    second = mark_index_changed(marker)

    assert first != second
    assert read_index_generation(marker) == second
    assert list(tmp_path.iterdir()) == [marker]


def test_failed_runtime_load_stops_server(monkeypatch):
    monkeypatch.setattr(semantic_service, "load_bi_encoder",
                        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("model unavailable")))

    class Server:
        stopped = False

        def shutdown(self):
            self.stopped = True

    runtime = semantic_service.SemanticRuntime(lambda: object())
    server = Server()

    semantic_service._load_runtime_and_stop_on_error(runtime, server)

    assert runtime.health()["status"] == "error"
    assert server.stopped


def test_ensure_ready_does_not_start_on_wrong_service(monkeypatch):
    starts: list[bool] = []
    monkeypatch.setattr(
        semantic_service,
        "get_health",
        lambda **kwargs: (_ for _ in ()).throw(
            semantic_service.SemanticServiceError("端口上的进程不是语义服务")
        ),
    )
    monkeypatch.setattr(
        semantic_service,
        "start_background",
        lambda **kwargs: starts.append(True) or 123,
    )

    with pytest.raises(semantic_service.SemanticServiceError, match="不是语义服务"):
        semantic_service.ensure_ready(timeout=0.1)

    assert starts == []


def test_ensure_ready_starts_after_connection_refused(monkeypatch):
    calls = 0
    starts: list[bool] = []

    def get_health(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise semantic_service.SemanticServiceConnectionError("refused")
        return {"status": "ready"}

    monkeypatch.setattr(semantic_service, "get_health", get_health)
    monkeypatch.setattr(
        semantic_service,
        "start_background",
        lambda **kwargs: starts.append(True) or 123,
    )
    monkeypatch.setattr(semantic_service.time, "sleep", lambda _: None)

    assert semantic_service.ensure_ready(timeout=1) == {"status": "ready"}
    assert starts == [True]


def test_stop_service_waits_until_port_is_released(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        semantic_service,
        "get_health",
        lambda **kwargs: calls.append("health") or {"status": "ready"},
    )
    monkeypatch.setattr(
        semantic_service,
        "_request_json",
        lambda *args, **kwargs: calls.append("shutdown") or {"status": "stopping"},
    )
    monkeypatch.setattr(
        semantic_service,
        "_wait_for_service_stop",
        lambda *args, **kwargs: calls.append("wait"),
    )

    assert semantic_service.stop_service() == {"status": "stopping"}
    assert calls == ["health", "shutdown", "wait"]


def test_remote_search_uses_ready_loopback_service(monkeypatch):
    calls: list[str] = []
    encoder = types.SimpleNamespace(device="cpu")
    reranker = object()
    collection = object()

    monkeypatch.setattr(semantic_service, "load_bi_encoder", lambda **kwargs: calls.append("bi") or encoder)
    monkeypatch.setattr(semantic_service, "load_cross_encoder", lambda **kwargs: calls.append("cross") or reranker)
    monkeypatch.setattr(semantic_service, "get_or_create_chroma_collection", lambda: calls.append("collection") or collection)

    def search_with_components(**kwargs):
        calls.append(f"search:{kwargs['query']}:{kwargs['top_k']}")
        assert kwargs["bi_encoder"] is encoder
        assert kwargs["cross_encoder"] is reranker
        assert kwargs["collection"] is collection
        return [{"title": kwargs["query"], "score": 0.9}]

    fake_query = types.SimpleNamespace(search_with_components=search_with_components)
    runtime = semantic_service.SemanticRuntime(lambda: fake_query)
    runtime.load()
    server = semantic_service.create_server("127.0.0.1", 0, runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        result = semantic_service.remote_search(
            "resident models",
            4,
            host="127.0.0.1",
            port=server.server_port,
            auto_start=False,
            startup_timeout=1,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result == [{"title": "resident models", "score": 0.9}]
    assert calls[-1] == "search:resident models:4"


@pytest.mark.parametrize(
    ("query_text", "top_k"),
    [("", 5), ("paper", 0), ("paper", 101), ("paper", True)],
)
def test_remote_search_rejects_invalid_input_before_connecting(query_text, top_k):
    with pytest.raises(ValueError):
        semantic_service.remote_search(query_text, top_k, auto_start=False)
