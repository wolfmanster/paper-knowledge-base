"""
论文知识库 — 非交互式查询（供 Skill 调用）
========================================
用法:
  python query.py "你的问题" [top_k]                   语义搜索（默认）
  python query.py --mode text "关键字" [top_k]         文本搜索（快速）
  python query.py --mode semantic "你的问题" [top_k]   显式语义搜索

输出：JSON 格式搜索结果
"""

import json
import math
import sys
from pathlib import Path
from typing import TYPE_CHECKING

# Windows GBK/CP936 兼容：调整现有流，不替换宿主进程的 stdout。
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder, SentenceTransformer

BASE_DIR = Path(__file__).resolve().parent.parent

# 确保能找到 scripts/ 下的模块
SCRIPTS_DIR = BASE_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

CHROMA_DIR = BASE_DIR / "kb" / "chroma"
COLLECTION_NAME = "papers"

def _load_bi_encoder() -> "SentenceTransformer":
    """加载 Bi-Encoder，失败时打印错误并退出。"""
    model_path = (
        Path.home()
        / ".cache"
        / "huggingface"
        / "hub"
        / "models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2"
        / "snapshots"
        / "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
    )
    try:
        from sentence_transformers import SentenceTransformer

        if model_path.exists():
            return SentenceTransformer(str(model_path))
        return SentenceTransformer(
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
        )
    except Exception as e:
        _fail(f"Bi-Encoder 加载失败: {e}")


def _load_cross_encoder():
    """加载 Cross-Encoder，失败返回 None（降级为 Bi-Encoder 模式）。"""
    try:
        from sentence_transformers import CrossEncoder

        return CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
    except Exception as e:
        sys.stderr.write(f"[WARN] Cross-Encoder 加载失败（仅使用 Bi-Encoder）: {e}\n")
        return None


def _get_collection():
    """连接 ChromaDB 并获取论文集合，失败时打印错误并退出。"""
    try:
        import chromadb

        client = chromadb.PersistentClient(str(CHROMA_DIR))
        return client.get_collection(COLLECTION_NAME)
    except ValueError:
        _fail("Chroma 集合不存在。请运行: python scripts/ingest.py")
    except Exception as e:
        _fail(f"ChromaDB 连接失败: {e}")


def _fail(msg: str):
    print(json.dumps({"error": msg}, ensure_ascii=False))
    sys.exit(1)


def _sigmoid(x: float) -> float:
    """将 unbounded logit 映射到 [0, 1] 区间。"""
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 1.0 if x > 0 else 0.0


def get_paper_chunks(
    filename: str = "",
    paper_id: str = "",
    max_chars: int = 8000,
) -> list[dict]:
    """从 ChromaDB 按 filename 或 paper_id 获取论文的全部文本块。

    返回按 chunk_index 排序的块列表，每个块包含 text / section / chunk_index。
    这是读取论文全文的推荐方式——不依赖 PDF 文件是否存在。
    """
    if not filename and not paper_id:
        return []

    collection = _get_collection()

    # 用 where 子句过滤
    where_clause = {}
    if filename:
        where_clause["filename"] = filename
    if paper_id:
        where_clause["paper_id"] = paper_id

    try:
        results = collection.get(
            where=where_clause,
            include=["documents", "metadatas"],
        )
    except Exception:
        return []

    if not results["ids"]:
        return []

    # 组装并排序
    chunks = []
    for _doc_id, doc, meta in zip(
        results["ids"], results["documents"], results["metadatas"]
    ):
        chunks.append({
            "text": doc,
            "section": meta.get("section", ""),
            "chunk_index": meta.get("chunk_index", 0),
        })

    chunks.sort(key=lambda c: c["chunk_index"])

    # 按 max_chars 截断
    total = 0
    truncated = []
    for c in chunks:
        total += len(c["text"])
        if total > max_chars:
            break
        truncated.append(c)

    return truncated


def search(query: str, top_k: int = 5) -> list:
    """两阶段搜索：Bi-Encoder 初检 → Cross-Encoder 重排。"""
    bi_encoder = _load_bi_encoder()
    cross_encoder = _load_cross_encoder()
    collection = _get_collection()

    # Bi-Encoder 初检
    query_emb = bi_encoder.encode([query], normalize_embeddings=True).tolist()
    initial_k = max(top_k * 2, 20)
    results = collection.query(
        query_embeddings=query_emb,
        n_results=initial_k,
        include=["documents", "metadatas", "distances"],
    )

    if not results["ids"] or not results["ids"][0]:
        return []

    documents = results["documents"][0]
    metadatas = results["metadatas"][0]
    ids = results["ids"][0]

    # Cross-Encoder 重排
    if cross_encoder is not None:
        pairs = [[query, doc] for doc in documents]
        scores = [_sigmoid(float(s)) for s in cross_encoder.predict(pairs)]
        ranked = sorted(
            zip(scores, ids, documents, metadatas), key=lambda x: x[0], reverse=True
        )[:top_k]
    else:
        distances = results["distances"][0]
        ranked = [
            (1.0 - float(distance), i, d, m)
            for distance, i, d, m in zip(distances, ids, documents, metadatas)
        ][:top_k]

    # 格式化输出
    from utils import extract_title_from_filename

    output = []
    for score, doc_id, doc, meta in ranked:
        title = meta.get("title") or extract_title_from_filename(Path(meta.get("filename", "")))
        output.append({
            "score": round(float(score), 4),
            "title": title or "Unknown",
            "section": meta.get("section", ""),
            "filename": meta.get("filename", ""),
            "preview": doc[:300],
        })

    return output


if __name__ == "__main__":
    # ── 参数解析 ──────────────────────────────────────────
    # 用法:
    #   python query.py <查询语句> [top_k]                      语义搜索（默认）
    #   python query.py --mode text <查询语句> [top_k]           文本搜索
    #   python query.py -m text <查询语句> [top_k]
    #   python query.py --mode semantic <查询语句> [top_k]      显式语义搜索
    #   python query.py --get-paper-chunks "filename.pdf"       获取论文全文块
    #   python query.py -g "filename.pdf"                       同上（简写）
    #   python query.py --version                               显示版本号

    # --version 快速退出（不延迟导入）
    if "--version" in sys.argv[1:]:
        _version_file = BASE_DIR.parent / "_version.py"
        if _version_file.exists():
            _ver: dict[str, str] = {}
            exec(_version_file.read_text(encoding="utf-8"), _ver)
            print(f"paper-knowledge-base {_ver.get('__version__', 'unknown')}")
        else:
            print("paper-knowledge-base unknown (no _version.py)")
        sys.exit(0)

    search_mode = "semantic"
    get_chunks_filename = None
    args: list[str] = sys.argv[1:]

    # 解析 --get-paper-chunks / -g
    i = 0
    while i < len(args):
        if args[i] in ("--get-paper-chunks", "-g"):
            if i + 1 < len(args):
                get_chunks_filename = args[i + 1]
                del args[i : i + 2]
            else:
                _fail("--get-paper-chunks / -g 需要一个 filename 参数")
            break
        elif args[i] in ("--mode", "-m"):
            if i + 1 < len(args):
                search_mode = args[i + 1].lower()
                if search_mode not in ("text", "semantic"):
                    _fail(f"无效的 --mode 值: {search_mode}，可选: text | semantic")
                del args[i : i + 2]
            else:
                _fail("--mode 需要参数: text | semantic")
            break
        else:
            # 如果第一个参数不是 flag，使用旧的 positional 模式
            break

    # 优先处理 --get-paper-chunks
    if get_chunks_filename:
        chunks = get_paper_chunks(filename=get_chunks_filename)
        full_text = "\n\n".join(
            f"[{c['chunk_index']}] ({c['section']}) {c['text']}"
            for c in chunks
        )
        print(json.dumps({
            "filename": get_chunks_filename,
            "chunk_count": len(chunks),
            "total_chars": len(full_text),
            "full_text": full_text[:100000],  # 安全截断
        }, ensure_ascii=False, indent=2))
        sys.exit(0)

    if len(args) < 1:
        _fail("用法: python query.py [--mode text|semantic] <查询语句> [top_k]")

    query = args[0]
    if not query or not query.strip():
        _fail("查询内容不能为空。用法: python query.py [--mode text|semantic] <查询语句> [top_k]")

    try:
        top_k = int(args[1]) if len(args) > 1 else 5
    except ValueError:
        _fail("top_k 必须为整数")

    if search_mode == "text":
        # 文本/关键词搜索
        try:
            from quick_search import search as text_search
        except ImportError as e:
            _fail(f"无法加载 quick_search 模块: {e}")

        results = text_search(query, top_k)
    else:
        # 语义搜索
        results = search(query, top_k)

    print(json.dumps(results, ensure_ascii=False, indent=2))
