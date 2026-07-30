"""
论文知识库 — 集合信息生成工具
==============================
从 ChromaDB 中已有的论文元数据，自动生成 kb/collection_info.json，
描述当前论文集合的领域、数量、关键词等信息。

该文件被 CLI 搜索界面和 paper-search Skill 读取，用于动态展示
论文库的领域描述，确保描述与实际论文内容匹配。

用法:
  python scripts/generate_collection_info.py
"""

import json
import logging
from typing import Any

from paths import CHROMA_DIR, COLLECTION_NAME
from utils import has_chinese

COLLECTION_INFO_FILE = CHROMA_DIR.parent / "collection_info.json"

logger = logging.getLogger(__name__)


def _extract_keywords(titles: list[str], max_keywords: int = 10) -> list[str]:
    """从标题列表中提取高频关键词。

    收集所有标题中最长出现的词（英文）或双字以上词（中文），
    按频率排序返回 top-N。
    """
    import re
    from collections import Counter

    word_counts: Counter[str] = Counter()

    for title in titles:
        if not title:
            continue
        # 英文词
        en_words = re.findall(r"[a-zA-Z]+", title)
        word_counts.update(w.lower() for w in en_words if len(w) > 2)

        # 中文字（双字及以上）
        cn_chars = re.findall(r"[一-鿿]+", title)
        for segment in cn_chars:
            if len(segment) >= 2:
                word_counts.update([segment])

    return [w for w, _ in word_counts.most_common(max_keywords)]


def generate_collection_info() -> dict[str, Any]:
    """从 ChromaDB 读取论文标题和数量，生成集合描述。"""
    try:
        import chromadb
    except ImportError:
        logger.warning("chromadb 未安装，使用空数据")
        return _default_info()

    try:
        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        collection = client.get_collection(COLLECTION_NAME)
        chunk_count = collection.count()
    except Exception as e:
        logger.warning("读取 ChromaDB 失败: %s", e)
        return _default_info()

    if chunk_count == 0:
        return _default_info()

    # 读取所有标题
    try:
        all_data = collection.get(include=["metadatas"])
        papers: dict[str, str] = {}
        for index, metadata in enumerate(all_data.get("metadatas") or []):
            if not metadata:
                continue
            paper_id = str(
                metadata.get("paper_id")
                or metadata.get("zotero_key")
                or metadata.get("filename")
                or f"unknown-{index}"
            )
            papers.setdefault(paper_id, metadata.get("title", ""))
        titles = list(papers.values())
        paper_count = len(papers)
    except Exception as e:
        logger.warning("读取元数据失败: %s", e)
        return {
            "name": "My Paper Collection",
            "description": f"A collection containing {chunk_count} searchable text chunks.",
            "paper_count": 0,
            "chunk_count": chunk_count,
            "keywords": [],
            "language": "en",
        }

    keywords = _extract_keywords(titles)

    # 检测是否有中文内容
    has_cn = any(has_chinese(t) for t in titles if t)
    lang = "zh" if has_cn else "en"

    return {
        "name": "My Paper Collection",
        "description": (
            f"A collection of {paper_count} academic papers"
            + (f" covering topics related to {', '.join(keywords[:5])}." if keywords else ".")
        ),
        "paper_count": paper_count,
        "chunk_count": chunk_count,
        "keywords": keywords,
        "language": lang,
    }


def _default_info() -> dict[str, Any]:
    """返回空集合的默认信息。"""
    return {
        "name": "My Paper Collection",
        "description": "A collection of academic papers. Import papers first to generate collection info.",
        "paper_count": 0,
        "chunk_count": 0,
        "keywords": [],
        "language": "en",
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    info = generate_collection_info()
    COLLECTION_INFO_FILE.parent.mkdir(parents=True, exist_ok=True)
    COLLECTION_INFO_FILE.write_text(
        json.dumps(info, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(info, ensure_ascii=False))
    logger.info("集合信息已写入 %s", COLLECTION_INFO_FILE)


if __name__ == "__main__":
    main()
