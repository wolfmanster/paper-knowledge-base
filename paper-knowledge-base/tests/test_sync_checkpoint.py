"""Tests for sync checkpoint, process lock, MinerU paths and subprocess capture."""

from __future__ import annotations

import inspect
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import sync_zotero
import watch_zotero


# ═══════════════════════════════════════════════════════════════
#  Checkpoint management
# ═══════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════
#  Sync process lock
# ═══════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════
#  MinerU path resolution
# ═══════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════
#  Subprocess capture
# ═══════════════════════════════════════════════════════════════

def test_captured_process_timeout_terminates_child_tree():
    with pytest.raises(subprocess.TimeoutExpired):
        sync_zotero.run_captured_process(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            timeout_seconds=0.1,
        )
