"""Tests for Zotero sync deduplication, chunk replacement rollback and deletion sync."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import sync_zotero


# ═══════════════════════════════════════════════════════════════
#  Test helpers
# ═══════════════════════════════════════════════════════════════

class MetadataCollection:
    def __init__(self, rows: list[tuple[str, dict]]):
        self.rows = rows

    def get(self, *, include, limit=1000, offset=0, where=None):
        rows = self.rows
        if where:
            rows = [
                row for row in rows
                if all(row[1].get(key) == value for key, value in where.items())
            ]
        rows = rows[offset:offset + limit]
        return {
            "ids": [row[0] for row in rows],
            "metadatas": [row[1] for row in rows],
        }


class RollbackCollection:
    def __init__(self):
        self.records = {
            "paper#0": {
                "document": "old document",
                "metadata": {"paper_id": "paper"},
                "embedding": [0.1, 0.2],
            }
        }
        self.failed = False

    def get(self, *, include, where):
        rows = [
            (record_id, record)
            for record_id, record in self.records.items()
            if record["metadata"].get("paper_id") == where["paper_id"]
        ]
        return {
            "ids": [row[0] for row in rows],
            "documents": [row[1]["document"] for row in rows],
            "metadatas": [row[1]["metadata"] for row in rows],
            "embeddings": [row[1]["embedding"] for row in rows],
        }

    def delete(self, *, ids):
        for record_id in ids:
            self.records.pop(record_id, None)

    def add(self, *, ids, embeddings, documents, metadatas):
        if ids == ["paper#1"] and not self.failed:
            self.failed = True
            raise RuntimeError("simulated write failure")
        for record_id, embedding, document, metadata in zip(
            ids, embeddings, documents, metadatas
        ):
            self.records[record_id] = {
                "document": document,
                "metadata": metadata,
                "embedding": embedding,
            }


# ═══════════════════════════════════════════════════════════════
#  Deduplication
# ═══════════════════════════════════════════════════════════════

def test_title_dedup_uses_normalized_exact_match():
    collection = MetadataCollection([
        ("paper-1#0", {"paper_id": "paper-1", "title": "A Useful: Paper!"}),
    ])

    assert sync_zotero.deduplicate_paper("", "a useful paper", collection) == "paper-1"
    assert sync_zotero.deduplicate_paper("", "a useful paper extended", collection) is None


def test_duplicate_zotero_item_detection_distinguishes_the_source_item():
    assert sync_zotero.is_duplicate_zotero_item(101, 202)
    assert not sync_zotero.is_duplicate_zotero_item(101, 101)
    assert sync_zotero.is_duplicate_zotero_item(None, 202)


def test_full_rescan_skips_duplicate_before_mineru_extraction(tmp_path, monkeypatch):
    zotero_dir = tmp_path / "Zotero"
    (zotero_dir / "storage").mkdir(parents=True)
    extractor_calls: list[Path] = []

    class Cursor:
        def execute(self, query, params=()):
            return []

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    class Client:
        def __init__(self, path):
            self.path = path

    item = {
        "item_id": 202,
        "key": "DUPLICATE",
        "doi": "10.1000/example",
        "title": "Duplicate paper",
    }
    monkeypatch.setitem(sys.modules, "chromadb", types.SimpleNamespace(PersistentClient=Client))
    monkeypatch.setitem(
        sys.modules,
        "generate_collection_info",
        types.SimpleNamespace(main=lambda: None),
    )
    monkeypatch.setattr(sync_zotero, "get_zotero_db_connection", lambda _: Connection())
    monkeypatch.setattr(sync_zotero, "check_zotero_version", lambda _: 1)
    monkeypatch.setattr(sync_zotero, "get_paper_items", lambda *args, **kwargs: [item])
    monkeypatch.setattr(sync_zotero, "get_or_create_chroma_collection", lambda: object())
    monkeypatch.setattr(sync_zotero, "load_bi_encoder", lambda: object())
    monkeypatch.setattr(sync_zotero, "deduplicate_paper", lambda *args: "paper-1")
    monkeypatch.setattr(sync_zotero, "_get_existing_zotero_item_id", lambda *args: 101)
    monkeypatch.setattr(
        sync_zotero,
        "extract_with_mineru",
        lambda path, *args, **kwargs: extractor_calls.append(path),
    )
    monkeypatch.setattr(sync_zotero, "cleanup_deleted_items", lambda *args: 0)
    monkeypatch.setattr(sync_zotero, "save_checkpoint", lambda _: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sync_zotero.py",
            "--zotero-dir",
            str(zotero_dir),
            "--full-rescan",
            "--skip-build-index",
        ],
    )

    sync_zotero._main_unlocked()

    assert extractor_calls == []


# ═══════════════════════════════════════════════════════════════
#  Chunk replacement rollback
# ═══════════════════════════════════════════════════════════════

def test_replace_paper_chunks_restores_old_data_on_failure():
    collection = RollbackCollection()

    with pytest.raises(RuntimeError, match="simulated write failure"):
        sync_zotero.replace_paper_chunks(
            collection,
            paper_id="paper",
            ids=["paper#0", "paper#1"],
            embeddings=[[1.0, 0.0], [0.0, 1.0]],
            documents=["new zero", "new one"],
            metadatas=[{"paper_id": "paper"}, {"paper_id": "paper"}],
            batch_size=1,
        )

    assert list(collection.records) == ["paper#0"]
    assert collection.records["paper#0"]["document"] == "old document"


def test_successful_chunk_replacement_marks_index_changed(monkeypatch):
    import index_generation

    collection = RollbackCollection()
    collection.failed = True
    marks: list[bool] = []
    monkeypatch.setattr(
        index_generation,
        "mark_index_changed",
        lambda: marks.append(True),
    )

    sync_zotero.replace_paper_chunks(
        collection,
        paper_id="paper",
        ids=["paper#0"],
        embeddings=[[1.0, 0.0]],
        documents=["new document"],
        metadatas=[{"paper_id": "paper"}],
    )

    assert marks == [True]
    assert collection.records["paper#0"]["document"] == "new document"


# ═══════════════════════════════════════════════════════════════
#  Delete-only sync
# ═══════════════════════════════════════════════════════════════

def test_delete_only_sync_still_runs_cleanup(tmp_path, monkeypatch):
    zotero_dir = tmp_path / "Zotero"
    (zotero_dir / "storage").mkdir(parents=True)

    class Cursor:
        def execute(self, query, params=()):
            return []

    class Connection:
        def __init__(self):
            self.closed = False

        def cursor(self):
            return Cursor()

        def close(self):
            self.closed = True

    class Client:
        def __init__(self, path):
            self.path = path

    fake_collection = object()
    cleanup_calls: list[int] = []
    saved: list[dict] = []
    connection = Connection()

    fake_chromadb = types.SimpleNamespace(PersistentClient=Client)
    fake_transformers = types.SimpleNamespace(SentenceTransformer=object)
    fake_collection_info = types.SimpleNamespace(main=lambda: None)
    monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_transformers)
    monkeypatch.setitem(sys.modules, "generate_collection_info", fake_collection_info)
    monkeypatch.setattr(sync_zotero, "get_zotero_db_connection", lambda _: connection)
    monkeypatch.setattr(sync_zotero, "check_zotero_version", lambda _: 42)
    monkeypatch.setattr(sync_zotero, "get_paper_items", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        sync_zotero,
        "load_checkpoint",
        lambda: {"last_item_id": 10, "last_version": 40, "pending_item_ids": []},
    )
    monkeypatch.setattr(sync_zotero, "get_or_create_chroma_collection", lambda: fake_collection)
    monkeypatch.setattr(
        sync_zotero,
        "cleanup_deleted_items",
        lambda cursor, collection, version: cleanup_calls.append(version) or 0,
    )
    monkeypatch.setattr(sync_zotero, "save_checkpoint", lambda state: saved.append(state))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sync_zotero.py",
            "--zotero-dir",
            str(zotero_dir),
            "--skip-build-index",
        ],
    )

    sync_zotero.main()

    assert cleanup_calls == [40]
    assert saved == [{"last_item_id": 10, "last_version": 42, "pending_item_ids": []}]
    assert connection.closed
