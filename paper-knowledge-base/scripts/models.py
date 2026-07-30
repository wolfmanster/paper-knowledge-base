"""嵌入模型加载与 ChromaDB 集合管理。

提供:
  - Bi-Encoder / Cross-Encoder 模型加载（带本地缓存优先 + 设备自动选择）
  - ChromaDB 集合获取/创建
  - top_k 参数校验
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from paths import (
    BI_ENCODER_NAME,
    CHROMA_DIR,
    COLLECTION_NAME,
    CROSS_ENCODER_NAME,
)

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder, SentenceTransformer

logger = logging.getLogger(__name__)


def _preferred_model_device() -> str:
    """优先使用可用的 GPU，未检测到 GPU 时回退到 CPU。"""
    import torch

    if torch.cuda.is_available():
        return "cuda"
    mps_backend = getattr(getattr(torch, "backends", None), "mps", None)
    if mps_backend is not None and mps_backend.is_available():
        return "mps"
    return "cpu"


def load_bi_encoder(device: str = "auto") -> "SentenceTransformer":
    """加载 Bi-Encoder，优先使用本地缓存，然后 HuggingFace。"""
    from sentence_transformers import SentenceTransformer

    model_path = (
        Path.home()
        / ".cache"
        / "huggingface"
        / "hub"
        / "models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2"
        / "snapshots"
        / "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
    )
    if device == "auto":
        device = _preferred_model_device()
    try:
        if model_path.exists():
            return SentenceTransformer(str(model_path), device=device)
        return SentenceTransformer(BI_ENCODER_NAME, device=device)
    except Exception as e:
        logger.error("Bi-Encoder 加载失败: %s", e)
        raise


def load_cross_encoder(device: str = "auto") -> "CrossEncoder | None":
    """加载 Cross-Encoder，失败返回 None（降级为 Bi-Encoder 模式）。"""
    from sentence_transformers import CrossEncoder

    if device == "auto":
        device = _preferred_model_device()
    try:
        return CrossEncoder(CROSS_ENCODER_NAME, device=device)
    except Exception as e:
        logger.warning("Cross-Encoder 加载失败（仅使用 Bi-Encoder）: %s", e)
        return None


def get_or_create_chroma_collection() -> "chromadb.Collection":
    """连接 ChromaDB 并获取或创建论文集合。"""
    import chromadb

    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(str(CHROMA_DIR))
    try:
        return client.get_collection(COLLECTION_NAME)
    except (ValueError, chromadb.errors.NotFoundError):
        return client.create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )


def validate_top_k(top_k: int) -> int:
    """验证 top_k 值在有效范围内（1-100），不合法时返回默认值 5。"""
    try:
        k = int(top_k)
    except (ValueError, TypeError):
        return 5
    return max(1, min(k, 100))
