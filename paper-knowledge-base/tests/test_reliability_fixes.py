"""Regression tests for synchronization and index reliability fixes."""

from __future__ import annotations

import importlib.util
import inspect
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_index
import generate_collection_info
import index_generation
import utils as utils_module
import query as query_module
import sync_zotero
import watch_zotero


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


def test_query_prefers_cuda_when_available(monkeypatch):
    fake_torch = types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: True),
        backends=types.SimpleNamespace(
            mps=types.SimpleNamespace(is_available=lambda: False)
        ),
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    assert utils_module._preferred_model_device() == "cuda"


def test_query_falls_back_to_cpu_without_gpu(monkeypatch):
    fake_torch = types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: False),
        backends=types.SimpleNamespace(
            mps=types.SimpleNamespace(is_available=lambda: False)
        ),
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    assert utils_module._preferred_model_device() == "cpu"


def test_query_model_loaders_receive_preferred_device(monkeypatch):
    calls: list[tuple[str, str, str]] = []

    class FakeSentenceTransformer:
        def __init__(self, model_name, *, device):
            calls.append(("bi", str(model_name), device))

    class FakeCrossEncoder:
        def __init__(self, model_name, *, device):
            calls.append(("cross", str(model_name), device))

    monkeypatch.setattr(utils_module, "_preferred_model_device", lambda: "cuda")
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(
            SentenceTransformer=FakeSentenceTransformer,
            CrossEncoder=FakeCrossEncoder,
        ),
    )

    utils_module.load_bi_encoder()
    utils_module.load_cross_encoder()

    assert calls[0][0::2] == ("bi", "cuda")
    assert calls[1] == (
        "cross",
        "cross-encoder/ms-marco-MiniLM-L-6-v2",
        "cuda",
    )


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


def test_checkpoint_round_trip_preserves_pending_items(tmp_path, monkeypatch):
    checkpoint = tmp_path / "checkpoint.json"
    monkeypatch.setattr(sync_zotero, "CHECKPOINT_FILE", checkpoint)

    sync_zotero.save_checkpoint({
        "last_item_id": 20,
        "last_version": 30,
        "pending_item_ids": [7, 11],
    })

    assert sync_zotero.load_checkpoint() == {
        "last_item_id": 20,
        "last_version": 30,
        "pending_item_ids": [7, 11],
    }


def test_checkpoint_item_id_never_moves_backwards():
    items = [{"item_id": 12}, {"item_id": 27}]

    assert sync_zotero.compute_next_last_item_id(40, items) == 40
    assert sync_zotero.compute_next_last_item_id(10, items) == 27
    assert sync_zotero.compute_next_last_item_id(40, []) == 40


def test_watch_command_uses_current_python_and_optional_paths(tmp_path):
    sync_script = tmp_path / "sync_zotero.py"
    zotero_dir = tmp_path / "Zotero"
    mineru_dir = tmp_path / "MinerU-GUI"

    command = watch_zotero.build_sync_command(
        python_executable="python-test",
        sync_script=sync_script,
        zotero_dir=zotero_dir,
        mineru_dir=mineru_dir,
    )

    assert command == [
        "python-test",
        str(sync_script),
        "--zotero-dir",
        str(zotero_dir),
        "--mineru-dir",
        str(mineru_dir),
    ]


def test_watch_database_signature_tracks_changes(tmp_path):
    database = tmp_path / "zotero.sqlite"

    assert watch_zotero.database_signature(database) is None

    database.write_bytes(b"first")
    first = watch_zotero.database_signature(database)
    database.write_bytes(b"second-version")
    second = watch_zotero.database_signature(database)

    assert first is not None
    assert second is not None
    assert first != second


def test_mineru_python_supports_windows_and_posix_venvs(tmp_path):
    windows_root = tmp_path / "windows-mineru"
    windows_python = windows_root / ".venv" / "Scripts" / "python.exe"
    windows_python.parent.mkdir(parents=True)
    windows_python.touch()

    posix_root = tmp_path / "posix-mineru"
    posix_python = posix_root / ".venv" / "bin" / "python"
    posix_python.parent.mkdir(parents=True)
    posix_python.touch()

    assert sync_zotero.resolve_mineru_python(windows_root) == windows_python
    assert sync_zotero.resolve_mineru_python(posix_root) == posix_python


def test_zotero_mineru_defaults_accept_500_mb_and_all_pages():
    attachment_default = inspect.signature(
        sync_zotero.resolve_attachment_path
    ).parameters["max_size_mb"].default
    pages_default = inspect.signature(
        sync_zotero.extract_with_mineru
    ).parameters["max_pages"].default
    timeout_default = inspect.signature(
        sync_zotero.extract_with_mineru
    ).parameters["timeout_seconds"].default

    assert attachment_default == 500
    assert pages_default == 0
    assert timeout_default == 24 * 60 * 60
    assert watch_zotero.DEFAULT_INTERVAL_SECONDS == 86_400.0


def test_sync_process_lock_excludes_a_second_holder(tmp_path):
    lock_path = tmp_path / "sync.lock"
    first = sync_zotero.SyncProcessLock(lock_path)
    second = sync_zotero.SyncProcessLock(lock_path)

    assert first.acquire(blocking=False)
    try:
        assert not second.acquire(blocking=False)
    finally:
        first.release()

    assert second.acquire(blocking=False)
    second.release()


def test_sync_process_lock_excludes_another_process(tmp_path):
    lock_path = tmp_path / "sync.lock"
    code = (
        "import sys,time;"
        f"sys.path.insert(0,{str(SCRIPTS_DIR)!r});"
        "from pathlib import Path;"
        "import sync_zotero;"
        f"lock=sync_zotero.SyncProcessLock(Path({str(lock_path)!r}));"
        "lock.acquire();print('locked',flush=True);time.sleep(30)"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        contender = sync_zotero.SyncProcessLock(lock_path)
        assert not contender.acquire(blocking=False)
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_captured_process_timeout_terminates_child_tree():
    with pytest.raises(subprocess.TimeoutExpired):
        sync_zotero.run_captured_process(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            timeout_seconds=0.1,
        )


def test_pending_item_is_selected_even_below_checkpoint():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE items (
            itemID INTEGER PRIMARY KEY,
            key TEXT,
            dateAdded TEXT,
            dateModified TEXT,
            version INTEGER,
            itemTypeID INTEGER
        );
        CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
        CREATE TABLE deletedItems (itemID INTEGER, dateDeleted TEXT);
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        CREATE TABLE itemDataValues (valueID INTEGER, value TEXT);
        INSERT INTO itemTypes VALUES (1, 'journalArticle');
        INSERT INTO items VALUES (1, 'KEY', '', '', 1, 1);
        """
    )

    items = sync_zotero.get_paper_items(
        connection.cursor(),
        since_item_id=10,
        since_version=10,
        pending_item_ids=[1],
    )

    assert [item["item_id"] for item in items] == [1]
    connection.close()


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
