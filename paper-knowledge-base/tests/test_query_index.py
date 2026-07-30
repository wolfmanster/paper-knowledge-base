"""Tests for query module, FTS index build and collection info generation."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_index
import generate_collection_info
import query as query_module


# ═══════════════════════════════════════════════════════════════
#  Query module deferred imports
# ═══════════════════════════════════════════════════════════════

def test_query_module_defers_sentence_transformers_import(monkeypatch):
    """Text-mode CLI startup must not load the semantic-search dependencies."""

    class BlockSentenceTransformers:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "sentence_transformers":
                raise AssertionError("query.py imported sentence_transformers eagerly")

    blocker = BlockSentenceTransformers()
    monkeypatch.setattr(sys, "meta_path", [blocker, *sys.meta_path])
    existing_transformers = sys.modules.pop("sentence_transformers", None)

    module_name = "query_without_semantic_dependencies"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / "query.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
        if existing_transformers is not None:
            sys.modules["sentence_transformers"] = existing_transformers


# ═══════════════════════════════════════════════════════════════
#  Query fallback (cosine similarity when cross-encoder unavailable)
# ═══════════════════════════════════════════════════════════════

def test_query_fallback_reports_cosine_similarity(monkeypatch):
    class Encoder:
        def encode(self, values, normalize_embeddings):
            return types.SimpleNamespace(tolist=lambda: [[1.0, 0.0]])

    class Collection:
        def query(self, **kwargs):
            assert "distances" in kwargs["include"]
            return {
                "ids": [["a#0", "b#0"]],
                "documents": [["alpha", "beta"]],
                "metadatas": [[
                    {"title": "Alpha", "filename": "alpha.pdf"},
                    {"title": "Beta", "filename": "beta.pdf"},
                ]],
                "distances": [[0.2, 0.7]],
            }

    monkeypatch.setattr(query_module, "load_bi_encoder", lambda: Encoder())
    monkeypatch.setattr(query_module, "load_cross_encoder", lambda: None)
    monkeypatch.setattr(query_module, "_get_collection", lambda: Collection())

    results = query_module.search("paper", top_k=2)

    assert [result["score"] for result in results] == [0.8, 0.3]


# ═══════════════════════════════════════════════════════════════
#  Collection info generation
# ═══════════════════════════════════════════════════════════════

def test_collection_info_counts_unique_papers(monkeypatch):
    class Collection:
        def count(self):
            return 3

        def get(self, *, include):
            return {
                "metadatas": [
                    {"paper_id": "a", "title": "Alpha cooling"},
                    {"paper_id": "a", "title": "Alpha cooling"},
                    {"paper_id": "b", "title": "Beta heating"},
                ]
            }

    class Client:
        def __init__(self, path):
            pass

        def get_collection(self, name):
            return Collection()

    monkeypatch.setitem(sys.modules, "chromadb", types.SimpleNamespace(PersistentClient=Client))

    info = generate_collection_info.generate_collection_info()

    assert info["paper_count"] == 2
    assert info["chunk_count"] == 3


# ═══════════════════════════════════════════════════════════════
#  FTS index build (failure must preserve existing index)
# ═══════════════════════════════════════════════════════════════

def test_failed_index_build_preserves_existing_database(tmp_path, monkeypatch):
    index_path = tmp_path / "index.db"
    index_path.write_bytes(b"existing-index")

    class Collection:
        def count(self):
            return 1

        def get(self, *, include, limit, offset):
            if offset:
                return {"ids": [], "documents": [], "metadatas": []}
            return {
                "ids": ["paper#0"],
                "documents": ["paper text"],
                "metadatas": [{"paper_id": "paper", "chunk_index": 0}],
            }

    class Client:
        def __init__(self, path):
            pass

        def get_collection(self, name):
            return Collection()

    monkeypatch.setattr(build_index, "INDEX_DB", index_path)
    monkeypatch.setattr("chromadb.PersistentClient", Client)
    monkeypatch.setattr(
        build_index,
        "extract_abstract",
        lambda _: (_ for _ in ()).throw(RuntimeError("extract failed")),
    )

    with pytest.raises(RuntimeError, match="extract failed"):
        build_index.main()

    assert index_path.read_bytes() == b"existing-index"
    assert not list(tmp_path.glob("*.db.tmp"))
