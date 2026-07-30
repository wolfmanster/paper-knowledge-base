"""Tests for `watch_zotero.py` — sync command building and DB signature tracking."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import watch_zotero


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
