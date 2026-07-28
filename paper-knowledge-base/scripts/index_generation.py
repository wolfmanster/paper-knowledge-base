"""Chroma 索引代际标记。

Chroma 的进程内 HNSW segment 不会自动感知其他进程的写入。写入脚本在完成
一次知识库变更后更新此标记，常驻搜索服务据此退出并重新加载索引。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
GENERATION_PATH = BASE_DIR / "kb" / "chroma.generation"


def read_index_generation(path: Path = GENERATION_PATH) -> str:
    """读取当前索引代际；标记不存在时返回初始代际。"""
    try:
        return path.read_text(encoding="utf-8").strip() or "0"
    except FileNotFoundError:
        return "0"


def mark_index_changed(path: Path = GENERATION_PATH) -> str:
    """原子更新索引代际并返回新标记。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    generation = f"{time.time_ns()}-{os.getpid()}"
    temp_path = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temp_path.write_text(generation, encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
    return generation
